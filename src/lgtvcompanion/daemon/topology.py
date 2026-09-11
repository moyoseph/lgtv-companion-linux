"""Display topology via DRM sysfs + EDID.

Linux replacement for upstream's Windows display-path matching: each
connected connector's EDID yields a stable key (manufacturer + product +
serial). Devices whose `unique_display_key` is present in the current
topology are powered on; configured-but-absent devices are powered off.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)

DRM_PATH = Path("/sys/class/drm")
POLL_INTERVAL = 15.0


def parse_edid_key(edid: bytes) -> str | None:
    """Stable identity: 3-letter PnP id + product code + serial.

    Prefers the descriptor-block serial string (tag 0xFF) over the numeric
    serial, matching how vendors distinguish units.
    """
    if len(edid) < 128 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    mfr_raw = struct.unpack(">H", edid[8:10])[0]
    mfr = "".join(chr(((mfr_raw >> shift) & 0x1F) + ord("A") - 1)
                  for shift in (10, 5, 0))
    product = struct.unpack("<H", edid[10:12])[0]
    serial = struct.unpack("<I", edid[12:16])[0]
    serial_str = ""
    for off in (54, 72, 90, 108):
        block = edid[off:off + 18]
        if len(block) == 18 and block[0:3] == b"\x00\x00\x00" and block[3] == 0xFF:
            # EDID strings are 0x0A-terminated and space/NUL padded
            serial_str = (block[5:18].decode(errors="replace")
                          .split("\n")[0].strip(" \x00\t"))
            break
    return f"{mfr}-{product:04x}-{serial_str or serial}"


def connected_displays() -> dict[str, str]:
    """{connector_name: edid_key} for every connected connector with an EDID."""
    out: dict[str, str] = {}
    for status_file in DRM_PATH.glob("card*-*/status"):
        try:
            if status_file.read_text().strip() != "connected":
                continue
            edid = (status_file.parent / "edid").read_bytes()
        except OSError:
            continue
        key = parse_edid_key(edid)
        if key:
            out[status_file.parent.name] = key
    return out


class TopologyWatcher:
    """Polls the connector set; calls on_change(present_keys) when it moves."""

    def __init__(self, on_change: Callable[[set[str]], Awaitable[None]]):
        self.on_change = on_change
        self._last: set[str] | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="topology-watch")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            keys = set((await asyncio.get_running_loop().run_in_executor(
                None, connected_displays)).values())
            if self._last is not None and keys != self._last:
                log.info("display topology changed: %s", sorted(keys) or "(none)")
                try:
                    await self.on_change(keys)
                except Exception:
                    log.exception("topology change handler failed")
            self._last = keys
            await asyncio.sleep(POLL_INTERVAL)
