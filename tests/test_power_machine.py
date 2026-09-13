from __future__ import annotations

from lgtvcompanion.ssap import power
from lgtvcompanion.ssap.power import PowerState


async def test_power_on_from_active_is_noop(tv, client):
    tv.power_state = "Active"
    state = await power.power_on(client)
    assert state == PowerState.ACTIVE
    assert ("system/turnOff", {}) not in tv.requests


async def test_power_on_from_screen_off_unblanks(tv, client):
    tv.power_state = "Screen Off"
    state = await power.power_on(client)
    assert state == PowerState.ACTIVE
    uris = [u for u, _ in tv.requests]
    assert "com.webos.service.tvpower/power/turnOnScreen" in uris


async def test_power_on_from_active_standby_uses_toggle_and_polls(tv, client):
    tv.power_state = "Active Standby"
    state = await power.power_on(client, timeout=5.0)
    assert state == PowerState.ACTIVE
    uris = [u for u, _ in tv.requests]
    assert "system/turnOff" in uris, "Active Standby needs the turnOff toggle"


async def test_power_on_sets_hdmi_input(tv, client):
    tv.power_state = "Active"
    await power.power_on(client, set_hdmi_input=2)
    assert tv.foreground_app == "com.webos.app.hdmi2"


async def test_power_off_on_own_input_succeeds(tv, client):
    tv.power_state = "Active"
    tv.foreground_app = "com.webos.app.hdmi4"
    assert await power.power_off(client, source_hdmi_input=4) is True
    assert tv.power_state == "Active Standby"


async def test_power_off_refused_on_other_source(tv, client):
    tv.power_state = "Active"
    tv.foreground_app = "netflix"
    assert await power.power_off(client, source_hdmi_input=4) is False
    assert tv.power_state == "Active", "TV must be left on"


async def test_power_off_force_ignores_guard(tv, client):
    tv.power_state = "Active"
    tv.foreground_app = "netflix"
    assert await power.power_off(client, source_hdmi_input=4, force=True) is True
    assert tv.power_state == "Active Standby"


async def test_power_off_already_standby_does_not_toggle_back_on(tv, client):
    tv.power_state = "Active Standby"
    assert await power.power_off(client, source_hdmi_input=4) is True
    assert tv.power_state == "Active Standby", "turnOff would have toggled it ON"


async def test_power_off_uses_standby_mode_active(tv, client):
    tv.power_state = "Active"
    tv.foreground_app = "com.webos.app.hdmi4"
    await power.power_off(client, source_hdmi_input=4)
    turnoffs = [p for u, p in tv.requests if u == "system/turnOff"]
    assert turnoffs == [{"standbyMode": "active"}]


async def test_blank_guarded_by_hdmi_input(tv, client):
    tv.power_state = "Active"
    tv.foreground_app = "com.webos.app.hdmi1"
    assert await power.blank_screen(client, source_hdmi_input=4) is False
    tv.foreground_app = "com.webos.app.hdmi4"
    assert await power.blank_screen(client, source_hdmi_input=4) is True
    assert tv.power_state == "Screen Off"


# -- edge branches --------------------------------------------------------------


async def test_unrecognized_power_state_is_unknown(tv, client):
    tv.power_state = "Garbage"
    assert await power.get_power_state(client) == PowerState.UNKNOWN


async def test_foreground_app_error_returns_none(tv, client):
    tv.error_uris[power.URI_FOREGROUND_APP] = {"errorCode": "500"}
    assert await power.get_foreground_app(client) is None


async def test_hdmi_guard_without_configured_input_allows(tv, client):
    assert await power.check_hdmi_guard(client, None) is True


async def test_hdmi_guard_unknown_foreground_allows(tv, client):
    tv.error_uris[power.URI_FOREGROUND_APP] = {"errorCode": "500"}
    assert await power.check_hdmi_guard(client, 4) is True


async def test_power_on_unknown_state_tries_unblank(tv, client):
    tv.power_state = "Garbage"
    state = await power.power_on(client)
    assert state == PowerState.UNKNOWN
    uris = [u for u, _ in tv.requests]
    assert power.URI_TURN_ON_SCREEN in uris


async def test_power_on_sets_hdmi_input_after_delay(tv, client):
    tv.power_state = "Active"
    await power.power_on(client, set_hdmi_input=2, set_hdmi_input_delay=0.01)
    assert tv.foreground_app == "com.webos.app.hdmi2"


async def test_blank_screen_noop_when_not_active(tv, client):
    tv.power_state = "Screen Off"
    assert await power.blank_screen(client) is True
    uris = [u for u, _ in tv.requests]
    assert power.URI_TURN_OFF_SCREEN not in uris


async def test_unblank_screen_turns_panel_on(tv, client):
    tv.power_state = "Screen Off"
    await power.unblank_screen(client)
    assert tv.power_state == "Active"
