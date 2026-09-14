from __future__ import annotations

import struct

from lgtvcompanion.agent.streams import SunshineWatcher, find_sunshine_log
from lgtvcompanion.config import RemoteStreamConfig
from lgtvcompanion.daemon.streams import StreamController
from lgtvcompanion.daemon.topology import parse_edid_key

from .harness import FakeSession


# --- Sunshine log watcher ----------------------------------------------------

def make_watcher(tmp_path, events):
    log = tmp_path / "sunshine.log"
    log.write_text("[2026:09:11] startup\n")
    w = SunshineWatcher(log, events.append)
    w._pos = log.stat().st_size
    return w, log


def test_sunshine_connect_disconnect(tmp_path):
    events = []
    w, log = make_watcher(tmp_path, events)
    with open(log, "a") as f:
        f.write("[2026:09:11]: Info: CLIENT CONNECTED\n")
    w.process(w._read_new())
    with open(log, "a") as f:
        f.write("[2026:09:11]: Info: CLIENT DISCONNECTED\n")
    w.process(w._read_new())
    assert events == [True, False]


def test_sunshine_duplicate_lines_debounced(tmp_path):
    events = []
    w, log = make_watcher(tmp_path, events)
    with open(log, "a") as f:
        f.write("CLIENT CONNECTED\nCLIENT CONNECTED\n")
    w.process(w._read_new())
    assert events == [True]


def test_sunshine_log_rotation(tmp_path):
    events = []
    w, log = make_watcher(tmp_path, events)
    log.write_text("CLIENT CONNECTED\n")   # truncated + rewritten, smaller
    w.process(w._read_new())
    assert events == [True]


def test_find_sunshine_log_explicit(tmp_path):
    log = tmp_path / "s.log"
    log.write_text("")
    assert find_sunshine_log(str(log)) == log
    assert find_sunshine_log(str(tmp_path / "missing.log")) is None


# --- StreamController --------------------------------------------------------

def stream_cfg(**kw):
    cfg = RemoteStreamConfig(enabled=True)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


async def test_stream_blank_then_restore():
    s = FakeSession("tv1")
    c = StreamController(stream_cfg(on_connect="blank", on_disconnect="restore"),
                         lambda: [s])
    await c.on_connect()
    assert s.calls == ["blank"]
    await c.on_disconnect()
    assert s.calls == ["blank", "on"]


async def test_stream_restore_skips_untouched():
    s = FakeSession("tv1")

    async def refuse():
        s.calls.append("blank-refused")
        return "refused-wrong-input"

    s.blank = refuse
    c = StreamController(stream_cfg(on_connect="blank", on_disconnect="restore"),
                         lambda: [s])
    await c.on_connect()
    result = await c.on_disconnect()
    assert result == {"tv1": "untouched"}   # we never changed it, don't power on


async def test_stream_keep_off():
    s = FakeSession("tv1")
    c = StreamController(stream_cfg(on_connect="off", on_disconnect="keep_off"),
                         lambda: [s])
    await c.on_connect()
    await c.on_disconnect()
    assert s.calls == ["off"]


async def test_stream_disabled_no_action():
    s = FakeSession("tv1")
    cfg = stream_cfg()
    cfg.enabled = False
    c = StreamController(cfg, lambda: [s])
    await c.on_connect()
    assert s.calls == []


async def test_stream_duplicate_connect_ignored():
    s = FakeSession("tv1")
    c = StreamController(stream_cfg(on_connect="blank"), lambda: [s])
    await c.on_connect()
    result = await c.on_connect()
    assert result == {"streaming": "already-connected"}
    assert s.calls == ["blank"]


# --- EDID parsing ------------------------------------------------------------

def make_edid(mfr="GSM", product=0x5B09, serial=0x01010101,
              serial_str=b"302MAXXXXX99") -> bytes:
    mfr_raw = 0
    for i, ch in enumerate(mfr):
        mfr_raw |= (ord(ch) - ord("A") + 1) << (10 - 5 * i)
    edid = bytearray(128)
    edid[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    edid[8:10] = struct.pack(">H", mfr_raw)
    edid[10:12] = struct.pack("<H", product)
    edid[12:16] = struct.pack("<I", serial)
    block = bytearray(18)
    block[3] = 0xFF
    block[5:5 + len(serial_str)] = serial_str
    edid[54:72] = block
    return bytes(edid)


def test_edid_key_with_serial_string():
    assert parse_edid_key(make_edid()) == "GSM-5b09-302MAXXXXX99"


def test_edid_key_numeric_serial_fallback():
    key = parse_edid_key(make_edid(serial_str=b""))
    assert key == f"GSM-5b09-{0x01010101}"


def test_edid_rejects_garbage():
    assert parse_edid_key(b"\x00" * 128) is None
    assert parse_edid_key(b"short") is None


# --- connected_displays / TopologyWatcher loop ---------------------------------

def test_connected_displays_skips_missing_edid(tmp_path, monkeypatch):
    from lgtvcompanion.daemon import topology
    drm = tmp_path / "drm"
    with_edid = drm / "card0-HDMI-A-1"
    with_edid.mkdir(parents=True)
    (with_edid / "status").write_text("connected\n")
    (with_edid / "edid").write_bytes(make_edid())
    no_edid = drm / "card0-HDMI-A-2"          # connected but EDID unreadable
    no_edid.mkdir(parents=True)
    (no_edid / "status").write_text("connected\n")
    monkeypatch.setattr(topology, "DRM_PATH", drm)
    assert topology.connected_displays() == {
        "card0-HDMI-A-1": "GSM-5b09-302MAXXXXX99"}


async def test_topology_watcher_detects_changes(monkeypatch):
    import asyncio
    from lgtvcompanion.daemon import topology
    seen: list = []
    state = {"keys": {"a": "K1"}}
    monkeypatch.setattr(topology, "POLL_INTERVAL", 0.02)
    monkeypatch.setattr(topology, "connected_displays", lambda: dict(state["keys"]))

    async def on_change(keys):
        seen.append(keys)

    w = topology.TopologyWatcher(on_change)
    w.start()
    try:
        await asyncio.sleep(0.06)             # baseline poll
        state["keys"] = {"a": "K1", "b": "K2"}
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0.01)
    finally:
        w.stop()
    assert seen and seen[0] == {"K1", "K2"}


