from __future__ import annotations

import pytest

from lgtvcompanion.cli.main import ParseError, parse_tokens
from lgtvcompanion.ssap.commands import COMMANDS, Kind, build_request, lookup


def test_every_command_has_help_and_valid_kind():
    for name, cmd in COMMANDS.items():
        assert cmd.help, f"-{name} lacks help text"
        assert isinstance(cmd.kind, Kind)
        if cmd.kind == Kind.REQUEST and cmd.name not in ("request", "request_with_param"):
            assert cmd.uri, f"-{name} lacks a URI"
        if cmd.kind == Kind.LUNA_SETTING:
            assert cmd.luna_category and cmd.luna_setting
        if cmd.kind in (Kind.POWER, Kind.META):
            assert cmd.action


def test_lookup_case_insensitive():
    assert lookup("PowerOn") is COMMANDS["poweron"]
    assert lookup("SETHDMI3") is COMMANDS["sethdmi3"]
    assert lookup("nonsense") is None


def test_build_request_shapes():
    uri, payload = build_request(COMMANDS["volume"], [55])
    assert (uri, payload) == ("audio/setVolume", {"volume": 55})
    uri, payload = build_request(COMMANDS["sethdmi"], [3])
    assert payload == {"id": "com.webos.app.hdmi3"}
    uri, payload = build_request(COMMANDS["request"], ["system/turnOff"])
    assert (uri, payload) == ("system/turnOff", None)
    uri, payload = build_request(COMMANDS["request_with_param"],
                                 ["audio/setMute", {"mute": True}])
    assert (uri, payload) == ("audio/setMute", {"mute": True})
    uri, payload = build_request(COMMANDS["get_system_settings"],
                                 ["picture", ["backlight"]])
    assert payload == {"category": "picture", "keys": ["backlight"]}
    uri, payload = build_request(COMMANDS["get_system_settings"], ["picture"])
    assert payload == {"category": "picture"}


def test_parse_single_command():
    inv = parse_tokens(["-poweron"])
    assert [c.name for c, _ in inv.commands] == ["poweron"]
    assert inv.devices == []


def test_parse_multi_command_with_devices():
    inv = parse_tokens(["-backlight", "80", "-mute", "LivingRoom", "tv2"])
    assert [(c.name, a) for c, a in inv.commands] == [("backlight", [80]), ("mute", [])]
    assert inv.devices == ["LivingRoom", "tv2"]


def test_parse_case_insensitive_and_ranges():
    inv = parse_tokens(["-VOLUME", "20"])
    assert inv.commands[0][1] == [20]
    with pytest.raises(ParseError):
        parse_tokens(["-volume", "101"])
    with pytest.raises(ParseError):
        parse_tokens(["-volume"])


def test_parse_output_modes():
    inv = parse_tokens(["-output", "friendly", "-poweron"])
    assert inv.output_mode == "friendly"
    inv = parse_tokens(["-ok", "state", "-request", "power/getPowerState"])
    assert (inv.output_mode, inv.output_key) == ("key", "state")
    inv = parse_tokens(["-output", "key", "backlight", "-get_system_settings", "picture"])
    assert (inv.output_mode, inv.output_key) == ("key", "backlight")
    assert [c.name for c, _ in inv.commands] == ["get_system_settings"]
    inv = parse_tokens(["-od", "-poweron"])
    assert inv.output_mode == "default"


def test_parse_output_missing_key_is_error_not_silent():
    # a flag-looking token must NOT be swallowed as the key name
    with pytest.raises(ParseError, match="missing key"):
        parse_tokens(["-output", "key", "-ok", "state", "-poweron"])
    with pytest.raises(ParseError, match="missing key"):
        parse_tokens(["-ok", "-poweron"])
    with pytest.raises(ParseError, match="missing mode"):
        parse_tokens(["-output", "-poweron"])


def test_parse_json_arg():
    inv = parse_tokens(["-request_with_param", "audio/setMute", '{"mute": true}'])
    assert inv.commands[0][1] == ["audio/setMute", {"mute": True}]
    with pytest.raises(ParseError):
        parse_tokens(["-request_with_param", "audio/setMute", "{bad json"])


def test_unknown_command_rejected():
    with pytest.raises(ParseError):
        parse_tokens(["-frobnicate"])
