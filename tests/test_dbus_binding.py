"""Real D-Bus signal-binding tests: PowerEvents.start / connect_logind_manager
(logind, system bus) and ScreenLockWatcher.start (ScreenSaver, session bus).

Linux-only and require a working session bus — the coverage CI job runs the
suite under `dbus-run-session`. Everywhere else (macOS dev, the lean matrix
jobs) these skip cleanly.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="D-Bus binding is Linux-only")


async def _connect_session():
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus
    return await MessageBus(bus_type=BusType.SESSION).connect()


@pytest.fixture
async def dbus_env(monkeypatch):
    """Skip unless a real session bus is reachable; point the SYSTEM bus at it
    too so BusType.SYSTEM code lands on the same daemon."""
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not addr:
        pytest.skip("no session bus (run under dbus-run-session)")
    try:
        probe = await _connect_session()
    except Exception as e:
        pytest.skip(f"cannot connect to session bus: {e}")
    probe.disconnect()
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", addr)
    yield addr


async def _poll(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "signal not received before timeout"
        await asyncio.sleep(0.01)


# -- PowerEvents.start (logind, system bus) ------------------------------------


async def test_power_events_start_receives_sleep_signals(dbus_env, monkeypatch):
    from lgtvcompanion.daemon import power_events

    from .dbus_mocks import LOGIND, LOGIND_PATH, Login1ManagerMock

    monkeypatch.setattr(power_events.shutil, "which", lambda n: None)  # no inhibitor
    server = await _connect_session()
    mock = Login1ManagerMock()
    server.export(LOGIND_PATH, mock)
    await server.request_name(LOGIND)

    events: list[str] = []

    def cb(name):
        async def run():
            events.append(name)
        return run

    pe = power_events.PowerEvents(
        on_suspend=cb("suspend"), on_resume=cb("resume"),
        on_shutdown=cb("shutdown"), on_reboot=cb("reboot"))
    try:
        await pe.start()                        # real introspect + subscribe
        mock.prepare_for_sleep(True)            # emit PrepareForSleep(true)
        await _poll(lambda: "suspend" in events)
        mock.prepare_for_sleep(False)           # -> resume
        await _poll(lambda: "resume" in events)
    finally:
        await pe.stop()
        server.disconnect()


async def test_power_events_shutdown_metadata_routes_reboot(dbus_env, monkeypatch):
    from dbus_fast import Variant

    from lgtvcompanion.daemon import power_events

    from .dbus_mocks import LOGIND, LOGIND_PATH, Login1ManagerMock

    monkeypatch.setattr(power_events.shutil, "which", lambda n: None)
    server = await _connect_session()
    mock = Login1ManagerMock()
    server.export(LOGIND_PATH, mock)
    await server.request_name(LOGIND)

    events: list[str] = []

    def cb(name):
        async def run():
            events.append(name)
        return run

    pe = power_events.PowerEvents(
        on_suspend=cb("suspend"), on_resume=cb("resume"),
        on_shutdown=cb("shutdown"), on_reboot=cb("reboot"))
    try:
        await pe.start()
        mock.prepare_for_shutdown_with_metadata(True, {"type": Variant("s", "reboot")})
        await _poll(lambda: "reboot" in events)
    finally:
        await pe.stop()
        server.disconnect()


async def test_connect_logind_manager_returns_working_proxy(dbus_env):
    from lgtvcompanion.daemon import power_events

    from .dbus_mocks import LOGIND, LOGIND_PATH, Login1ManagerMock

    server = await _connect_session()
    server.export(LOGIND_PATH, Login1ManagerMock())
    await server.request_name(LOGIND)
    try:
        mgr = await power_events.connect_logind_manager()
        assert mgr is not None
        assert await mgr.call_list_inhibitors() == []
    finally:
        server.disconnect()


# -- ScreenLockWatcher.start (ScreenSaver, session bus) ------------------------


async def test_screen_lock_watcher_binds_and_receives(dbus_env):
    from lgtvcompanion.agent.lock import ScreenLockWatcher

    from .dbus_mocks import SCREENSAVER, ScreenSaverMock

    server = await _connect_session()
    mock = ScreenSaverMock()
    server.export("/org/freedesktop/ScreenSaver", mock)
    await server.request_name(SCREENSAVER)

    client = await _connect_session()
    changes: list[bool] = []
    watcher = ScreenLockWatcher(client, changes.append)
    try:
        assert await watcher.available() is True
        assert await watcher.start() is True     # real introspect + subscribe
        mock.active_changed(True)
        mock.active_changed(False)
        await _poll(lambda: changes == [True, False])
    finally:
        client.disconnect()
        server.disconnect()


async def test_screen_lock_watcher_unavailable_without_service(dbus_env):
    from lgtvcompanion.agent.lock import ScreenLockWatcher
    client = await _connect_session()
    try:
        watcher = ScreenLockWatcher(client, lambda b: None)
        assert await watcher.available() is False   # nobody owns the name
        assert await watcher.start() is False       # both paths fail
    finally:
        client.disconnect()


# -- agent _state_loop live-bus wiring -----------------------------------------


async def test_agent_state_loop_wires_probes_on_real_bus(dbus_env, monkeypatch, tmp_path):
    # On a real session bus with no KWin/ScreenSaver present, _state_loop's
    # available() checks return False (probe/watcher become None) — exercising
    # the live wiring at 78-85 without those services.
    from lgtvcompanion.agent import main as agent_main
    from lgtvcompanion.agent import mpris as mpris_mod

    monkeypatch.setattr(agent_main, "STATE_REPORT_INTERVAL", 0.02)

    async def no_mpris(bus=None):
        return False

    monkeypatch.setattr(mpris_mod, "mpris_any_playing", no_mpris)
    a = agent_main.Agent(str(tmp_path / "sock"))
    task = asyncio.create_task(a._state_loop())
    try:
        # let it connect the session bus + probe availability at least once
        await asyncio.sleep(0.2)
        assert not task.done()                   # survived the live wiring
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
