"""Steam Controller activity detection over raw /dev/hidraw* reports.

In a gamescope session the kernel `hid-steam` driver stops emitting evdev events
the moment the Steam client opens the controller over hidraw, so the agent's
evdev `InputMonitor` sees nothing — a keyboard, which Steam never claims, still
works, hence the asymmetry. This module detects real controller input straight
from the hidraw report stream instead (read-only; hidraw has no exclusive grab,
so it coexists with Steam and bypasses gamescope's EVIOCGRAB).

The controller streams its input report continuously (~266 Hz on the 2026 Steam
Controller) even when idle, so "a report arrived" is meaningless — but at idle
the payload is frozen except a sequence-counter byte (the IMU is off by
default). We therefore diff the payload and ignore bytes that change on nearly
every frame (the counter, and the IMU block if a profile turns gyro on). This is
deliberately *offset-agnostic*: it does not hard-code the community-reversed byte
layout, which varies by firmware and by USB/Bluetooth/2.4GHz link, so it stays
correct across Steam controller variants and fails safe on anything it can't
parse. (For reference, the SC2 "0x42" input report carries buttons/triggers/pads/
sticks at bytes 0x02–0x1d and the maskable IMU block at 0x1e–0x35.)
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

VALVE_VID = 0x28DE

# Only the high-rate input report matters; battery/link housekeeping arrives at
# <=2 Hz and its payload legitimately changes, so it must never count as input.
HIGH_RATE_MAX_INTERVAL = 0.05   # s; input report ~3.76 ms, housekeeping >=0.5 s
WARMUP_FRAMES = 128             # ~0.5 s at 266 Hz: learn the noise mask first
EWMA_ALPHA = 0.05               # per-offset change-frequency smoothing
NOISE_THRESHOLD = 0.5           # offset changing on >50% of frames = carrier noise
GUARD_FRACTION = 0.7            # sustained activity rate above this => untrust device


class _ReportStream:
    """Volatility-masked change detector for one report ID on one device."""

    def __init__(self) -> None:
        self._last: bytes | None = None
        self._last_time: float | None = None
        self._interval = 1.0          # EWMA inter-arrival gap (s)
        self._freq: list[float] = []  # per-offset EWMA change frequency
        self._frames = 0
        self._activity_rate = 0.0     # EWMA of flagged activity (runaway guard)
        self._trusted = True

    def feed(self, report: bytes, now: float) -> bool:
        if self._last_time is not None:
            gap = now - self._last_time
            if gap >= 0:
                self._interval += EWMA_ALPHA * (gap - self._interval)
        self._last_time = now

        prev, self._last = self._last, report
        # (re)initialise the mask when the layout appears or its length changes
        if prev is None or len(prev) != len(report):
            self._freq = [0.0] * len(report)
            self._frames = 0
            return False

        changed = [a != b for a, b in zip(report, prev, strict=True)]
        for i, c in enumerate(changed):
            self._freq[i] += EWMA_ALPHA * ((1.0 if c else 0.0) - self._freq[i])
        self._frames += 1

        # gate detection on: it's the fast input report, the mask is trained, and
        # we still trust this stream. The mask keeps updating regardless.
        if (self._interval >= HIGH_RATE_MAX_INTERVAL
                or self._frames < WARMUP_FRAMES or not self._trusted):
            return False

        # activity = a normally-stable byte moved. Jittery bytes (the counter,
        # the IMU block, analog-stick LSBs) have high change-frequency and are
        # masked out, so they neither trigger nor suppress real input.
        active = any(changed[i] and self._freq[i] <= NOISE_THRESHOLD
                     for i in range(len(changed)))

        self._activity_rate += EWMA_ALPHA * (
            (1.0 if active else 0.0) - self._activity_rate)
        if self._activity_rate > GUARD_FRACTION:
            # the mask never converged (unknown format / noise we failed to
            # mask): stop trusting this stream so idle-blank still works.
            self._trusted = False
            log.warning("hidraw stream looks unparseable; ignoring it for idle "
                        "(activity rate %.2f)", self._activity_rate)
            return False
        return active


class SteamControllerActivity:
    """Per-device detector: feed each raw HID report, get True on genuine input.

    One instance per /dev/hidraw* node; reports are demultiplexed by report ID
    (byte 0) so housekeeping reports never contaminate the input-report mask.
    """

    def __init__(self) -> None:
        self._streams: dict[int, _ReportStream] = {}

    def feed(self, report: bytes, now: float) -> bool:
        if not report:
            return False
        stream = self._streams.get(report[0])
        if stream is None:
            stream = self._streams[report[0]] = _ReportStream()
        return stream.feed(report, now)
