"""A fake webOS TV: scriptable websocket server for protocol tests.

Emulates the register flow (PROMPT → registered, wrong-key rejection), the
getPowerState state machine including the Active-Standby-turnOff-toggles-ON
quirk, getForegroundAppInfo, createAlert capture for luna assertions, and
error injection. Extra default-off knobs let tests script failure modes:
register_error, close_on_register (the standby "EWS" close), close_uris
(drop the socket mid-request), send_raw (non-JSON frames), and a pointer
side-socket that records button frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field

from websockets.asyncio.server import serve

VALID_KEY = "fake-client-key-0123456789abcdef"

POINTER_SOCKET_URI = "com.webos.service.networkinput/getPointerInputSocket"


@dataclass
class FakeTv:
    power_state: str = "Active"      # Active | Screen Off | Active Standby
    foreground_app: str = "com.webos.app.hdmi4"
    prompt_before_pairing: bool = True
    reject_keys: bool = False        # force re-pair even with VALID_KEY
    register_error: str | None = None  # reply to register with this error text
    close_on_register: bool = False  # close 1008 instead of registering (EWS)
    pointer_socket_enabled: bool = True  # False -> reply without a socketPath
    close_uris: set = field(default_factory=set)  # uri -> close socket mid-request
    error_uris: dict = field(default_factory=dict)   # uri -> payload
    # in error_uris payloads, the special key "_error_text" overrides the
    # frame-level "error" string (default "injected") — for testing text-matched
    # errors like "401 insufficient permissions".
    luna_calls: list = field(default_factory=list)   # captured createAlert payloads
    requests: list = field(default_factory=list)     # every (uri, payload)
    button_frames: list = field(default_factory=list)  # pointer-socket frames
    registered_clients: int = 0
    port: int = 0
    pointer_port: int = 0
    clients: list = field(default_factory=list)      # live server-side sockets
    _server: object = None
    _pointer_server: object = None

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for srv in (self._server, self._pointer_server):
            if srv is not None:
                srv.close()
                with contextlib.suppress(Exception):
                    await srv.wait_closed()

    async def send_raw(self, text: str) -> None:
        """Push a raw frame to the most recent client (e.g. non-JSON noise)."""
        await self.clients[-1].send(text)

    async def _handle(self, ws) -> None:
        self.clients.append(ws)
        async for raw in ws:
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "register":
                await self._register(ws, msg)
            elif mtype in ("request", "subscribe"):
                await self._request(ws, msg)

    async def _register(self, ws, msg) -> None:
        if self.close_on_register:
            await ws.close(code=1008, reason="Try Again Later (EWS)")
            return
        if self.register_error is not None:
            await ws.send(json.dumps({
                "type": "error", "id": msg.get("id"),
                "error": self.register_error, "payload": {}}))
            return
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

    async def _pointer_handle(self, ws) -> None:
        async for raw in ws:
            self.button_frames.append(raw)

    async def _request(self, ws, msg) -> None:
        uri = msg.get("uri", "").removeprefix("ssap://")
        payload = msg.get("payload") or {}
        mid = msg.get("id")
        self.requests.append((uri, payload))

        if uri in self.close_uris:
            await ws.close()
            return

        if uri in self.error_uris:
            err_payload = dict(self.error_uris[uri])
            err_text = err_payload.pop("_error_text", "injected")
            await ws.send(json.dumps({
                "type": "error", "id": mid, "error": err_text,
                "payload": err_payload}))
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
        elif uri == POINTER_SOCKET_URI:
            if not self.pointer_socket_enabled:
                resp = {"returnValue": False}
                await ws.send(json.dumps(
                    {"type": "response", "id": mid, "payload": resp}))
                return
            if self._pointer_server is None:
                self._pointer_server = await serve(
                    self._pointer_handle, "127.0.0.1", 0)
                self.pointer_port = \
                    self._pointer_server.sockets[0].getsockname()[1]
            resp = {"returnValue": True,
                    "socketPath": f"ws://127.0.0.1:{self.pointer_port}/"}
        else:
            resp = {"returnValue": True}
        await ws.send(json.dumps({"type": "response", "id": mid, "payload": resp}))
