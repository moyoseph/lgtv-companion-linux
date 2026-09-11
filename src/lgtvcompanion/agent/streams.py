"""Sunshine remote-stream detection by tailing sunshine.log.

Runs in the agent: the log lives in the user's home, which the confined
system daemon cannot read. Reports {"streaming": bool} to the daemon, which
owns the reaction policy (blank/off on connect, restore on disconnect).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

POLL_INTERVAL = 2.0
CONNECT_RE = re.compile(r"CLIENT CONNECTED")
DISCONNECT_RE = re.compile(r"CLIENT DISCONNECTED")

DEFAULT_LOG_LOCATIONS = (
    "~/.config/sunshine/sunshine.log",
    "~/.var/app/dev.lizardbyte.app.Sunshine/config/sunshine/sunshine.log",
)


def find_sunshine_log(configured: str = "auto") -> Path | None:
    if configured and configured != "auto":
        p = Path(configured).expanduser()
        return p if p.exists() else None
    for candidate in DEFAULT_LOG_LOCATIONS:
        p = Path(candidate).expanduser()
        if p.exists():
            return p
    return None


class SunshineWatcher:
    """Polls the log for CLIENT CONNECTED/DISCONNECTED lines. Survives log
    truncation/rotation by resetting to the start when the file shrinks."""

    def __init__(self, path: Path, on_change: Callable[[bool], None]):
        self.path = path
        self.on_change = on_change
        self.streaming = False
        self._pos = 0
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        try:
            self._pos = self.path.stat().st_size  # only future events count
        except OSError:
            self._pos = 0
        self._task = asyncio.create_task(self._loop(), name="sunshine-watch")
        log.info("watching %s for stream sessions", self.path)

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _read_new(self) -> str:
        try:
            size = self.path.stat().st_size
        except OSError:
            return ""
        if size < self._pos:
            self._pos = 0  # rotated/truncated
        if size == self._pos:
            return ""
        with open(self.path, errors="replace") as f:
            f.seek(self._pos)
            chunk = f.read()
            self._pos = f.tell()
        return chunk

    def process(self, chunk: str) -> None:
        for line in chunk.splitlines():
            if CONNECT_RE.search(line):
                self._set(True)
            elif DISCONNECT_RE.search(line):
                self._set(False)

    def _set(self, streaming: bool) -> None:
        if streaming != self.streaming:
            self.streaming = streaming
            log.info("sunshine stream %s", "connected" if streaming else "ended")
            self.on_change(streaming)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            chunk = await asyncio.get_running_loop().run_in_executor(
                None, self._read_new)
            if chunk:
                self.process(chunk)
