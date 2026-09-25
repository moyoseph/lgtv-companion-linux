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

IN_ATTRIB = 0x00000004
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_Q_OVERFLOW = 0x00004000
# struct inotify_event header: wd, mask, cookie, len — then `len` bytes of
# NUL-padded name.
_INOTIFY_HEADER = struct.Struct("iIII")
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
    WATCH_PREFIX = b""      # only inotify names with this prefix trigger a
                            # rescan (empty = all); needed when WATCH_DIR is a
                            # busy directory like /dev
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
        self._setup_inotify()
        # Always keep the periodic rescan as well: inotify events can be lost
        # around suspend/resume, and _scan() also revalidates open fds against
        # re-created same-name nodes (_is_stale).
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
        # A node deleted and re-created under the same name (USB re-enumeration
        # on resume) leaves our fd pointing at the dead predecessor — reopen it.
        for path in present & self._fds.keys():
            if self._is_stale(path):
                self._close_device(path)
                self._open_device(path, errors)
        for path in present - self._fds.keys():
            self._open_device(path, errors)
        for path in self._fds.keys() - present:
            self._close_device(path)
        if not self._fds and errors:
            log.warning("no %s devices readable: %s",
                        self.LABEL, "; ".join(errors[:4]))

    def _is_stale(self, path: str) -> bool:
        try:
            st, fst = os.stat(path), os.fstat(self._fds[path])
        except OSError:
            return True
        return (st.st_ino, st.st_rdev) != (fst.st_ino, fst.st_rdev)

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
        if not data:   # EOF: device gone (re-enumerated); free the name for _scan
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
            # IN_ATTRIB: udev applies the uaccess ACL after creating the node,
            # so a create-time open can fail EACCES — the chmod event retries it.
            wd = libc.inotify_add_watch(fd, self.WATCH_DIR,
                                        IN_CREATE | IN_DELETE | IN_ATTRIB)
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
            data = os.read(self._inotify_fd, 4096)
        except (BlockingIOError, OSError):
            return
        # Only rescan when an event names one of our nodes (WATCH_PREFIX) — on a
        # busy WATCH_DIR like /dev, unrelated churn (ptys…) must not cause scans.
        # A queue overflow means events were dropped: rescan unconditionally.
        relevant, off = False, 0
        while off + _INOTIFY_HEADER.size <= len(data):
            _wd, mask, _cookie, nlen = _INOTIFY_HEADER.unpack_from(data, off)
            name = data[off + _INOTIFY_HEADER.size:
                        off + _INOTIFY_HEADER.size + nlen].rstrip(b"\0")
            off += _INOTIFY_HEADER.size + nlen
            if (mask & IN_Q_OVERFLOW or not self.WATCH_PREFIX
                    or name.startswith(self.WATCH_PREFIX)):
                relevant = True
        if not relevant:
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
    WATCH_PREFIX = b"event"
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
