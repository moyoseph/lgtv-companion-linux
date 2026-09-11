from __future__ import annotations

import pytest

from lgtvcompanion.ssap.commands import (
    LUNA_ADJUST_CURVE_PRESET,
    LUNA_SET_CURVE_PRESET,
    LUNA_SET_DEVICE_INFO,
    LUNA_SET_GSR,
    LUNA_SET_SYSTEM_SETTINGS,
    LUNA_SET_TPC,
    COMMANDS,
    Kind,
    build_luna_raw,
    build_luna_setting,
    lookup,
)
from lgtvcompanion.ssap.keys import BUTTONS, canonical_button


def test_table_size_full_parity():
    # 74 generated luna settings + hand-written power/hdmi/audio/button/
    # service-menu/curve/generic/meta commands
    assert len([c for c in COMMANDS.values() if c.kind == Kind.LUNA_SETTING]) == 74
    assert len(COMMANDS) >= 110


def test_generated_luna_string_value():
    cmd = lookup("backlight")
    assert cmd.kind == Kind.LUNA_SETTING
    category, settings = build_luna_setting(cmd, [cmd.args[0].parse("80")])
    # upstream sends numerics as strings unless ValFormat == "int"
    assert (category, settings) == ("picture", {"backlight": "80"})


def test_generated_luna_int_value():
    cmd = lookup("lowleveladjustment")
    category, settings = build_luna_setting(cmd, [cmd.args[0].parse("-15")])
    assert (category, settings) == ("other", {"lowLevelAdjustment": -15})
    with pytest.raises(ValueError):
        cmd.args[0].parse("-31")


def test_generated_per_hdmi_nesting():
    cmd = lookup("gamemode_hdmi2")
    category, settings = build_luna_setting(cmd, [cmd.args[0].parse("on")])
    assert (category, settings) == ("other", {"gameMode": {"hdmi2": "on"}})


def test_generated_enum_ranges():
    pm = lookup("picturemode")
    assert pm.args[0].parse("Game") == "game"
    assert "filmMaker" in pm.args[0].values
    ct = lookup("colortemperature")
    assert ct.args[0].parse("-50") == -50
    with pytest.raises(ValueError):
        ct.args[0].parse("51")
    hdr = lookup("hdrdynamictonemapping")
    assert hdr.args[0].parse("hgig") == "HGIG"


def test_luna_raw_builders():
    assert build_luna_raw(lookup("servicemenu_tpc_enable"), []) == [
        (LUNA_SET_TPC, {"enable": True})]
    assert build_luna_raw(lookup("servicemenu_gsr_disable"), []) == [
        (LUNA_SET_GSR, {"enable": False})]
    # legacy service menu flag is inverted upstream
    assert build_luna_raw(lookup("servicemenu_legacy_enable"), []) == [
        (LUNA_SET_SYSTEM_SETTINGS,
         {"category": "other", "settings": {"svcMenuFlag": False}})]
    assert build_luna_raw(lookup("set_input_type"), ["HDMI_1", "pc", "My PC"]) == [
        (LUNA_SET_DEVICE_INFO, {"id": "HDMI_1", "icon": "pc.png", "label": "My PC"})]
    assert build_luna_raw(lookup("set_curve_preset"), ["flat"]) == [
        (LUNA_SET_CURVE_PRESET, {"type": "flat", "reason": "com.pal.app.settings"})]
    assert build_luna_raw(lookup("adjust_curve_preset"), ["2", 40]) == [
        (LUNA_ADJUST_CURVE_PRESET, {"type": "curvature2", "value": "40%"})]
    assert build_luna_raw(lookup("set_curvature"), ["55"]) == [
        (LUNA_ADJUST_CURVE_PRESET, {"type": "curvature1", "value": "55%"}),
        (LUNA_SET_CURVE_PRESET, {"type": "curvature1", "reason": "com.pal.app.settings"}),
    ]
    assert build_luna_raw(lookup("settings_other"), [{"svcMenuFlag": True}]) == [
        (LUNA_SET_SYSTEM_SETTINGS,
         {"category": "other", "settings": {"svcMenuFlag": True}})]


def test_buttons_vendored():
    assert len(BUTTONS) == 76
    assert canonical_button("volumeup") == "VOLUMEUP"
    assert canonical_button("9") == "9"
    with pytest.raises(ValueError):
        canonical_button("WARP_DRIVE")
    cmd = lookup("button")
    assert cmd.args[0].parse("home") == "HOME"


async def test_button_command_via_session(tv, tmp_path):
    from .test_session_and_ipc import make_session
    s = make_session(tv, tmp_path)
    # fake TV has no pointer socket endpoint -> expect a clean error, not a hang
    with pytest.raises((ConnectionError, OSError)):
        await s.execute(lookup("button"), ["HOME"])
    await s.disconnect()


async def test_generated_command_through_session(tv, tmp_path):
    from .test_session_and_ipc import make_session
    s = make_session(tv, tmp_path)
    result = await s.execute(lookup("energysaving"), ["max"])
    assert result == {"returnValue": True}
    alert = tv.luna_calls[-1]
    assert alert["onclose"]["params"] == {
        "category": "picture", "settings": {"energySaving": "max"}}
    await s.disconnect()
