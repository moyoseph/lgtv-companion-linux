"""Pure-Python device monitoring for /dev/input and /dev/hidraw (no python-evdev
dependency — it ships sdist-only and needs kernel headers, a bad fit for
immutable OSes).

`_HotplugMonitor` is the shared skeleton: glob a device class, open each node
non-blocking, pump it through the event loop, and track hotplug with inotify
(falling back to periodic rescans). `InputMonitor` reads evdev events
(struct { timeval, __u16 type, __u16 code, __s32 value } = 'llHHi', 24 bytes on
64-bit); `HidrawMonitor` (in hidraw.py) reads raw HID reports. Both reuse the
skeleton by overriding `_accept()` (should we watch this node?) and `_handle()`
(what to do with the bytes read).
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

INPUT_PROP_ACCELEROMETER = 0x06


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("E") << 8) | nr


def is_accelerometer(fd: int) -> bool:
    """Gyro/accel devices stream constantly and must never count as user
    activity (upstream excludes them the same way)."""
    import fcntl
    buf = bytearray(8)
    try:
        fcntl.ioctl(fd, _ioc(2, 0x09, len(buf)), buf)  # EVIOCGPROP
    except OSError:
        return False
    return bool(buf[INPUT_PROP_ACCELEROMETER // 8] & (1 << (INPUT_PROP_ACCELEROMETER % 8)))


class _HotplugMonitor:
    """Watches every readable node matching PATTERN and pumps it through the
    event loop, with inotify hotplug on WATCH_DIR. Subclasses set the class
    attributes and override `_accept`/`_handle`."""

    PATTERN = ""            # glob, e.g. "/dev/input/event*"
    WATCH_DIR = b""         # inotify dir, e.g. b"/dev/input"
    LABEL = "device"        # used in log messages
    READ_SIZE = 4096        # bytes per os.read

    def __init__(self, callback: Callable) -> None:
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
        log.info("%s monitor: %d device(s) open", self.LABEL, len(self._fds))

    def stop(self) -> None:
        for path in list(self._fds):
            self._close_device(path)
        if self._inotify_fd is not None and self._loop is not None:
            self._loop.remove_reader(self._inotify_fd)
            os.close(self._inotify_fd)
            self._inotify_fd = None
        if self._rescan_task is not None:
            self._rescan_task.cancel()

    # -- subclass hooks -------------------------------------------------------

    def _accept(self, path: str, fd: int) -> bool:
        """Whether to keep watching this freshly-opened node."""
        return True

    def _handle(self, path: str, fd: int, data: bytes) -> None:
        """Process bytes read from a node (invoke self.callback as appropriate)."""
        raise NotImplementedError

    # -- device management ----------------------------------------------------

    def _scan(self) -> None:
        present = set(glob.glob(self.PATTERN))
        errors: list[str] = []
        for path in present - self._fds.keys():
            self._open_device(path, errors)
        for path in self._fds.keys() - present:
            self._close_device(path)
        if not self._fds and errors:
            log.warning("no %s devices readable: %s",
                        self.LABEL, "; ".join(errors[:4]))

    def _open_device(self, path: str, errors: list[str] | None = None) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            log.debug("%s: cannot open (%s)", path, e)
            if errors is not None:
                errors.append(f"{path}: {e}")
            return
        if not self._accept(path, fd):
            os.close(fd)
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
            data = os.read(fd, self.READ_SIZE)
        except BlockingIOError:
            return
        except OSError:
            self._close_device(path)
            return
        self._handle(path, fd, data)

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
            wd = libc.inotify_add_watch(fd, self.WATCH_DIR, IN_CREATE | IN_DELETE)
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


class InputMonitor(_HotplugMonitor):
    """Watches every readable /dev/input/event* and invokes the callback with
    (device_path, type, code, value) for each event."""

    PATTERN = "/dev/input/event*"
    WATCH_DIR = b"/dev/input"
    LABEL = "input"
    READ_SIZE = EVENT_SIZE * 64

    def __init__(self, callback: Callable[[str, int, int, int], None]):
        super().__init__(callback)

    def _accept(self, path: str, fd: int) -> bool:
        if is_accelerometer(fd):
            log.debug("%s: accelerometer, skipped", path)
            return False
        return True

    def _handle(self, path: str, fd: int, data: bytes) -> None:
        for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _sec, _usec, etype, code, value = struct.unpack_from(
                EVENT_FORMAT, data, off)
            self.callback(path, etype, code, value)
