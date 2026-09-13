"""Pure-logic tests for EDID/topology parsing, the Sunshine/process stream
watchers, and the remaining CLI token-parser branches."""

from __future__ import annotations

import struct

from lgtvcompanion.agent import streams
from lgtvcompanion.cli.main import (
    ParseError,
    _is_negative_number,
    _looks_like_flag,
    parse_tokens,
    print_help,
)
from lgtvcompanion.daemon import topology

# -- EDID / topology ----------------------------------------------------------


def _make_edid(*, mfr_raw=0x1E6D, product=0xABCD, serial=0, serial_str=None):
    edid = bytearray(128)
    edid[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    edid[8:10] = struct.pack(">H", mfr_raw)     # 0x1E6D -> "GSM"
    edid[10:12] = struct.pack("<H", product)
    edid[12:16] = struct.pack("<I", serial)
    if serial_str is not None:
        block = b"\x00\x00\x00\xff\x00" + (serial_str + "\n").encode()
        block = block.ljust(18, b" ")[:18]
        edid[54:72] = block
    return bytes(edid)


def test_parse_edid_key_with_serial_string():
    key = topology.parse_edid_key(_make_edid(serial_str="SN123"))
    assert key == "GSM-abcd-SN123"


def test_parse_edid_key_falls_back_to_numeric_serial():
    key = topology.parse_edid_key(_make_edid(serial=42))
    assert key == "GSM-abcd-42"


def test_parse_edid_key_rejects_bad_header():
    assert topology.parse_edid_key(b"\x00" * 128) is None
    assert topology.parse_edid_key(b"\x00\xff") is None  # too short


def test_connected_displays_reads_sysfs(tmp_path, monkeypatch):
    drm = tmp_path / "drm"
    good = drm / "card0-HDMI-A-1"
    good.mkdir(parents=True)
    (good / "status").write_text("connected\n")
    (good / "edid").write_bytes(_make_edid(serial_str="SN123"))
    off = drm / "card0-DP-1"
    off.mkdir(parents=True)
    (off / "status").write_text("disconnected\n")
    monkeypatch.setattr(topology, "DRM_PATH", drm)

    out = topology.connected_displays()
    assert out == {"card0-HDMI-A-1": "GSM-abcd-SN123"}


def test_topology_watcher_stop_without_task():
    topology.TopologyWatcher(lambda keys: None).stop()  # no task → no raise


# -- Sunshine log watcher -----------------------------------------------------


def test_find_sunshine_log_configured_path(tmp_path):
    log_file = tmp_path / "sunshine.log"
    log_file.write_text("")
    assert streams.find_sunshine_log(str(log_file)) == log_file
    assert streams.find_sunshine_log(str(tmp_path / "absent.log")) is None


def test_find_sunshine_log_auto(monkeypatch, tmp_path):
    log_file = tmp_path / "sunshine.log"
    log_file.write_text("")
    monkeypatch.setattr(streams, "DEFAULT_LOG_LOCATIONS", (str(log_file),))
    assert streams.find_sunshine_log("auto") == log_file


def test_sunshine_process_toggles_on_connect_disconnect():
    events: list = []
    w = streams.SunshineWatcher(None, lambda s: events.append(s))  # path unused by process()
    w.process("noise\nCLIENT CONNECTED foo\nmore")
    w.process("CLIENT CONNECTED again")   # already streaming → no re-fire
    w.process("CLIENT DISCONNECTED bar")
    assert events == [True, False]
    assert w.streaming is False


def test_sunshine_read_new_handles_growth_and_rotation(tmp_path):
    path = tmp_path / "sunshine.log"
    path.write_text("first line\n")
    w = streams.SunshineWatcher(path, lambda s: None)
    w._pos = path.stat().st_size            # start at end (only future events)
    with open(path, "a") as f:
        f.write("CLIENT CONNECTED\n")
    chunk = w._read_new()
    assert "CLIENT CONNECTED" in chunk
    # truncation resets the read position
    path.write_text("x")
    assert w._read_new() == "x"


# -- process stream watcher ---------------------------------------------------


def test_process_watcher_globs_and_debounces():
    events: list = []
    w = streams.ProcessStreamWatcher(["parsec*", "chrome-remote-desktop*"],
                                     lambda a: events.append(a))
    w.poll_once({"bash", "parsecd"})     # match -> active
    w.poll_once({"parsecd"})             # still active -> no re-fire
    w.poll_once({"bash"})                # gone -> inactive
    assert events == [True, False]


def test_running_process_names_returns_a_set():
    assert isinstance(streams._running_process_names(), set)


# -- CLI token parser edge branches -------------------------------------------


def test_is_negative_number():
    assert _is_negative_number("-5") is True
    assert _is_negative_number("-3.14") is True
    assert _is_negative_number("-poweron") is False


def test_looks_like_flag():
    assert _looks_like_flag("-mute") is True
    assert _looks_like_flag("-5") is False
    assert _looks_like_flag("tv1") is False


def test_parse_stops_optional_args_at_next_command():
    inv = parse_tokens(["-get_system_settings", "picture", "-poweron"])
    assert len(inv.commands) == 2
    assert inv.commands[0][0].name == "get_system_settings"
    assert inv.commands[0][1] == ["picture"]
    assert inv.commands[1][0].name == "poweron"


def test_parse_no_command_is_error():
    try:
        parse_tokens(["tv1", "tv2"])
    except ParseError as e:
        assert "no command" in str(e)
    else:
        raise AssertionError("expected ParseError")


def test_print_help_lists_commands(capsys):
    print_help()
    assert "-poweron" in capsys.readouterr().out
