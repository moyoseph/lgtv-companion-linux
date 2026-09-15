from __future__ import annotations

from lgtvcompanion.steamcontroller import (
    WARMUP_FRAMES,
    SteamControllerActivity,
    _ReportStream,
)

RATE = 0.00376   # ~266 Hz input-report cadence
SLOW = 1.0       # housekeeping cadence


def _report(seq: int, *, rid: int = 0x42, length: int = 54, **override: int) -> bytes:
    """A synthetic HID report: report id at 0, sequence counter at 1, else 0
    unless overridden by byte offset (e.g. _report(5, **{"2": 0x01}))."""
    buf = bytearray(length)
    buf[0] = rid
    buf[1] = seq & 0xFF
    for off, val in override.items():
        buf[int(off)] = val & 0xFF
    return bytes(buf)


def _warm(det: SteamControllerActivity, t0: float = 0.0) -> float:
    """Feed enough idle frames (only the counter moving) to train + pass warmup;
    return the next timestamp. Asserts no false activity during warmup."""
    t = t0
    for seq in range(WARMUP_FRAMES + 20):
        assert det.feed(_report(seq), t) is False
        t += RATE
    return t


def test_idle_stream_never_reports_activity():
    det = SteamControllerActivity()
    t = _warm(det)
    # more idle frames after warmup — the counter is masked, so still nothing
    for seq in range(WARMUP_FRAMES, WARMUP_FRAMES + 200):
        assert det.feed(_report(seq), t) is False
        t += RATE


def test_button_press_is_activity():
    det = SteamControllerActivity()
    t = _warm(det)
    seq = WARMUP_FRAMES + 20
    # a button bit flips in byte 0x02 — a normally-stable byte moved
    assert det.feed(_report(seq, **{"2": 0x01}), t) is True


def test_imu_noise_is_masked_but_stable_byte_counts():
    # IMU on: bytes 0x28/0x29 churn every frame alongside the counter.
    det = SteamControllerActivity()
    t = 0.0
    for seq in range(WARMUP_FRAMES + 20):
        assert det.feed(
            _report(seq, **{"40": seq * 7 & 0xFF, "41": seq * 13 & 0xFF}), t) is False
        t += RATE
    seq = WARMUP_FRAMES + 20
    # a frame where only the (masked) IMU bytes move => not activity
    assert det.feed(_report(seq, **{"40": 0x11, "41": 0x22}), t) is False
    t += RATE
    # a frame where a stable byte (a button) moves => activity
    assert det.feed(_report(seq + 1, **{"40": 0x33, "41": 0x44, "2": 0x08}), t) is True


def test_low_rate_report_never_counts():
    # Same report id but arriving slowly (housekeeping cadence) with a changing
    # payload must never register — only the fast input stream is trusted.
    det = SteamControllerActivity()
    t = 0.0
    for seq in range(WARMUP_FRAMES + 40):
        assert det.feed(_report(seq, **{"2": seq & 0xFF}), t) is False
        t += SLOW


def test_housekeeping_report_id_ignored():
    # A distinct low-rate report id (battery/link) whose payload changes each
    # time it appears must not be treated as input activity.
    det = SteamControllerActivity()
    t = _warm(det)
    for i in range(20):
        assert det.feed(_report(i, rid=0x43, length=20, **{"2": i & 0xFF}), t) is False
        t += 2.5


def test_runaway_guard_untrusts_unparseable_stream():
    # Two bytes each change on ~half the frames (freq ~0.5, so unmasked), so
    # activity would fire on nearly every frame — the guard must disengage.
    det = SteamControllerActivity()
    t = 0.0
    results = []
    for seq in range(WARMUP_FRAMES + 400):
        even = 0xAA if seq % 2 == 0 else 0x00
        odd = 0xBB if seq % 2 == 1 else 0x00
        results.append(det.feed(_report(seq, **{"2": even, "3": odd}), t))
        t += RATE
    # after the guard trips it stays disengaged: the tail is all False
    assert results[-1] is False
    assert not any(results[-50:]), "guard should keep the stream untrusted"


def test_empty_report_is_safe():
    det = SteamControllerActivity()
    assert det.feed(b"", 0.0) is False


def test_length_change_reinitialises_mask():
    s = _ReportStream()
    assert s.feed(_report(0), 0.0) is False           # first: baseline
    assert s.feed(_report(1, length=32), RATE) is False  # length change: re-baseline
