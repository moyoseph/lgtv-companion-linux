"""Luna settings writes via the createAlert/closeAlert injection.

SSAP does not expose luna:// endpoints directly. Upstream's trick: create an
alert whose onclick/onclose/onfail all point at the luna URI with the desired
params, then immediately close the alert — closing fires the luna call.
"""

from __future__ import annotations

import logging

from .client import SsapClient

log = logging.getLogger(__name__)

URI_CREATE_ALERT = "system.notifications/createAlert"
URI_CLOSE_ALERT = "system.notifications/closeAlert"

LUNA_SET_SYSTEM_SETTINGS = "luna://com.webos.settingsservice/setSystemSettings"
LUNA_SET_DEVICE_INFO = "luna://com.webos.service.eim/setDeviceInfo"
LUNA_SET_WOL = "luna://com.webos.settingsservice/setSystemSettings"


async def luna_send(client: SsapClient, luna_uri: str, params: dict) -> None:
    button = {"label": "", "onClick": luna_uri, "params": params}
    payload = {
        "message": " ",
        "buttons": [button],
        "onclose": {"uri": luna_uri, "params": params},
        "onfail": {"uri": luna_uri, "params": params},
    }
    resp = await client.request(URI_CREATE_ALERT, payload)
    alert_id = resp.get("alertId")
    if alert_id:
        await client.request(URI_CLOSE_ALERT, {"alertId": alert_id})
    else:
        log.warning("%s: createAlert returned no alertId (%s)", client.host, resp)


async def set_system_setting(client: SsapClient, category: str, settings: dict) -> None:
    await luna_send(client, LUNA_SET_SYSTEM_SETTINGS,
                    {"category": category, "settings": settings})


async def enable_tv_wol(client: SsapClient) -> None:
    """Turn on the TV's 'Turn on via network' setting — done after pairing so
    Wake-on-LAN works without a trip through the TV menus."""
    await set_system_setting(client, "network", {"wolwowlOnOff": "true"})
