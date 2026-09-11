"""lgtvc-agent: session-side eyes for the system daemon.

Runs in the user's graphical session (Plasma desktop or gamescope Game Mode)
where /dev/input is readable via the seat's uaccess ACL — the SELinux-confined
system daemon cannot read it. Reports user activity (filtered: mouse debounce,
stick deadband, ignored keys) and session state (MPRIS playback, fullscreen)
to the daemon over the IPC socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time

from .. import config as config_mod
from .. import ipc
from ..activity import EV_KEY, ActivityFilter, parse_ignored_keys
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


class Agent:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._client: ipc.IpcClient | None = None
        self._last_input_report = 0.0
        self._filter = ActivityFilter(_load_ignored_keys())
        self._monitor = InputMonitor(self._on_input)
        self._send_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=16)
        self._state: dict = {"mpris_playing": False, "fullscreen": None}

    def _on_input(self, path: str, etype: int, code: int, value: int) -> None:
        if not self._filter.is_activity(path, etype, code, value):
            return
        now = time.monotonic()
        if now - self._last_input_report < INPUT_REPORT_INTERVAL:
            return
        self._last_input_report = now
        with contextlib.suppress(asyncio.QueueFull):
            self._send_queue.put_nowait({"activity": True, "key": etype == EV_KEY})

    async def _state_loop(self) -> None:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        from .fullscreen import FullscreenProbe
        from .mpris import mpris_any_playing

        bus = None
        fs_probe: FullscreenProbe | None = None
        try:
            bus = await MessageBus(bus_type=BusType.SESSION).connect()
            fs_probe = FullscreenProbe(bus)
            if not await fs_probe.available():
                log.info("KWin not on the session bus — fullscreen veto disabled")
                fs_probe = None
        except Exception as e:
            log.debug("session bus unavailable (%s); mpris/fullscreen off", e)

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

    def _start_sunshine_watch(self) -> None:
        from .streams import SunshineWatcher, find_sunshine_log
        path = config_mod.find_config()
        configured = "auto"
        if path is not None:
            try:
                configured = config_mod.load(path).global_.remote_stream.sunshine_log
            except (ValueError, OSError):
                pass
        log_path = find_sunshine_log(configured)
        if log_path is None:
            log.info("no sunshine.log found — stream detection off")
            return
        def on_change(streaming: bool) -> None:
            with contextlib.suppress(asyncio.QueueFull):
                self._send_queue.put_nowait({"streaming": streaming})
        self._sunshine = SunshineWatcher(log_path, on_change)
        self._sunshine.start()

    async def run(self) -> None:
        self._monitor.start()
        self._start_sunshine_watch()
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    socket_path = os.environ.get("LGTVC_SOCKET", ipc.DEFAULT_SOCKET)
    if len(sys.argv) > 1 and sys.argv[1] == "--socket":
        socket_path = sys.argv[2]
    try:
        asyncio.run(Agent(socket_path).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
