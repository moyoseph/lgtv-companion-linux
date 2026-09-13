"""Daemon construction branches, agent-report routing, idle mute, topology
persistence edges, and the full run() lifecycle over a real IPC socket."""

from __future__ import annotations

import asyncio
import contextlib

from lgtvcompanion import config as config_mod
from lgtvcompanion import ipc
from lgtvcompanion.config import DeviceConfig
from lgtvcompanion.daemon import main as daemon_main

from .harness import FakeSession, make_daemon, make_session, short_sock


def _full_cfg():
    cfg = config_mod.Config(devices=[DeviceConfig(id="tv1", host="127.0.0.1")])
    cfg.global_.wake_on_input.enabled = True
    cfg.global_.topology.enabled = True
    cfg.global_.idle.enabled = True
    cfg.global_.daemon_power_events = True
    return cfg


# -- __init__ optional subsystems -----------------------------------------------


def test_init_builds_optional_subsystems(tmp_path):
    d = make_daemon(tmp_path, cfg=_full_cfg())
    assert d.wake_on_input is not None
    assert d.topology is not None
    assert d.idle_engine is not None
    assert d.power_events is not None


def test_init_defaults(tmp_path):
    d = make_daemon(tmp_path)
    assert d.wake_on_input is not None          # wake-on-input is on by default
    assert d.topology is None
    assert d.idle_engine is None
    assert d.power_events is None


# -- agent-report routing ----------------------------------------------------------


async def test_report_mpris_updates_vetoes(tmp_path):
    d = make_daemon(tmp_path)
    d._on_report({"mpris_playing": True, "fullscreen": None})
    assert "MPRIS" in (await d.vetoes.check() or "")


async def test_report_streaming_drives_stream_controller(tmp_path):
    cfg = config_mod.Config(devices=[DeviceConfig(id="tv1", host="127.0.0.1")])
    cfg.global_.remote_stream.enabled = True
    cfg.global_.remote_stream.on_connect = "blank"
    cfg.global_.remote_stream.on_disconnect = "restore"
    d = make_daemon(tmp_path, cfg=cfg)
    d.sessions = [FakeSession("tv1")]
    d._on_report({"streaming": True})
    for _ in range(200):
        if d.sessions[0].calls:
            break
        await asyncio.sleep(0.01)
    assert d.sessions[0].calls == ["blank"]
    d._on_report({"streaming": False})
    for _ in range(200):
        if len(d.sessions[0].calls) > 1:
            break
        await asyncio.sleep(0.01)
    assert d.sessions[0].calls == ["blank", "on"]


async def test_report_key_activity_wakes_unreachable_tv(tmp_path):
    cfg = config_mod.Config(devices=[DeviceConfig(id="tv1", host="127.0.0.1")])
    cfg.global_.wake_on_input.enabled = True
    cfg.global_.wake_on_input.cooldown_s = 0.0
    d = make_daemon(tmp_path, cfg=cfg)
    d.sessions = [FakeSession("tv1", reachable=False)]
    d._on_report({"activity": True, "key": True})
    for _ in range(200):
        if d.sessions[0].calls:
            break
        await asyncio.sleep(0.01)
    assert d.sessions[0].calls == ["on"]


async def test_report_activity_skipped_while_power_op_in_flight(tmp_path):
    cfg = config_mod.Config(devices=[DeviceConfig(id="tv1", host="127.0.0.1")])
    cfg.global_.wake_on_input.enabled = True
    cfg.global_.wake_on_input.cooldown_s = 0.0
    d = make_daemon(tmp_path, cfg=cfg)
    d.sessions = [FakeSession("tv1", reachable=False)]
    d.sessions[0].busy = True
    d._on_report({"activity": True, "key": True})
    await asyncio.sleep(0.05)
    assert d.sessions[0].calls == []


# -- lock / topology error paths ---------------------------------------------------


async def test_on_lock_action_failure_is_logged_not_raised(tmp_path):
    d = make_daemon(tmp_path, on_lock="blank")
    d.sessions = [FakeSession("tv1", fail=("blank",))]
    await d._on_lock(True)                      # must not raise
    assert d.sessions[0].calls == ["blank"]


