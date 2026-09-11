"""lgtvc-agent: session-side eyes for the system daemon.

Runs in the user's graphical session (Plasma desktop or gamescope Game Mode)
where /dev/input is readable via the seat's uaccess ACL — the SELinux-confined
system daemon cannot read it. Reports input activity (and, from v0.2, MPRIS
playback and fullscreen state) to the daemon over the IPC socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time

from .. import ipc
from ..daemon.inputdev import EV_KEY, KEY_PRESS, InputMonitor

log = logging.getLogger("lgtvc-agent")

INPUT_REPORT_INTERVAL = 1.0   # at most one input_event report per second
RECONNECT_DELAY = 5.0


class Agent:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._client: ipc.IpcClient | None = None
        self._last_input_report = 0.0
        self._monitor = InputMonitor(self._on_input)
        self._send_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=16)

    def _on_input(self, path: str, etype: int, code: int, value: int) -> None:
        if etype != EV_KEY or value != KEY_PRESS:
            return
        now = time.monotonic()
        if now - self._last_input_report < INPUT_REPORT_INTERVAL:
            return
        self._last_input_report = now
        with contextlib.suppress(asyncio.QueueFull):
            self._send_queue.put_nowait({"input_event": True})

    async def run(self) -> None:
        self._monitor.start()
        try:
            while True:
                try:
                    self._client = ipc.IpcClient(self.socket_path)
                    await self._client.connect()
                    log.info("connected to daemon at %s", self.socket_path)
                    while True:
                        report = await self._send_queue.get()
                        await self._client.send_report(report)
                except (ConnectionError, OSError) as e:
                    log.debug("daemon connection lost (%s); retrying", e)
                    if self._client is not None:
                        await self._client.close()
                    await asyncio.sleep(RECONNECT_DELAY)
        finally:
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
