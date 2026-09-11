from __future__ import annotations

import struct

from lgtvcompanion.agent.streams import SunshineWatcher, find_sunshine_log
from lgtvcompanion.config import RemoteStreamConfig
from lgtvcompanion.daemon.streams import StreamController
from lgtvcompanion.daemon.topology import parse_edid_key


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

class FakeSession:
    def __init__(self, id_):
        self.cfg = type("C", (), {"id": id_})()
        self.calls = []

    async def power_off(self):
        self.calls.append("off")
        return "off"

    async def power_on(self):
        self.calls.append("on")
        return "Active"

    async def blank(self):
        self.calls.append("blank")
        return "blanked"


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
