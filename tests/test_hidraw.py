"""Tests for daemon/hidraw.py: sysfs vendor lookup, Valve-only device
acceptance, report → activity → callback, and detector cleanup. Only the
process boundaries (glob, sysfs) are faked; the hotplug skeleton is covered by
test_inputdev.py."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from lgtvcompanion.daemon import hidraw as hraw
from lgtvcompanion.daemon import inputdev as idev
from lgtvcompanion.steamcontroller import VALVE_VID, WARMUP_FRAMES

RATE = 0.00376


def _report(seq: int, *, rid: int = 0x42, length: int = 54, **override: int) -> bytes:
    buf = bytearray(length)
    buf[0] = rid
    buf[1] = seq & 0xFF
    for off, val in override.items():
        buf[int(off)] = val & 0xFF
    return bytes(buf)


# --- hidraw_vendor -----------------------------------------------------------

def test_hidraw_vendor_parses_uevent(tmp_path):
    dev = tmp_path / "hidraw7" / "device"
    dev.mkdir(parents=True)
    (dev / "uevent").write_text(
        "DRIVER=hid-steam\nHID_ID=0003:000028DE:00001302\nHID_NAME=Steam Controller\n")
    assert hraw.hidraw_vendor("/dev/hidraw7", sysfs_root=str(tmp_path)) == VALVE_VID


def test_hidraw_vendor_missing_is_none(tmp_path):
    assert hraw.hidraw_vendor("/dev/hidraw9", sysfs_root=str(tmp_path)) is None


def test_hidraw_vendor_garbage_is_none(tmp_path):
    dev = tmp_path / "hidraw0" / "device"
    dev.mkdir(parents=True)
    (dev / "uevent").write_text("HID_ID=not-a-valid-line\n")
    assert hraw.hidraw_vendor("/dev/hidraw0", sysfs_root=str(tmp_path)) is None


# --- _accept -----------------------------------------------------------------

def test_accept_only_valve_devices(monkeypatch):
    vendors = {"/dev/hidraw0": VALVE_VID, "/dev/hidraw1": 0x045E}
    monkeypatch.setattr(hraw, "hidraw_vendor", lambda p: vendors.get(p))
    m = hraw.HidrawMonitor(lambda path: None)
    assert m._accept("/dev/hidraw0", 3) is True
    assert "/dev/hidraw0" in m._detectors
    assert m._accept("/dev/hidraw1", 4) is False
    assert "/dev/hidraw1" not in m._detectors


# --- _handle → activity → callback -------------------------------------------

def _feed_frames(m, path, n, *, start=0, t0=0.0, **override):
    """Drive m._handle with n synthetic frames; return (fired_paths, next_t)."""
    fired: list[str] = []
    m.callback = fired.append
    t = t0
    ticker = {"t": t}
    m._loop = SimpleNamespace(time=lambda: ticker["t"])
    for seq in range(start, start + n):
        ticker["t"] = t
        m._handle(path, 5, _report(seq, **override))
        t += RATE
    return fired, t


def test_handle_reports_button_activity_after_warmup(monkeypatch):
    monkeypatch.setattr(hraw, "hidraw_vendor", lambda p: VALVE_VID)
    m = hraw.HidrawMonitor(lambda path: None)
    m._accept("/dev/hidraw0", 5)
    # warm up on idle frames (only the counter moves) — nothing should fire
    fired, t = _feed_frames(m, "/dev/hidraw0", WARMUP_FRAMES + 20)
    assert fired == []
    # a button byte moves -> callback fires with the node path
    ticker_t = t
    m._loop = SimpleNamespace(time=lambda: ticker_t)
    hits: list[str] = []
    m.callback = hits.append
    m._handle("/dev/hidraw0", 5, _report(WARMUP_FRAMES + 21, **{"2": 0x01}))
    assert hits == ["/dev/hidraw0"]


def test_handle_ignores_unknown_path(monkeypatch):
    m = hraw.HidrawMonitor(lambda path: (_ for _ in ()).throw(AssertionError()))
    m._loop = SimpleNamespace(time=lambda: 0.0)
    m._handle("/dev/hidraw-not-watched", 5, _report(0))   # no detector -> no-op


# --- hotplug wiring ------------------------------------------------------------

def test_watch_dir_is_dev_with_hidraw_prefix():
    # hidraw nodes live directly in /dev; watching "/dev/hidraw" (not a
    # directory) failed silently and lost inotify hotplug — the resume bug.
    assert hraw.HidrawMonitor.WATCH_DIR == b"/dev"
    assert hraw.HidrawMonitor.WATCH_PREFIX == b"hidraw"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux inotify")
async def test_hidraw_setup_inotify_real_watch():
    m = hraw.HidrawMonitor(lambda path: None)
    m._loop = asyncio.get_running_loop()
    ok = m._setup_inotify()                     # real libc inotify on /dev
    try:
        assert ok is True
    finally:
        m.stop()


# --- scan / cleanup ----------------------------------------------------------

async def test_scan_replaced_node_gets_fresh_detector(monkeypatch, tmp_path):
    # Same-name re-created node (resume) -> reopened with a NEW detector, so
    # the warmup mask and the runaway guard start over.
    fifo = str(tmp_path / "hidraw0")
    os.mkfifo(fifo)
    monkeypatch.setattr(idev.glob, "glob", lambda pattern: [fifo])
    monkeypatch.setattr(hraw, "hidraw_vendor", lambda p: VALVE_VID)
    m = hraw.HidrawMonitor(lambda path: None)
    m._loop = asyncio.get_running_loop()
    try:
        m._scan()
        old_ino, old_detector = os.fstat(m._fds[fifo]).st_ino, m._detectors[fifo]
        os.unlink(fifo)
        os.mkfifo(fifo)                        # same name, new inode
        m._scan()
        # fd numbers get reused — the identity check is inode + detector object
        assert os.fstat(m._fds[fifo]).st_ino != old_ino
        assert m._detectors[fifo] is not old_detector
    finally:
        for path in list(m._fds):
            m._close_device(path)


def test_eof_read_drops_fd_and_detector():
    m = hraw.HidrawMonitor(lambda path: None)
    r, w = os.pipe()
    os.close(w)                                # closed write end -> read() = b""
    m._loop = SimpleNamespace(remove_reader=lambda fd: None)
    m._fds["/dev/hidraw0"] = r
    m._detectors["/dev/hidraw0"] = object()
    m._on_readable("/dev/hidraw0", r)
    assert "/dev/hidraw0" not in m._fds
    assert "/dev/hidraw0" not in m._detectors


async def test_scan_opens_valve_node(monkeypatch, tmp_path):
    fifo = str(tmp_path / "hidraw0")
    os.mkfifo(fifo)
    monkeypatch.setattr(idev.glob, "glob", lambda pattern: [fifo])
    monkeypatch.setattr(hraw, "hidraw_vendor", lambda p: VALVE_VID)
    m = hraw.HidrawMonitor(lambda path: None)
    m._loop = asyncio.get_running_loop()
    try:
        m._scan()
        assert set(m._fds) == {fifo}
        assert fifo in m._detectors
    finally:
        for path in list(m._fds):
            m._close_device(path)


def test_close_device_drops_detector():
    m = hraw.HidrawMonitor(lambda path: None)
    r, w = os.pipe()
    m._loop = SimpleNamespace(remove_reader=lambda fd: None)
    m._fds["/dev/hidraw0"] = r
    m._detectors["/dev/hidraw0"] = object()
    m._close_device("/dev/hidraw0")
    assert "/dev/hidraw0" not in m._detectors
    assert "/dev/hidraw0" not in m._fds
    os.close(w)
