"""Agent main-loop tests: config-driven ignored keys, input filtering and rate
limiting, the session-state loop, update/stream sub-watchers, and run()'s
daemon connection (retry + report forwarding) against a real IpcServer."""

from __future__ import annotations

import asyncio
import contextlib
import time

from lgtvcompanion import config as config_mod
from lgtvcompanion.activity import EV_KEY, EV_REL
from lgtvcompanion.agent import main as agent_main
from lgtvcompanion.agent import mpris as mpris_mod
from lgtvcompanion.agent import update as update_mod
from lgtvcompanion.agent.main import Agent
from lgtvcompanion.daemon.server import IpcServer

from .harness import short_sock


def _noop():
    return None


async def _poll(cond, timeout=2.0):
    async def _wait():
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_wait(), timeout)


def _agent(monkeypatch, tmp_path, cfg=None) -> Agent:
    """Agent with a hermetic config: the given cfg saved to tmp_path, or no
    config at all (find_config -> None) so the developer machine's real config
    can never leak in."""
    if cfg is None:
        monkeypatch.setattr(config_mod, "find_config", lambda: None)
    else:
        path = tmp_path / "agent-config.json"
        config_mod.save(cfg, path)
        monkeypatch.setattr(config_mod, "find_config", lambda: path)
    a = Agent(str(tmp_path / "sock"))
    # place the rate-limit window firmly in the past without depending on how
    # long the machine has been up (monotonic() starts near 0 on some boxes)
    a._last_input_report = time.monotonic() - 10.0
    return a


# -- _load_ignored_keys -------------------------------------------------------

def test_ignored_keys_loaded_from_config(monkeypatch, tmp_path):
    cfg = config_mod.Config()
    cfg.global_.idle.ignored_keys = ["KEY_MUTE", 42]
    a = _agent(monkeypatch, tmp_path, cfg)
    assert a._filter.ignored_keys == {113, 42}   # KEY_MUTE=113 + raw code


def test_ignored_keys_unreadable_config(monkeypatch, tmp_path):
    bad = tmp_path / "agent-config.json"
    bad.write_text("{ this is not json")
    monkeypatch.setattr(config_mod, "find_config", lambda: bad)
    a = Agent(str(tmp_path / "sock"))            # warns, must not raise
    assert a._filter.ignored_keys == set()


def test_ignored_keys_no_config(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path)
    assert a._filter.ignored_keys == set()


# -- _on_input ----------------------------------------------------------------

def test_key_press_reports_activity(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path)
    a._on_input("/dev/input/event3", EV_KEY, 30, 1)
    assert a._send_queue.get_nowait() == {"activity": True, "key": True}
    assert a._send_queue.empty()


def test_key_press_rate_limited(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path)
    a._on_input("/dev/input/event3", EV_KEY, 30, 1)
    assert a._send_queue.get_nowait() == {"activity": True, "key": True}
    a._on_input("/dev/input/event3", EV_KEY, 31, 1)   # within the 1 s window
    assert a._send_queue.empty()


def test_release_and_ignored_key_do_not_report(monkeypatch, tmp_path):
    cfg = config_mod.Config()
    cfg.global_.idle.ignored_keys = ["KEY_MUTE"]
    a = _agent(monkeypatch, tmp_path, cfg)
    a._on_input("/dev/input/event3", EV_KEY, 30, 0)    # release, not press
    a._on_input("/dev/input/event3", EV_KEY, 113, 1)   # ignored KEY_MUTE press
    assert a._send_queue.empty()


def test_mouse_motion_debounced_then_reports(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path)
    a._on_input("/dev/input/event5", EV_REL, 0, 1)
    a._on_input("/dev/input/event5", EV_REL, 0, 1)
    assert a._send_queue.empty()                       # < 3 samples in window
    a._on_input("/dev/input/event5", EV_REL, 0, 1)
    assert a._send_queue.get_nowait() == {"activity": True, "key": False}


# -- _on_sc_input (Steam Controller via hidraw) -------------------------------

def test_sc_input_reports_unblank_only(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path)
    a._on_sc_input("/dev/hidraw0")
    assert a._send_queue.get_nowait() == {"activity": True, "key": False}
    assert a._send_queue.empty()


