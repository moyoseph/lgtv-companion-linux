"""Pure-Python /dev/input event monitoring (no python-evdev dependency —
it ships sdist-only and needs kernel headers, a bad fit for immutable OSes).

input_event is struct { timeval, __u16 type, __u16 code, __s32 value } =
'llHHi' (24 bytes on 64-bit). Hotplug is watched with inotify on /dev/input,
falling back to periodic rescans if inotify is unavailable.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import glob
import logging
import os
import struct
from collections.abc import Callable

log = logging.getLogger(__name__)

EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

EV_KEY = 0x01
KEY_PRESS = 1

IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
RESCAN_FALLBACK_INTERVAL = 45.0


class InputMonitor:
    """Watches every readable /dev/input/event* and invokes the callback with
    (device_path, type, code, value) for each event."""

    def __init__(self, callback: Callable[[str, int, int, int], None]):
        self.callback = callback
        self._fds: dict[str, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inotify_fd: int | None = None
        self._rescan_task: asyncio.Task | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._scan()
        if not self._setup_inotify():
            self._rescan_task = asyncio.get_running_loop().create_task(
                self._rescan_loop())
        log.info("input monitor: %d device(s) open", len(self._fds))

    def stop(self) -> None:
        for path in list(self._fds):
            self._close_device(path)
        if self._inotify_fd is not None and self._loop is not None:
            self._loop.remove_reader(self._inotify_fd)
            os.close(self._inotify_fd)
            self._inotify_fd = None
        if self._rescan_task is not None:
            self._rescan_task.cancel()

    # -- device management ----------------------------------------------------

    def _scan(self) -> None:
        present = set(glob.glob("/dev/input/event*"))
        errors: list[str] = []
        for path in present - self._fds.keys():
            self._open_device(path, errors)
        for path in self._fds.keys() - present:
            self._close_device(path)
        if not self._fds and errors:
            log.warning("no input devices readable: %s", "; ".join(errors[:4]))

    def _open_device(self, path: str, errors: list[str] | None = None) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            log.debug("%s: cannot open (%s)", path, e)
            if errors is not None:
                errors.append(f"{path}: {e}")
            return
        self._fds[path] = fd
        assert self._loop is not None
        self._loop.add_reader(fd, self._on_readable, path, fd)

    def _close_device(self, path: str) -> None:
        fd = self._fds.pop(path, None)
        if fd is None:
            return
        if self._loop is not None:
            self._loop.remove_reader(fd)
        try:
            os.close(fd)
        except OSError:
            pass

    def _on_readable(self, path: str, fd: int) -> None:
        try:
            data = os.read(fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return
        except OSError:
            self._close_device(path)
            return
        for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _sec, _usec, etype, code, value = struct.unpack_from(
                EVENT_FORMAT, data, off)
            self.callback(path, etype, code, value)

    # -- hotplug ---------------------------------------------------------------

    def _setup_inotify(self) -> bool:
        libc_name = ctypes.util.find_library("c")
        if not libc_name:
            return False
        try:
            libc = ctypes.CDLL(libc_name, use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK)
            if fd < 0:
                return False
            wd = libc.inotify_add_watch(fd, b"/dev/input", IN_CREATE | IN_DELETE)
            if wd < 0:
                os.close(fd)
                return False
        except (OSError, AttributeError):
            return False
        self._inotify_fd = fd
        assert self._loop is not None
        self._loop.add_reader(fd, self._on_inotify)
        return True

    def _on_inotify(self) -> None:
        assert self._inotify_fd is not None
        try:
            os.read(self._inotify_fd, 4096)
        except (BlockingIOError, OSError):
            return
        # Small delay lets udev finish applying permissions/ACLs first.
        assert self._loop is not None
        self._loop.call_later(1.0, self._scan)

    async def _rescan_loop(self) -> None:
        while True:
            await asyncio.sleep(RESCAN_FALLBACK_INTERVAL)
            self._scan()