async def test_topology_watcher_survives_handler_exception(monkeypatch):
    import asyncio
    from lgtvcompanion.daemon import topology
    state = {"keys": {"a": "K1"}}
    monkeypatch.setattr(topology, "POLL_INTERVAL", 0.02)
    monkeypatch.setattr(topology, "connected_displays", lambda: dict(state["keys"]))

    async def bad_handler(keys):
        raise RuntimeError("handler exploded")

    w = topology.TopologyWatcher(bad_handler)
    w.start()
    try:
        await asyncio.sleep(0.06)
        state["keys"] = {}
        await asyncio.sleep(0.08)             # change fires the raising handler
        assert not w._task.done()             # loop swallowed it and lives on
    finally:
        w.stop()


# --- SunshineWatcher live loop + /proc scanning ---------------------------------

async def test_sunshine_watcher_live_loop(tmp_path, monkeypatch):
    import asyncio
    from lgtvcompanion.agent import streams
    monkeypatch.setattr(streams, "POLL_INTERVAL", 0.02)
    log = tmp_path / "sunshine.log"
    log.write_text("startup\n")
    events: list = []
    w = SunshineWatcher(log, events.append)
    w.start()
    try:
        with open(log, "a") as f:
            f.write("CLIENT CONNECTED\n")
        for _ in range(200):
            if events:
                break
            await asyncio.sleep(0.01)
    finally:
        w.stop()
    assert events == [True]


async def test_sunshine_watcher_start_with_missing_file(tmp_path):
    w = SunshineWatcher(tmp_path / "absent.log", lambda s: None)
    w.start()          # OSError -> position resets to 0, no raise
    try:
        assert w._pos == 0
        assert w._read_new() == ""            # still missing -> empty
    finally:
        w.stop()


def test_running_process_names_parses_comm_and_cmdline(tmp_path, monkeypatch):
    from lgtvcompanion.agent import streams
    proc = tmp_path / "proc"
    p1 = proc / "123"
    p1.mkdir(parents=True)
    # kernel truncates comm at 15 chars; cmdline holds the full path
    (p1 / "comm").write_text("chrome-remote-d\n")
    (p1 / "cmdline").write_bytes(
        b"/opt/google/chrome-remote-desktop/chrome-remote-desktop\x00--args\x00")
    p2 = proc / "456"
    p2.mkdir()
    (p2 / "comm").write_text("bash\n")        # no cmdline -> OSError, skipped
    real_path = streams.Path

    class PathShim:
        def __call__(self, p):
            return proc if p == "/proc" else real_path(p)

    monkeypatch.setattr(streams, "Path", PathShim())
    names = streams._running_process_names()
    assert "chrome-remote-desktop" in names   # from cmdline argv0 basename
    assert "chrome-remote-d" in names         # from truncated comm
    assert "bash" in names


async def test_process_stream_watcher_live_loop(monkeypatch):
    import asyncio
    from lgtvcompanion.agent import streams
    monkeypatch.setattr(streams, "POLL_INTERVAL", 0.02)
    monkeypatch.setattr(streams, "_running_process_names", lambda: {"parsecd"})
    events: list = []
    w = streams.ProcessStreamWatcher(["parsec*"], events.append)
    w.start()
    try:
        for _ in range(200):
            if events:
                break
            await asyncio.sleep(0.01)
    finally:
        w.stop()
    assert events == [True]


def test_find_sunshine_log_auto_none(monkeypatch):
    from lgtvcompanion.agent import streams
    monkeypatch.setattr(streams, "DEFAULT_LOG_LOCATIONS", ("/no/such/a", "/no/such/b"))
    assert streams.find_sunshine_log("auto") is None


def test_sunshine_read_new_no_growth(tmp_path):
    log = tmp_path / "sunshine.log"
    log.write_text("line\n")
    w = SunshineWatcher(log, lambda s: None)
    w._pos = log.stat().st_size
    assert w._read_new() == ""              # size == pos -> "" (73)


def test_running_process_names_comm_oserror(tmp_path, monkeypatch):
    from lgtvcompanion.agent import streams
    proc = tmp_path / "proc"
    p = proc / "123"
    p.mkdir(parents=True)
    (p / "comm").mkdir()                    # comm is a dir -> read_text OSError (111-112)
    (p / "cmdline").write_bytes(b"/usr/bin/parsecd\x00")
    real = streams.Path

    class PathShim:
        def __call__(self, x):
            return proc if x == "/proc" else real(x)

    monkeypatch.setattr(streams, "Path", PathShim())
    names = streams._running_process_names()
    assert "parsecd" in names               # cmdline still parsed despite comm error
