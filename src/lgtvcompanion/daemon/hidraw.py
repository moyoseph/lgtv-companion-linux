"""Monitor Valve Steam Controller /dev/hidraw* nodes for input activity.

Reuses `InputMonitor`'s hotplug/fd skeleton (`_HotplugMonitor`) but reads raw HID
reports instead of evdev events, and identifies devices by vendor ID via sysfs
(no ioctl). Each accepted node gets its own offset-agnostic `SteamControllerActivity`
detector; the callback fires (with the node path) on genuine controller input.

This exists because in gamescope the Steam client claims the controller over
hidraw and the kernel stops emitting its evdev events — see steamcontroller.py.
Read-only: we never write to the node, so Steam is undisturbed and hidraw's
lack of an exclusive-grab lets us coexist.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from ..steamcontroller import VALVE_VID, SteamControllerActivity
from .inputdev import _HotplugMonitor

log = logging.getLogger(__name__)

SYSFS_HIDRAW = "/sys/class/hidraw"


def _read_uevent(path: str, sysfs_root: str) -> str:
    name = os.path.basename(path)
    try:
        return Path(sysfs_root, name, "device", "uevent").read_text()
    except OSError:
        return ""


def hidraw_info(path: str,
                sysfs_root: str = SYSFS_HIDRAW) -> tuple[int | None, int | None, str]:
    """(vendor, product, name) for a /dev/hidrawN node from sysfs `uevent`
    (HID_ID=bus:vendor:product, HID_NAME=...). No ioctl needed; missing or
    unparseable fields come back as None / ""."""
    vid: int | None = None
    pid: int | None = None
    name = ""
    for line in _read_uevent(path, sysfs_root).splitlines():
        if line.startswith("HID_ID="):
            parts = line.split("=", 1)[1].split(":")
            if len(parts) == 3:
                try:
                    vid, pid = int(parts[1], 16), int(parts[2], 16)
                except ValueError:
                    vid = pid = None
        elif line.startswith("HID_NAME="):
            name = line.split("=", 1)[1].strip()
    return vid, pid, name


def hidraw_vendor(path: str, sysfs_root: str = SYSFS_HIDRAW) -> int | None:
    """Vendor ID for a /dev/hidrawN node, or None if unreadable/unparseable."""
    return hidraw_info(path, sysfs_root)[0]


class HidrawMonitor(_HotplugMonitor):
    """Watches every readable Valve /dev/hidraw* node and calls back with the
    node path whenever its controller reports genuine input."""

    PATTERN = "/dev/hidraw*"
    # hidraw nodes live directly in /dev (there is no /dev/hidraw directory —
    # watching that path used to fail silently, losing inotify hotplug), so
    # watch /dev and filter events down to hidraw* names.
    WATCH_DIR = b"/dev"
    WATCH_PREFIX = b"hidraw"
    LABEL = "hidraw"
    READ_SIZE = 256           # a single HID report; SC reports are <=64 bytes

    def __init__(self, callback: Callable[[str], None]):
        super().__init__(callback)
        self._detectors: dict[str, SteamControllerActivity] = {}

    def _accept(self, path: str, fd: int) -> bool:
        if hidraw_vendor(path) != VALVE_VID:
            return False
        self._detectors[path] = SteamControllerActivity()
        log.info("watching Steam controller hidraw %s", path)
        return True

    def _close_device(self, path: str) -> None:
        self._detectors.pop(path, None)
        super()._close_device(path)

    def _handle(self, path: str, fd: int, data: bytes) -> None:
        detector = self._detectors.get(path)
        if detector is None:
            return
        now = self._loop.time() if self._loop is not None else 0.0
        if detector.feed(data, now):
            self.callback(path)