def test_sc_input_rate_limited(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path)
    a._on_sc_input("/dev/hidraw0")
    assert a._send_queue.get_nowait() == {"activity": True, "key": False}
    a._on_sc_input("/dev/hidraw0")                     # within the 1 s window
    assert a._send_queue.empty()


def test_steam_controller_enabled_by_default(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path)                  # no config -> default on
    assert a._sc_monitor is not None


def test_steam_controller_can_be_disabled(monkeypatch, tmp_path):
    cfg = config_mod.Config()
    cfg.global_.steam_controller.enabled = False
    a = _agent(monkeypatch, tmp_path, cfg)
    assert a._sc_monitor is None


# -- _state_loop --------------------------------------------------------------

async def test_state_loop_reports_then_dedupes(monkeypatch, tmp_path):
    # session bus naturally unavailable -> covers the except branch
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent")
    monkeypatch.setattr(agent_main, "STATE_REPORT_INTERVAL", 0.01)

    async def playing(bus=None):
        return True

    # _state_loop does `from .mpris import mpris_any_playing` at call time
    monkeypatch.setattr(mpris_mod, "mpris_any_playing", playing)
    a = _agent(monkeypatch, tmp_path)
    task = asyncio.create_task(a._state_loop())
    try:
        frame = await asyncio.wait_for(a._send_queue.get(), 2.0)
        assert frame == {"mpris_playing": True, "fullscreen": None}
        await asyncio.sleep(0.1)             # ~10 further ticks, all identical
        assert a._send_queue.empty()         # deduped: no repeat frames
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2.0)


async def test_state_loop_survives_mpris_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent")
    monkeypatch.setattr(agent_main, "STATE_REPORT_INTERVAL", 0.01)
    calls = []

    async def flaky(bus=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("mpris probe blew up")
        return True

    monkeypatch.setattr(mpris_mod, "mpris_any_playing", flaky)
    a = _agent(monkeypatch, tmp_path)
    task = asyncio.create_task(a._state_loop())
    try:
        frame = await asyncio.wait_for(a._send_queue.get(), 2.0)
        assert frame == {"mpris_playing": True, "fullscreen": None}
        assert len(calls) >= 2               # first call raised, loop kept going
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2.0)


# -- _on_lock_change ----------------------------------------------------------

def test_lock_change_queues_report(monkeypatch, tmp_path):
    a = _agent(monkeypatch, tmp_path)
    a._on_lock_change(True)
    assert a._send_queue.get_nowait() == {"locked": True}


# -- _start_update_check ------------------------------------------------------

def test_update_check_off_creates_no_updater(monkeypatch, tmp_path):
    cfg = config_mod.Config()
    cfg.global_.update_check = "off"
    a = _agent(monkeypatch, tmp_path, cfg)
    a._start_update_check(None)
    assert not hasattr(a, "_updater")


async def test_update_check_notify_starts_updater(monkeypatch, tmp_path):
    monkeypatch.setattr(update_mod, "_fetch_latest_tag", _noop)  # no network
    a = _agent(monkeypatch, tmp_path)        # no config -> default "notify"
    a._start_update_check(None)
    task = a._updater._task
    try:
        assert task is not None
        await asyncio.sleep(0.05)            # first (patched) fetch runs
        assert not task.done()               # parked on the daily sleep
    finally:
        a._updater.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2.0)


# -- _start_stream_watch ------------------------------------------------------

async def test_stream_watch_starts_sunshine_and_process_watchers(monkeypatch, tmp_path):
    log_file = tmp_path / "sunshine.log"
    log_file.write_text("startup\n")
    cfg = config_mod.Config()
    cfg.global_.remote_stream.enabled = True
    cfg.global_.remote_stream.sunshine_log = str(log_file)
    cfg.global_.remote_stream.processes = ["parsec*"]
    a = _agent(monkeypatch, tmp_path, cfg)
    a._start_stream_watch()
    try:
        assert a._sunshine.path == log_file
        assert a._sunshine._task is not None
        assert a._proc_stream.patterns == ["parsec*"]
        assert a._proc_stream._task is not None
    finally:
        for w in (getattr(a, "_sunshine", None), getattr(a, "_proc_stream", None)):
            if w is None:
                continue
            task = w._task
            w.stop()
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2.0)


