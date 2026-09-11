"""A fake webOS TV: scriptable websocket server for protocol tests.

Emulates the register flow (PROMPT → registered, wrong-key rejection), the
getPowerState state machine including the Active-Standby-turnOff-toggles-ON
quirk, getForegroundAppInfo, createAlert capture for luna assertions, and
error injection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field

from websockets.asyncio.server import serve

VALID_KEY = "fake-client-key-0123456789abcdef"


@dataclass
class FakeTv:
    power_state: str = "Active"      # Active | Screen Off | Active Standby
    foreground_app: str = "com.webos.app.hdmi4"
    prompt_before_pairing: bool = True
    reject_keys: bool = False        # force re-pair even with VALID_KEY
    error_uris: dict = field(default_factory=dict)   # uri -> payload
    luna_calls: list = field(default_factory=list)   # captured createAlert payloads
    requests: list = field(default_factory=list)     # every (uri, payload)
    registered_clients: int = 0
    port: int = 0
    _server: object = None

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "register":
                await self._register(ws, msg)
            elif mtype in ("request", "subscribe"):
                await self._request(ws, msg)

    async def _register(self, ws, msg) -> None:
        key = msg.get("payload", {}).get("client-key")
        if key == VALID_KEY and not self.reject_keys:
            self.registered_clients += 1
            await ws.send(json.dumps({
                "type": "registered", "id": msg.get("id"),
                "payload": {"client-key": VALID_KEY}}))
            return
        if key and key != VALID_KEY:
            # invalid stored key: TV falls back to prompting
            await ws.send(json.dumps({
                "type": "response", "id": msg.get("id"),
                "payload": {"pairingType": "PROMPT"}}))
            await asyncio.sleep(0.05)
            await ws.send(json.dumps({
                "type": "registered", "id": msg.get("id"),
                "payload": {"client-key": VALID_KEY}}))
            self.registered_clients += 1
            return
        if self.prompt_before_pairing:
            await ws.send(json.dumps({
                "type": "response", "id": msg.get("id"),
                "payload": {"pairingType": "PROMPT"}}))
            await asyncio.sleep(0.05)  # "user pressed OK"
        self.registered_clients += 1
        await ws.send(json.dumps({
            "type": "registered", "id": msg.get("id"),
            "payload": {"client-key": VALID_KEY}}))

    async def _request(self, ws, msg) -> None:
        uri = msg.get("uri", "").removeprefix("ssap://")
        payload = msg.get("payload") or {}
        mid = msg.get("id")
        self.requests.append((uri, payload))

        if uri in self.error_uris:
            await ws.send(json.dumps({
                "type": "error", "id": mid, "error": "injected",
                "payload": self.error_uris[uri]}))
            return

        if uri == "com.webos.service.tvpower/power/getPowerState":
            resp = {"returnValue": True, "state": self.power_state}
        elif uri == "com.webos.service.tvpower/power/turnOnScreen":
            if self.power_state == "Screen Off":
                self.power_state = "Active"
                resp = {"returnValue": True}
            elif self.power_state == "Active":
                resp = {"returnValue": False, "errorCode": "-102",
                        "errorText": "already on"}
                await ws.send(json.dumps({"type": "error", "id": mid,
                                          "error": "-102", "payload": resp}))
                return
            else:
                resp = {"returnValue": True}
        elif uri == "com.webos.service.tvpower/power/turnOffScreen":
            if self.power_state == "Active":
                self.power_state = "Screen Off"
            resp = {"returnValue": True}
        elif uri == "system/turnOff":
            # The quirk: from Active Standby, turnOff acts as a toggle-ON.
            if self.power_state == "Active Standby":
                self.power_state = "Active"
            else:
                self.power_state = "Active Standby"
            resp = {"returnValue": True}
        elif uri == "com.webos.applicationManager/getForegroundAppInfo":
            resp = {"returnValue": True, "appId": self.foreground_app}
        elif uri == "system.launcher/launch":
            app = payload.get("id", "")
            if app.startswith("com.webos.app.hdmi"):
                self.foreground_app = app
            resp = {"returnValue": True, "id": app}
        elif uri == "system.notifications/createAlert":
            self.luna_calls.append(payload)
            resp = {"returnValue": True, "alertId": f"alert-{len(self.luna_calls)}"}
        elif uri == "system.notifications/closeAlert":
            resp = {"returnValue": True}
        elif uri == "audio/setMute":
            resp = {"returnValue": True, "mute": payload.get("mute")}
        elif uri == "audio/setVolume":
            resp = {"returnValue": True, "volume": payload.get("volume")}
        elif uri == "settings/getSystemSettings":
            resp = {"returnValue": True, "settings": {"backlight": "80"}}
        else:
            resp = {"returnValue": True}
        await ws.send(json.dumps({"type": "response", "id": mid, "payload": resp}))
