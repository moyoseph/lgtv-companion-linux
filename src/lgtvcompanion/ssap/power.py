"""Power-state machine: on / off / blank with webOS state quirks."""

from __future__ import annotations

import asyncio
import logging
from enum import Enum

from .client import SsapClient, SsapError

log = logging.getLogger(__name__)

URI_GET_POWER_STATE = "com.webos.service.tvpower/power/getPowerState"
URI_TURN_ON_SCREEN = "com.webos.service.tvpower/power/turnOnScreen"
URI_TURN_OFF_SCREEN = "com.webos.service.tvpower/power/turnOffScreen"
URI_TURN_OFF = "system/turnOff"
URI_FOREGROUND_APP = "com.webos.applicationManager/getForegroundAppInfo"
URI_LAUNCH = "system.launcher/launch"

POWER_POLL_INTERVAL = 1.0


class PowerState(Enum):
    ACTIVE = "Active"
    SCREEN_OFF = "Screen Off"
    ACTIVE_STANDBY = "Active Standby"
    SUSPEND = "Suspend"
    UNKNOWN = "Unknown"


def hdmi_app_id(input_number: int) -> str:
    return f"com.webos.app.hdmi{input_number}"


async def get_power_state(client: SsapClient) -> PowerState:
    payload = await client.request(URI_GET_POWER_STATE)
    state = payload.get("state", "")
    for ps in PowerState:
        if ps.value.lower() == str(state).lower():
            return ps
    log.debug("%s: unrecognized power state %r", client.host, state)
    return PowerState.UNKNOWN


async def get_foreground_app(client: SsapClient) -> str | None:
    try:
        payload = await client.request(URI_FOREGROUND_APP)
        return payload.get("appId") or None
    except (TimeoutError, SsapError, ConnectionError):
        return None


async def check_hdmi_guard(client: SsapClient, source_hdmi_input: int | None) -> bool:
    """True if it is safe to power off / blank: either no guard configured, or
    the TV's foreground app is this PC's HDMI input. Unknown foreground falls
    through to True so a normal shutdown on the PC's own input still works."""
    if not source_hdmi_input:
        return True
    app = await get_foreground_app(client)
    if app is None:
        return True
    if app != hdmi_app_id(source_hdmi_input):
        log.info("%s: foreground is %s, not %s — refusing to touch power",
                 client.host, app, hdmi_app_id(source_hdmi_input))
        return False
    return True


async def power_on(
    client: SsapClient,
    *,
    timeout: float = 40.0,
    set_hdmi_input: int | None = None,
    set_hdmi_input_delay: float = 0.0,
) -> PowerState:
    """Bring an already-connected TV to Active.

    State handling (upstream-faithful):
      Active          -> nothing to do
      Screen Off      -> turnOnScreen (unblank)
      Active Standby  -> system/turnOff acts as a toggle-ON, then poll
                         getPowerState every second until Active
    WoL / reconnect for a fully-off TV is the session manager's job — by the
    time this runs a websocket session exists.
    """
    state = await get_power_state(client)
    if state == PowerState.SCREEN_OFF:
        await client.request(URI_TURN_ON_SCREEN, {"standbyMode": "active"})
        state = await get_power_state(client)
    elif state == PowerState.ACTIVE_STANDBY:
        await client.request(URI_TURN_OFF)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(POWER_POLL_INTERVAL)
            state = await get_power_state(client)
            if state == PowerState.ACTIVE:
                break
    elif state == PowerState.UNKNOWN:
        # Some firmwares answer getPowerState oddly right after waking; an
        # explicit unblank is harmless (-102 when already on).
        await client.request(URI_TURN_ON_SCREEN, {"standbyMode": "active"})
        state = await get_power_state(client)

    if set_hdmi_input:
        if set_hdmi_input_delay > 0:
            await asyncio.sleep(set_hdmi_input_delay)
        await client.request(URI_LAUNCH, {"id": hdmi_app_id(set_hdmi_input)})
    return state


async def power_off(
    client: SsapClient,
    *,
    source_hdmi_input: int | None = None,
    check_hdmi_input: bool = True,
    standby_mode: str = "active",
    force: bool = False,
) -> bool:
    """Power the TV off. Returns False if the HDMI guard refused.

    standby_mode "active" keeps the TV's network interface alive in standby so
    Wake-on-LAN can bring it back (the proven legacy behavior).
    """
    state = await get_power_state(client)
    if state in (PowerState.ACTIVE_STANDBY, PowerState.SUSPEND):
        return True  # already off enough; turnOff would toggle it back ON
    if (not force and check_hdmi_input
            and not await check_hdmi_guard(client, source_hdmi_input)):
        return False
    await client.request(URI_TURN_OFF, {"standbyMode": standby_mode})
    return True


async def blank_screen(
    client: SsapClient,
    *,
    source_hdmi_input: int | None = None,
    check_hdmi_input: bool = True,
) -> bool:
    """Turn off the panel emitters, keep webOS running. Returns False if the
    HDMI guard refused."""
    state = await get_power_state(client)
    if state != PowerState.ACTIVE:
        return True
    if check_hdmi_input and not await check_hdmi_guard(client, source_hdmi_input):
        return False
    await client.request(URI_TURN_OFF_SCREEN, {"standbyMode": "active"})
    return True


async def unblank_screen(client: SsapClient) -> None:
    await client.request(URI_TURN_ON_SCREEN, {"standbyMode": "active"})