def test_stream_watch_without_log_or_processes(monkeypatch, tmp_path):
    cfg = config_mod.Config()
    cfg.global_.remote_stream.enabled = True
    cfg.global_.remote_stream.sunshine_log = str(tmp_path / "missing.log")
    a = _agent(monkeypatch, tmp_path, cfg)
    a._start_stream_watch()
    assert not hasattr(a, "_sunshine")
    assert not hasattr(a, "_proc_stream")


# -- run() --------------------------------------------------------------------

async def test_run_retries_then_forwards_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_main, "RECONNECT_DELAY", 0.05)
    sock = short_sock()
    reports: list[dict] = []

    async def dispatcher(cmd, args, devices):
        return {}

    server = IpcServer(sock, dispatcher, on_report=reports.append)
    a = _agent(monkeypatch, tmp_path)
    a.socket_path = sock
    # /dev/input and the session bus are process boundaries; each piece is
    # exercised by its own test above
    a._monitor.start = _noop
    a._monitor.stop = _noop
    a._sc_monitor.start = _noop
    a._sc_monitor.stop = _noop
    a._start_stream_watch = _noop

    async def no_state_loop():
        return None

    a._state_loop = no_state_loop

    agent_task = asyncio.create_task(a.run())
    try:
        await asyncio.sleep(0.12)            # no server yet: connect fails, retries
        assert reports == []
        await server.start()
        await _poll(lambda: len(reports) >= 1)
        assert reports[0] == {"mpris_playing": False, "fullscreen": None}
        a._send_queue.put_nowait({"activity": True, "key": True})
        await _poll(lambda: {"activity": True, "key": True} in reports)
    finally:
        agent_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(agent_task, 2.0)
        if a._client is not None:            # close before server.stop(): py3.12+
            await a._client.close()          # wait_closed waits for open conns
        await asyncio.wait_for(server.stop(), 2.0)


# -- config-load-except + main() entry ------------------------------------------

async def test_update_check_config_load_error_defaults_notify(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json")
    monkeypatch.setattr(config_mod, "find_config", lambda: path)
    a = Agent(str(tmp_path / "sock"))
    a._start_update_check(None)               # load raises -> mode stays "notify"
    try:
        assert a._updater is not None
    finally:
        a._updater.stop()


def test_stream_watch_config_load_error(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json")
    monkeypatch.setattr(config_mod, "find_config", lambda: path)
    monkeypatch.setattr(config_mod, "user_config_path", lambda: path)
    a = Agent(str(tmp_path / "sock"))
    # no sunshine log + rs is None -> both watchers skipped, no raise
    a._start_stream_watch()
    if getattr(a, "_sunshine", None):
        a._sunshine.stop()
    if getattr(a, "_proc_stream", None):
        a._proc_stream.stop()


def test_agent_main_entry(monkeypatch, tmp_path):
    ran = {}

    async def fake_run(self):
        ran["socket"] = self.socket_path

    monkeypatch.setattr(agent_main.Agent, "run", fake_run)
    monkeypatch.setattr(agent_main.sys, "argv",
                        ["lgtvc-agent", "--socket", str(tmp_path / "s.sock")])
    agent_main.main()
    assert ran["socket"] == str(tmp_path / "s.sock")


def test_agent_main_swallows_keyboard_interrupt(monkeypatch):
    async def fake_run(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_main.Agent, "run", fake_run)
    monkeypatch.setattr(agent_main.sys, "argv", ["lgtvc-agent"])
    agent_main.main()                          # returns cleanly


def test_offline_mode_creates_no_updater(monkeypatch, tmp_path):
    cfg = config_mod.Config()
    cfg.global_.update_check = "notify"      # would normally run...
    cfg.global_.offline_mode = True          # ...but offline_mode wins
    a = _agent(monkeypatch, tmp_path, cfg)
    a._start_update_check(None)
    assert not hasattr(a, "_updater")