async def test_topology_change_skips_unmapped_and_survives_failure(tmp_path):
    d = make_daemon(tmp_path)
    d.sessions = [FakeSession("tv1"),           # no display key -> skipped
                  FakeSession("tv2", key="GSM-2", fail=("on",))]
    await d._on_topology_change({"GSM-2"})      # must not raise
    assert d.sessions[0].calls == []
    assert d.sessions[1].calls == ["on"]


def test_save_topology_swallows_oserror(tmp_path):
    d = make_daemon(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a dir")
    d._state_dir = blocker / "sub"
    d._save_topology({"GSM-1"})                 # mkdir fails -> swallowed


async def test_restore_topology_without_saved_state(tmp_path):
    d = make_daemon(tmp_path)
    d.sessions = [FakeSession("tv1", key="GSM-1")]
    await d._restore_topology()
    assert d.sessions[0].calls == []


# -- idle blank/unblank with speaker mute (real DeviceSession + FakeTv) -------------


async def test_idle_blank_and_unblank_toggle_mute(tv, tmp_path):
    d = make_daemon(tmp_path)
    d.cfg.global_.idle.mute_speakers = True
    d.sessions = [make_session(tv, tmp_path)]
    try:
        await d._idle_blank()
        assert tv.power_state == "Screen Off"
        assert ("audio/setMute", {"mute": True}) in tv.requests
        await d._idle_unblank()
        assert tv.power_state == "Active"
        assert ("audio/setMute", {"mute": False}) in tv.requests
    finally:
        await d.sessions[0].disconnect()


async def test_idle_actions_survive_session_failure(tmp_path):
    d = make_daemon(tmp_path)
    d.sessions = [FakeSession("tv1", fail=("blank", "unblank"))]
    await d._idle_blank()                       # must not raise
    await d._idle_unblank()
    assert d.sessions[0].calls == ["blank", "unblank"]


# -- run() lifecycle -----------------------------------------------------------------


async def test_run_lifecycle_serves_ipc_then_stops(tmp_path, monkeypatch):
    async def no_logind():
        return None

    monkeypatch.setattr(daemon_main, "connect_logind_manager", no_logind)
    cfg = config_mod.Config(devices=[DeviceConfig(id="tv1", host="127.0.0.1")])
    cfg.global_.idle.enabled = True
    cfg.global_.topology.enabled = True
    cfg.global_.power_on_at_boot = False
    sock = short_sock()
    d = make_daemon(tmp_path, cfg=cfg)
    d.server.socket_path = sock
    d.sessions = [FakeSession("tv1")]

    task = asyncio.create_task(d.run())
    try:
        import os
        for _ in range(300):
            if os.path.exists(sock):
                break
            await asyncio.sleep(0.01)
        client = ipc.IpcClient(sock)
        await client.connect()
        resp = await asyncio.wait_for(client.request("status"), timeout=5)
        assert resp["ok"] is True
        assert "tv1" in resp["results"]["devices"]
        # close the client BEFORE stopping: 3.12+ wait_closed() waits for it
        await client.close()
        d._stopping.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


# -- main() entry ---------------------------------------------------------------------


def test_main_wires_daemon_from_args(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    config_mod.save(config_mod.Config(
        devices=[DeviceConfig(id="tv1", host="127.0.0.1")]), path)
    built: dict = {}

    class FakeDaemon:
        def __init__(self, cfg, keystore, socket_path, **kw):
            built.update(cfg=cfg, socket_path=socket_path, **kw)

        async def run(self):
            return None

    monkeypatch.setattr(daemon_main, "Daemon", FakeDaemon)
    monkeypatch.setattr(daemon_main.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(config_mod, "user_state_path", lambda: tmp_path / "state")
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(tmp_path / "run"))
    monkeypatch.setattr(daemon_main.sys, "argv",
                        ["lgtvc-daemon", "--config", str(path)])
    daemon_main.main()
    # RUNTIME_DIRECTORY steers the socket when --socket is absent
    assert built["socket_path"] == str(tmp_path / "run" / "ipc.sock")
    assert built["cfg"].devices[0].id == "tv1"


def test_main_without_config_exits(monkeypatch):
    import pytest
    monkeypatch.setattr(config_mod, "find_config", lambda: None)
    monkeypatch.setattr(daemon_main.sys, "argv", ["lgtvc-daemon"])
    with pytest.raises(SystemExit, match="no config found"):
        daemon_main.main()
