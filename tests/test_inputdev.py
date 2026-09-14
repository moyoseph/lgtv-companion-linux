"""Tests for daemon/inputdev.py: ioctl bit-packing, accelerometer detection,
event decoding, device scan/open/close (FIFO stand-ins for /dev/input), and
inotify handling. Only the process boundaries (ioctl, glob, libc) are faked."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes.util
import logging
import os
import struct
from types import SimpleNamespace

from lgtvcompanion.daemon import inputdev as idev


# --- _ioc / is_accelerometer -------------------------------------------------

def test_ioc_matches_eviocgprop():
    # EVIOCGPROP(8) as the kernel encodes it
    assert idev._ioc(2, 0x09, 8) == 0x80084509


def test_is_accelerometer_bit_set(monkeypatch):
    def fake_ioctl(fd, request, buf):
        assert request == 0x80084509
        buf[0] |= 0x40  # INPUT_PROP_ACCELEROMETER (6) = bit 6 of byte 0
        return 0

    monkeypatch.setattr("fcntl.ioctl", fake_ioctl)
    assert idev.is_accelerometer(0) is True


def test_is_accelerometer_bit_clear(monkeypatch):
    monkeypatch.setattr("fcntl.ioctl", lambda fd, request, buf: 0)
    assert idev.is_accelerometer(0) is False


def test_is_accelerometer_ioctl_error(monkeypatch):
    def fake_ioctl(fd, request, buf):
        raise OSError("not an evdev node")

    monkeypatch.setattr("fcntl.ioctl", fake_ioctl)
    assert idev.is_accelerometer(0) is False


# --- _on_readable ------------------------------------------------------------

def test_on_readable_decodes_events_and_ignores_partial():
    events: list[tuple] = []
    m = idev.InputMonitor(lambda *a: events.append(a))
    r, w = os.pipe()
    try:
        os.write(w, struct.pack(idev.EVENT_FORMAT, 0, 0, idev.EV_KEY, 30, 1)
                 + struct.pack(idev.EVENT_FORMAT, 0, 0, 2, 0, -3)
                 + b"partial-trailing-bytes")
        m._on_readable("/dev/x", r)
    finally:
        os.close(r)
        os.close(w)
    assert events == [("/dev/x", idev.EV_KEY, 30, 1), ("/dev/x", 2, 0, -3)]


def test_on_readable_empty_nonblocking_pipe_is_silent():
    events: list[tuple] = []
    m = idev.InputMonitor(lambda *a: events.append(a))
    r, w = os.pipe()
    try:
        os.set_blocking(r, False)
        m._on_readable("/dev/x", r)  # nothing buffered -> BlockingIOError
    finally:
        os.close(r)
        os.close(w)
    assert events == []


def test_on_readable_oserror_evicts_device():
    m = idev.InputMonitor(lambda *a: None)
    fd = os.open(os.devnull, os.O_WRONLY)  # read() on write-only fd -> OSError
    m._fds["/dev/x"] = fd
    m._on_readable("/dev/x", fd)  # _close_device guards m._loop is None
    assert "/dev/x" not in m._fds


# --- _scan / _open_device / _close_device ------------------------------------

async def test_scan_opens_new_and_evicts_removed(monkeypatch, tmp_path):
    fifo1, fifo2 = str(tmp_path / "event0"), str(tmp_path / "event1")
    os.mkfifo(fifo1)
    os.mkfifo(fifo2)
    present = [fifo1, fifo2]
    monkeypatch.setattr(idev.glob, "glob", lambda pattern: list(present))
    m = idev.InputMonitor(lambda *a: None)
    m._loop = asyncio.get_running_loop()
    try:
        m._scan()
        assert set(m._fds) == {fifo1, fifo2}
        present.remove(fifo2)
        m._scan()
        assert set(m._fds) == {fifo1}
    finally:
        for path in list(m._fds):
            m._close_device(path)


async def test_scan_skips_accelerometers(monkeypatch, tmp_path):
    fifo = str(tmp_path / "event0")
    os.mkfifo(fifo)
    monkeypatch.setattr(idev.glob, "glob", lambda pattern: [fifo])
    monkeypatch.setattr(idev, "is_accelerometer", lambda fd: True)
    m = idev.InputMonitor(lambda *a: None)
    m._loop = asyncio.get_running_loop()
    m._scan()
    assert m._fds == {}


def test_scan_warns_when_no_device_readable(monkeypatch, tmp_path, caplog):
    missing = str(tmp_path / "gone" / "event0")
    monkeypatch.setattr(idev.glob, "glob", lambda pattern: [missing])
    m = idev.InputMonitor(lambda *a: None)
    with caplog.at_level(logging.WARNING, logger="lgtvcompanion.daemon.inputdev"):
        m._scan()
    assert m._fds == {}
    assert "no input devices readable" in caplog.text


# --- _on_inotify ---------------------------------------------------------------

def test_on_inotify_schedules_delayed_rescan():
    r, w = os.pipe()
    scheduled: list[tuple] = []
    try:
        os.write(w, b"\x00" * 32)  # pretend inotify queued an event
        m = idev.InputMonitor(lambda *a: None)
        m._inotify_fd = r
        m._loop = SimpleNamespace(
            call_later=lambda delay, cb: scheduled.append((delay, cb)))
        m._on_inotify()
    finally:
        os.close(r)
        os.close(w)
    assert scheduled == [(1.0, m._scan)]


def test_on_inotify_drained_fd_schedules_nothing():
    r, w = os.pipe()
    scheduled: list[tuple] = []
    try:
        os.set_blocking(r, False)
        m = idev.InputMonitor(lambda *a: None)
        m._inotify_fd = r
        m._loop = SimpleNamespace(
            call_later=lambda delay, cb: scheduled.append((delay, cb)))
        m._on_inotify()  # empty fd -> BlockingIOError -> silent
    finally:
        os.close(r)
        os.close(w)
    assert scheduled == []


# --- start / stop / _setup_inotify --------------------------------------------

async def test_start_falls_back_to_rescan_task_and_stop_cancels(monkeypatch):
    monkeypatch.setattr(idev.glob, "glob", lambda pattern: [])
    m = idev.InputMonitor(lambda *a: None)
    monkeypatch.setattr(m, "_setup_inotify", lambda: False)
    m.start()
    try:
        assert m._rescan_task is not None
        assert not m._rescan_task.done()
    finally:
        m.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(m._rescan_task, timeout=2.0)
    assert m._rescan_task.cancelled()


def test_setup_inotify_without_libc_returns_false(monkeypatch):
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
    m = idev.InputMonitor(lambda *a: None)
    assert m._setup_inotify() is False
    assert m._inotify_fd is None


def test_stop_tears_down_inotify_fd():
    m = idev.InputMonitor(lambda *a: None)
    r, w = os.pipe()
    removed = []
    m._inotify_fd = r
    m._loop = SimpleNamespace(remove_reader=lambda fd: removed.append(fd))
    m.stop()
    assert removed == [r]
    assert m._inotify_fd is None
    os.close(w)
    with contextlib.suppress(OSError):
        os.close(r)                            # already closed by stop()


def test_close_device_removes_reader_and_swallows_double_close():
    m = idev.InputMonitor(lambda *a: None)
    r, w = os.pipe()
    removed = []
    m._loop = SimpleNamespace(remove_reader=lambda fd: removed.append(fd))
    m._fds["/dev/input/eventX"] = r
    os.close(r)                                # force os.close(fd) to raise OSError
    m._close_device("/dev/input/eventX")       # remove_reader + swallowed OSError
    assert removed == [r]
    assert "/dev/input/eventX" not in m._fds
    os.close(w)


async def test_rescan_loop_runs(monkeypatch):
    monkeypatch.setattr(idev, "RESCAN_FALLBACK_INTERVAL", 0.02)
    monkeypatch.setattr(idev.glob, "glob", lambda pat: [])
    scans = {"n": 0}
    m = idev.InputMonitor(lambda *a: None)
    real_scan = m._scan

    def counting_scan():
        scans["n"] += 1
        real_scan()

    m._scan = counting_scan
    monkeypatch.setattr(m, "_setup_inotify", lambda: False)
    m.start()
    try:
        for _ in range(200):
            if scans["n"] >= 2:                 # initial + at least one rescan-loop pass
                break
            await asyncio.sleep(0.01)
        assert scans["n"] >= 2
    finally:
        m.stop()


def test_stop_closes_open_devices():
    m = idev.InputMonitor(lambda *a: None)
    r, w = os.pipe()
    removed = []
    m._loop = SimpleNamespace(remove_reader=lambda fd: removed.append(fd))
    m._fds["/dev/input/event0"] = r            # an open device -> stop() closes it (72)
    m.stop()
    assert removed == [r]
    assert m._fds == {}
    os.close(w)
    with contextlib.suppress(OSError):
        os.close(r)


def test_close_device_untracked_path_is_noop():
    m = idev.InputMonitor(lambda *a: None)
    m._close_device("/dev/input/never-opened")   # fd is None -> early return (111)
