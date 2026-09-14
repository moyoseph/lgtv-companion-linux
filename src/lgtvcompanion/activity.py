"""User-activity filtering over raw evdev events.

Mirrors upstream's Raw Input handling: key presses count immediately (unless
ignored), pointer motion needs several samples inside a short window (mouse
debounce), and analog sticks must move beyond a deadband (stick jitter).
Accelerometer devices are excluded entirely at the monitor level.

Two controller inputs are *not* jittery analog sticks and must bypass the
deadband, or they'd never register (Steam Controller / gamepad navigation is
the common victim): D-pads/hats are discrete (-1/0/1), and triggers rest at 0
with a tiny 0..255 range — both sit far below the stick deadband.
"""

from __future__ import annotations

import time

EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03

MOUSE_SAMPLES_REQUIRED = 3     # upstream: mouse must move on >= 3 samples
SAMPLE_WINDOW_S = 1.0
ABS_DEADBAND = 2000            # raw units; sticks idle-jitter well below this

# ABS axes that would otherwise be swallowed by the stick deadband:
ABS_HAT0X, ABS_HAT3Y = 0x10, 0x17               # D-pads: ABS_HAT0X..ABS_HAT3Y
TRIGGER_CODES = frozenset({0x02, 0x05, 0x09, 0x0a})  # ABS_Z / RZ / GAS / BRAKE
TRIGGER_THRESHOLD = 60         # above rest/noise, below any deliberate pull

# Curated evdev KEY_* names for config ignored_keys; numeric codes also accepted
KEY_NAMES = {
    "KEY_MUTE": 113, "KEY_VOLUMEDOWN": 114, "KEY_VOLUMEUP": 115,
    "KEY_POWER": 116, "KEY_PAUSE": 119, "KEY_STOP": 128,
    "KEY_BACK": 158, "KEY_HOMEPAGE": 172,
    "KEY_NEXTSONG": 163, "KEY_PLAYPAUSE": 164, "KEY_PREVIOUSSONG": 165,
    "KEY_STOPCD": 166, "KEY_PLAY": 207,
    "KEY_BRIGHTNESSDOWN": 224, "KEY_BRIGHTNESSUP": 225,
}


def parse_ignored_keys(entries: list) -> set[int]:
    out: set[int] = set()
    for entry in entries:
        if isinstance(entry, int):
            out.add(entry)
        elif isinstance(entry, str) and entry.upper() in KEY_NAMES:
            out.add(KEY_NAMES[entry.upper()])
        elif isinstance(entry, str) and entry.isdigit():
            out.add(int(entry))
    return out


class ActivityFilter:
    """Feed raw events; is_activity() tells you which ones count."""

    def __init__(self, ignored_keys: set[int] | None = None):
        self.ignored_keys = ignored_keys or set()
        self._rel_samples: list[float] = []
        self._abs_last: dict[tuple[str, int], int] = {}

    def is_activity(self, device: str, etype: int, code: int, value: int) -> bool:
        now = time.monotonic()
        if etype == EV_KEY:
            return value == 1 and code not in self.ignored_keys
        if etype == EV_REL:
            self._rel_samples = [t for t in self._rel_samples
                                 if now - t < SAMPLE_WINDOW_S]
            self._rel_samples.append(now)
            return len(self._rel_samples) >= MOUSE_SAMPLES_REQUIRED
        if etype == EV_ABS:
            if ABS_HAT0X <= code <= ABS_HAT3Y:   # D-pad: discrete, no deadband
                return value != 0
            if code in TRIGGER_CODES:            # trigger: rests at 0, tiny range
                return abs(value) > TRIGGER_THRESHOLD
            key = (device, code)                 # analog stick: jitter deadband
            last = self._abs_last.get(key)
            self._abs_last[key] = value
            return last is not None and abs(value - last) > ABS_DEADBAND
        return False
