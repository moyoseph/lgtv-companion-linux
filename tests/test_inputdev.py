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
import pytest
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


def test_on_readable_eof_evicts_device():
    # EOF (device re-enumerated after resume) must free the path so the next
    # scan can reopen it — it used to be passed to _handle and leak the dead fd.
    events: list[tuple] = []
    m = idev.InputMonitor(lambda *a: events.append(a))
    r, w = os.pipe()
    os.close(w)                                # closed write end -> read() = b""
    m._fds["/dev/x"] = r
    m._on_readable("/dev/x", r)
    assert "/dev/x" not in m._fds
    assert events == []


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


async def test_scan_reopens_same_name_replaced_node(monkeypatch, tmp_path):
    # USB re-enumeration on resume re-creates the node under the same name:
    # the path is present in both glob and _fds, but our fd points at the dead
    # predecessor. _scan must notice (inode changed) and reopen.
    fifo = str(tmp_path / "event0")
    os.mkfifo(fifo)
    monkeypatch.setattr(idev.glob, "glob", lambda pattern: [fifo])
    m = idev.InputMonitor(lambda *a: None)
    m._loop = asyncio.get_running_loop()
    try:
        m._scan()
        old_ino = os.fstat(m._fds[fifo]).st_ino
        os.unlink(fifo)
        os.mkfifo(fifo)                        # same name, new inode
        m._scan()
        # the fd NUMBER may be reused by the OS — compare what it points at
        assert os.fstat(m._fds[fifo]).st_ino == os.stat(fifo).st_ino != old_ino
        assert not m._is_stale(fifo)
    finally:
        for path in list(m._fds):
            m._close_device(path)


async def test_scan_keeps_unchanged_node_open(monkeypatch, tmp_path):
    fifo = str(tmp_path / "event0")
    os.mkfifo(fifo)
    monkeypatch.setattr(idev.glob, "glob", lambda pattern: [fifo])
    m = idev.InputMonitor(lambda *a: None)
    m._loop = asyncio.get_running_loop()
    try:
        m._scan()
        fd = m._fds[fifo]
        m._scan()                              # same inode -> no churn
        assert m._fds[fifo] == fd
    finally:
        for path in list(m._fds):
            m._close_device(path)


def test_is_stale_on_stat_failure():
    m = idev.InputMonitor(lambda *a: None)
    m._fds["/dev/gone"] = -1                   # os.fstat(-1) raises -> stale
    assert m._is_stale("/dev/gone") is True


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

def _inotify_event(name: bytes, mask: int = idev.IN_CREATE) -> bytes:
    """A wire-format struct inotify_event with a NUL-padded name."""
    padded = name + b"\0" * (-len(name) % 16 or 16) if name else b""
    return idev._INOTIFY_HEADER.pack(1, mask, 0, len(padded)) + padded


def _inotify_monitor(fd: int, scheduled: list[tuple]) -> idev.InputMonitor:
    m = idev.InputMonitor(lambda *a: None)
    m._inotify_fd = fd
    m._loop = SimpleNamespace(
        call_later=lambda delay, cb: scheduled.append((delay, cb)))
    return m


def test_on_inotify_schedules_delayed_rescan():
    r, w = os.pipe()
    scheduled: list[tuple] = []
    try:
        os.write(w, _inotify_event(b"event7"))
        m = _inotify_monitor(r, scheduled)
        m._on_inotify()
    finally:
        os.close(r)
        os.close(w)
    assert scheduled == [(1.0, m._scan)]


def test_on_inotify_ignores_unrelated_names():
    r, w = os.pipe()
    scheduled: list[tuple] = []
    try:
        # /dev churn that is not ours (WATCH_PREFIX=b"event") must not rescan
        os.write(w, _inotify_event(b"ttyUSB0") + _inotify_event(b"hidraw3"))
        _inotify_monitor(r, scheduled)._on_inotify()
    finally:
        os.close(r)
        os.close(w)
    assert scheduled == []


def test_on_inotify_queue_overflow_forces_rescan():
    r, w = os.pipe()
    scheduled: list[tuple] = []
    try:
        # overflow events carry no name; events were dropped -> rescan anyway
        os.write(w, _inotify_event(b"", mask=idev.IN_Q_OVERFLOW))
        m = _inotify_monitor(r, scheduled)
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


async def test_start_runs_rescan_task_even_with_inotify(monkeypatch):
    # The periodic rescan is belt-and-braces for lost inotify events (suspend)
    # and revalidates open fds -> it must run even when inotify is available.
    monkeypatch.setattr(idev.glob, "glob", lambda pattern: [])
    m = idev.InputMonitor(lambda *a: None)
    monkeypatch.setattr(m, "_setup_inotify", lambda: True)
    m.start()
    try:
        assert m._rescan_task is not None
        assert not m._rescan_task.done()
    finally:
        m.stop()


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


import sys  # noqa: E402


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux inotify")
async def test_setup_inotify_real_watch():
    m = idev.InputMonitor(lambda *a: None)
    m._loop = asyncio.get_running_loop()
    ok = m._setup_inotify()                     # real libc inotify on /dev/input
    try:
        assert ok is True
        assert m._inotify_fd is not None
    finally:
        m.stop()
