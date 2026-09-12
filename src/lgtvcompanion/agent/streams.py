"""Sunshine remote-stream detection by tailing sunshine.log.

Runs in the agent: the log lives in the user's home, which the confined
system daemon cannot read. Reports {"streaming": bool} to the daemon, which
owns the reaction policy (blank/off on connect, restore on disconnect).
"""

from __future__ import annotations

import asyncio
import fnmatch
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
    "~/.config/apollo/sunshine.log",   # Apollo (Sunshine fork, same log format)
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


def _running_process_names() -> set[str]:
    """Lowercased process names to match against. Includes both /proc/PID/comm
    (truncated to 15 chars by the kernel) AND the basename of argv[0] from
    /proc/PID/cmdline (untruncated) — otherwise long names like
    "chrome-remote-desktop" would only appear truncated and miss the glob."""
    names: set[str] = set()
    for pid_dir in Path("/proc").glob("[0-9]*"):
        try:
            names.add((pid_dir / "comm").read_text().strip().lower())
        except OSError:
            pass
        try:
            argv0 = (pid_dir / "cmdline").read_bytes().split(b"\0", 1)[0]
            if argv0:
                names.add(argv0.decode(errors="replace").rsplit("/", 1)[-1].lower())
        except OSError:
            continue
    names.discard("")
    return names


class ProcessStreamWatcher:
    """Detects a streaming host by process name — Parsec, Chrome Remote Desktop,
    Apollo, a Moonlight host, etc. (upstream #266/#257). Polls /proc and
    fnmatches configured globs; calls on_change(active) when the aggregate flips.
    """

    def __init__(self, patterns: list[str], on_change: Callable[[bool], None]):
        self.patterns = [p.lower() for p in patterns]
        self.on_change = on_change
        self.active = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="process-stream-watch")
        log.info("watching for streaming processes: %s", ", ".join(self.patterns))

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def poll_once(self, names: set[str]) -> None:
        active = any(fnmatch.fnmatch(n, pat)
                     for n in names for pat in self.patterns)
        if active != self.active:
            self.active = active
            log.info("streaming process %s", "detected" if active else "gone")
            self.on_change(active)

    async def _loop(self) -> None:
        while True:
            names = await asyncio.get_running_loop().run_in_executor(
                None, _running_process_names)
            self.poll_once(names)
            await asyncio.sleep(POLL_INTERVAL)
