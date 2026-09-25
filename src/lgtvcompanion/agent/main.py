"""lgtvc-agent: session-side eyes for the system daemon.

Runs in the user's graphical session (Plasma desktop or gamescope Game Mode)
where /dev/input is readable via the seat's uaccess ACL — the SELinux-confined
system daemon cannot read it. Reports user activity (filtered: mouse debounce,
stick deadband, ignored keys) and session state (MPRIS playback, fullscreen)
to the daemon over the IPC socket. In gamescope the Steam client claims the
controller over hidraw (no evdev events), so a HidrawMonitor reads it directly
so controller-only use still keeps the TV awake.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time

from .. import config as config_mod
from .. import ipc
from ..activity import EV_KEY, ActivityFilter, parse_ignored_keys
from ..daemon.hidraw import HidrawMonitor
from ..daemon.inputdev import InputMonitor

log = logging.getLogger("lgtvc-agent")

INPUT_REPORT_INTERVAL = 1.0   # at most one activity report per second
STATE_REPORT_INTERVAL = 10.0  # periodic session-state (mpris/fullscreen) report
RECONNECT_DELAY = 5.0


def _load_ignored_keys() -> set[int]:
    path = config_mod.find_config()
    if path is None:
        return set()
    try:
        cfg = config_mod.load(path)
    except (ValueError, OSError) as e:
        log.warning("config unreadable (%s); no ignored keys", e)
        return set()
    return parse_ignored_keys(cfg.global_.idle.ignored_keys)


def _steam_controller_config() -> config_mod.SteamControllerConfig:
    """Best-effort load; dataclass defaults (enabled + wake) when the config is
    missing or unreadable."""
    path = config_mod.find_config()
    if path is not None:
        try:
            return config_mod.load(path).global_.steam_controller
        except (ValueError, OSError):
            pass
    return config_mod.SteamControllerConfig()


class Agent:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._client: ipc.IpcClient | None = None
        self._last_input_report = 0.0
        self._last_sc_report = 0.0
        self._filter = ActivityFilter(_load_ignored_keys())
        self._monitor = InputMonitor(self._on_input)
        # In gamescope the Steam client claims the controller over hidraw, so it
        # emits no evdev events; read it directly to keep the TV awake there.
        sc = _steam_controller_config()
        self._sc_monitor = HidrawMonitor(self._on_sc_input) if sc.enabled else None
        self._sc_wake = sc.wake
        self._send_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=16)
        self._state: dict = {"mpris_playing": False, "fullscreen": None}
        # streaming is the OR of all sources (sunshine log + process watch); the
        # daemon consumes a single bool, so we only report the combined change.
        self._stream_sources: dict[str, bool] = {}
        self._stream_combined = False

    def _on_input(self, path: str, etype: int, code: int, value: int) -> None:
        if not self._filter.is_activity(path, etype, code, value):
            return
        now = time.monotonic()
        if now - self._last_input_report < INPUT_REPORT_INTERVAL:
            return
        self._last_input_report = now
        with contextlib.suppress(asyncio.QueueFull):
            self._send_queue.put_nowait({"activity": True, "key": etype == EV_KEY})

    def _on_sc_input(self, path: str) -> None:
        # Steam-controller input over hidraw. With steam_controller.wake (the
        # default) it counts as a key press so the daemon may power on a fully
        # off TV; wake=false keeps it unblank-only. Throttled like _on_input.
        now = time.monotonic()
        if now - self._last_sc_report < INPUT_REPORT_INTERVAL:
            return
        self._last_sc_report = now
        with contextlib.suppress(asyncio.QueueFull):
            self._send_queue.put_nowait({"activity": True, "key": self._sc_wake})

    async def _state_loop(self) -> None:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        from .fullscreen import FullscreenProbe
        from .lock import ScreenLockWatcher
        from .mpris import mpris_any_playing

        bus = None
        fs_probe: FullscreenProbe | None = None
        try:
            bus = await MessageBus(bus_type=BusType.SESSION).connect()
            fs_probe = FullscreenProbe(bus)
            if not await fs_probe.available():
                log.info("KWin not on the session bus — fullscreen veto disabled")
                fs_probe = None
            lock_watcher = ScreenLockWatcher(bus, self._on_lock_change)
            if not (await lock_watcher.available() and await lock_watcher.start()):
                log.info("no screensaver on the session bus — lock detection off")
            self._start_update_check(bus)
        except Exception as e:
            log.debug("session bus unavailable (%s); mpris/fullscreen/lock off", e)

        while True:
            await asyncio.sleep(STATE_REPORT_INTERVAL)
            try:
                playing = await mpris_any_playing(bus)
            except Exception as e:
                log.debug("mpris probe failed: %s", e)
                playing = False
            fullscreen = None
            if fs_probe is not None:
                fullscreen = await fs_probe.probe()
            state = {"mpris_playing": playing, "fullscreen": fullscreen}
            if state != self._state:
                self._state = state
                with contextlib.suppress(asyncio.QueueFull):
                    self._send_queue.put_nowait(dict(state))

    def _on_lock_change(self, locked: bool) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._send_queue.put_nowait({"locked": locked})

    def _start_update_check(self, bus) -> None:
        path = config_mod.find_config()
        mode, offline = "notify", False
        if path is not None:
            try:
                g = config_mod.load(path).global_
                mode, offline = g.update_check, g.offline_mode
            except (ValueError, OSError):
                pass
        if offline or mode == "off":   # offline_mode never phones GitHub
            return
        from .update import UpdateChecker
        self._updater = UpdateChecker(bus)
        self._updater.start()

    def _set_stream_source(self, name: str, active: bool) -> None:
        self._stream_sources[name] = active
        combined = any(self._stream_sources.values())
        if combined != self._stream_combined:
            self._stream_combined = combined
            with contextlib.suppress(asyncio.QueueFull):
                self._send_queue.put_nowait({"streaming": combined})

    def _start_stream_watch(self) -> None:
        from .streams import (
            ProcessStreamWatcher,
            SunshineWatcher,
            find_sunshine_log,
        )
        rs = None
        path = config_mod.find_config()
        if path is not None:
            try:
                rs = config_mod.load(path).global_.remote_stream
            except (ValueError, OSError):
                pass
        log_path = find_sunshine_log(rs.sunshine_log if rs else "auto")
        if log_path is not None:
            self._sunshine = SunshineWatcher(
                log_path, lambda s: self._set_stream_source("sunshine", s))
            self._sunshine.start()
        else:
            log.info("no sunshine.log found — sunshine detection off")
        if rs and rs.processes:
            self._proc_stream = ProcessStreamWatcher(
                rs.processes, lambda s: self._set_stream_source("process", s))
            self._proc_stream.start()

    async def run(self) -> None:
        self._monitor.start()
        if self._sc_monitor is not None:
            self._sc_monitor.start()
        self._start_stream_watch()
        state_task = asyncio.create_task(self._state_loop())
        try:
            while True:
                try:
                    self._client = ipc.IpcClient(self.socket_path)
                    await self._client.connect()
                    log.info("connected to daemon at %s", self.socket_path)
                    await self._client.send_report(dict(self._state))
                    while True:
                        report = await self._send_queue.get()
                        await self._client.send_report(report)
                except (ConnectionError, OSError) as e:
                    log.debug("daemon connection lost (%s); retrying", e)
                    if self._client is not None:
                        await self._client.close()
                    await asyncio.sleep(RECONNECT_DELAY)
        finally:
            state_task.cancel()
            self._monitor.stop()
            if self._sc_monitor is not None:
                self._sc_monitor.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    socket_path = ipc.default_socket()
    if len(sys.argv) > 1 and sys.argv[1] == "--socket":
        socket_path = sys.argv[2]
    try:
        asyncio.run(Agent(socket_path).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
