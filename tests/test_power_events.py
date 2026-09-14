"""Tests for daemon/power_events.py: logind signal handlers, the systemd-inhibit
delay-lock lifecycle, and callback error handling. D-Bus is never touched
(start() is not called); subprocess spawning is faked at the boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from dbus_fast import Variant

from lgtvcompanion.daemon import power_events


def make_pe():
    calls: list[str] = []

    def cb(name: str):
        async def run() -> None:
            calls.append(name)
        return run

    pe = power_events.PowerEvents(
        on_suspend=cb("suspend"), on_resume=cb("resume"),
        on_shutdown=cb("shutdown"), on_reboot=cb("reboot"))
    return pe, calls


def fake_inhibitor(terms: list, returncode: int | None = None):
    return SimpleNamespace(returncode=returncode,
                           terminate=lambda: terms.append("terminated"))


async def poll_until(pred, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not met before timeout"
        await asyncio.sleep(0.01)


# --- PrepareForSleep -----------------------------------------------------------

async def test_prepare_for_sleep_runs_suspend_and_releases_inhibitor():
    pe, calls = make_pe()
    terms: list = []
    pe._inhibitor = fake_inhibitor(terms)
    pe._prepare_for_sleep(True)
    await poll_until(lambda: calls == ["suspend"])
    assert terms == ["terminated"]
    assert pe._inhibitor is None


async def test_prepare_for_sleep_resume_edge_retakes_inhibitor(monkeypatch):
    pe, calls = make_pe()
    proc = SimpleNamespace(returncode=None, terminate=lambda: None)
    monkeypatch.setattr(power_events.shutil, "which",
                        lambda name: "/usr/bin/systemd-inhibit")

    async def fake_exec(*args, **kwargs):
        assert args[0] == "/usr/bin/systemd-inhibit"
        return proc

    # never spawn a real `systemd-inhibit ... sleep infinity` on CI
    monkeypatch.setattr(power_events.asyncio, "create_subprocess_exec", fake_exec)
    pe._prepare_for_sleep(False)
    await poll_until(lambda: calls == ["resume"])
    assert pe._inhibitor is proc


# --- PrepareForShutdown(WithMetadata) -------------------------------------------

async def test_shutdown_metadata_reboot_variant():
    pe, calls = make_pe()
    pe._prepare_for_shutdown_with_metadata(True, {"type": Variant("s", "reboot")})
    await poll_until(lambda: calls == ["reboot"])
    assert pe._saw_metadata is True


async def test_shutdown_metadata_plain_poweroff():
    pe, calls = make_pe()
    pe._prepare_for_shutdown_with_metadata(True, {"type": "poweroff"})
    await poll_until(lambda: calls == ["shutdown"])


async def test_shutdown_metadata_stop_edge_sets_flag_only():
    pe, calls = make_pe()
    pe._prepare_for_shutdown_with_metadata(False, {})
    assert pe._saw_metadata is True
    await asyncio.sleep(0.05)
    assert calls == []


async def test_plain_shutdown_suppressed_after_metadata():
    pe, calls = make_pe()
    pe._prepare_for_shutdown_with_metadata(True, {"type": "poweroff"})
    await poll_until(lambda: calls == ["shutdown"])
    pe._prepare_for_shutdown(True)  # deduped: metadata signal already handled
    await asyncio.sleep(0.05)
    assert calls == ["shutdown"]


async def test_plain_shutdown_fires_without_metadata():
    pe, calls = make_pe()
    pe._prepare_for_shutdown(False)  # stop edge: no callback
    await asyncio.sleep(0.02)
    assert calls == []
    pe._prepare_for_shutdown(True)
    await poll_until(lambda: calls == ["shutdown"])


# --- inhibitor lifecycle --------------------------------------------------------

async def test_run_then_release_swallows_error_and_releases():
    pe, _calls = make_pe()
    terms: list = []
    pe._inhibitor = fake_inhibitor(terms)

    async def bad() -> None:
        raise RuntimeError("boom")

    await pe._run_then_release(bad)  # must not raise
    assert terms == ["terminated"]
    assert pe._inhibitor is None


def test_release_inhibitor_skips_exited_process():
    pe, _ = make_pe()
    terms: list = []
    pe._inhibitor = fake_inhibitor(terms, returncode=0)
    pe._release_inhibitor()
    assert terms == []  # already exited -> terminate not called
    assert pe._inhibitor is None


def test_release_inhibitor_swallows_process_lookup_error():
    pe, _ = make_pe()

    def gone() -> None:
        raise ProcessLookupError

    pe._inhibitor = SimpleNamespace(returncode=None, terminate=gone)
    pe._release_inhibitor()  # must not raise
    assert pe._inhibitor is None


async def test_take_inhibitor_is_idempotent_while_alive(monkeypatch):
    pe, _ = make_pe()
    live = fake_inhibitor([])
    pe._inhibitor = live
    monkeypatch.setattr(
        power_events.shutil, "which",
        lambda name: pytest.fail("which() called despite live inhibitor"))
    await pe._take_inhibitor()
    assert pe._inhibitor is live


async def test_take_inhibitor_without_systemd_inhibit(monkeypatch):
    pe, _ = make_pe()
    monkeypatch.setattr(power_events.shutil, "which", lambda name: None)
    await pe._take_inhibitor()
    assert pe._inhibitor is None


async def test_take_inhibitor_exec_failure_is_swallowed(monkeypatch, tmp_path):
    pe, _ = make_pe()
    monkeypatch.setattr(power_events.shutil, "which",
                        lambda name: str(tmp_path / "nope"))
    await pe._take_inhibitor()  # FileNotFoundError (an OSError) -> warning only
    assert pe._inhibitor is None


# --- stop / connect_logind_manager ----------------------------------------------

async def test_stop_releases_inhibitor_and_disconnects_bus():
    pe, _ = make_pe()
    disconnects: list = []
    terms: list = []
    pe._bus = SimpleNamespace(disconnect=lambda: disconnects.append(1))
    pe._inhibitor = fake_inhibitor(terms)
    await pe.stop()
    assert disconnects == [1]
    assert terms == ["terminated"]
    assert pe._inhibitor is None


async def test_connect_logind_manager_returns_none_on_error(monkeypatch):
    class BrokenBus:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("no system bus in tests")

    monkeypatch.setattr(power_events, "MessageBus", BrokenBus)
    assert await power_events.connect_logind_manager() is None


async def test_resumed_logs_resume_callback_error(monkeypatch):
    monkeypatch.setattr(power_events.shutil, "which", lambda n: None)  # no inhibitor

    async def boom():
        raise RuntimeError("resume failed")

    pe = power_events.PowerEvents(
        on_suspend=boom, on_resume=boom, on_shutdown=boom, on_reboot=boom)
    await pe._resumed()                         # on_resume raises -> logged (150-151)
