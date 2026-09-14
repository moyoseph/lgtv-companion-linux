from __future__ import annotations

import asyncio
import ssl

import pytest

from lgtvcompanion.ssap.client import (
    InsufficientPermissions,
    KeyRejected,
    PairingRejected,
    SsapClient,
    SsapError,
    _ssl_context,
)
from lgtvcompanion.ssap.input_socket import send_buttons

from .fake_tv import VALID_KEY


async def test_register_with_valid_key(tv, client):
    assert tv.registered_clients == 1
    payload = await client.request("com.webos.service.tvpower/power/getPowerState")
    assert payload["state"] == "Active"


async def test_pairing_from_scratch_saves_key(tv):
    saved = []
    prompts = []
    c = SsapClient("127.0.0.1", use_ssl=False, client_key=None, port=tv.port,
                   timeout=3.0, on_new_key=saved.append,
                   on_pairing_prompt=lambda: prompts.append(1))
    await c.connect(pairing=True)
    await c.close()
    assert saved == [VALID_KEY]
    assert prompts, "pairing prompt callback should fire"


async def test_stale_key_raises_key_rejected(tv):
    c = SsapClient("127.0.0.1", use_ssl=False, client_key="stale-key",
                   port=tv.port, timeout=3.0)
    with pytest.raises(KeyRejected):
        await c.connect()


async def test_benign_minus_102_is_not_an_error(tv, client):
    tv.power_state = "Active"
    payload = await client.request("com.webos.service.tvpower/power/turnOnScreen")
    assert payload.get("errorCode") == "-102"


async def test_real_errors_raise(tv, client):
    tv.error_uris["audio/setVolume"] = {"errorCode": "500", "errorText": "boom"}
    with pytest.raises(SsapError):
        await client.request("audio/setVolume", {"volume": 10})


async def test_concurrent_requests_correlate_by_id(tv, client):
    results = await asyncio.gather(
        client.request("com.webos.service.tvpower/power/getPowerState"),
        client.request("com.webos.applicationManager/getForegroundAppInfo"),
        client.request("audio/setVolume", {"volume": 7}),
    )
    assert results[0]["state"] == "Active"
    assert results[1]["appId"] == "com.webos.app.hdmi4"
    assert results[2]["volume"] == 7


# -- not-connected / url / ssl-context edges ------------------------------------


async def test_request_and_subscribe_require_connection():
    c = SsapClient("127.0.0.1", use_ssl=False, port=1)
    with pytest.raises(ConnectionError):
        await c.request("audio/setVolume", {"volume": 1})
    with pytest.raises(ConnectionError):
        await c.subscribe("x/y", lambda m: None)


def test_url_variants():
    assert SsapClient("tv.lan").url == "wss://tv.lan:3001/"
    assert SsapClient("tv.lan", use_ssl=False).url == "ws://tv.lan:3000/"


def test_ssl_context_skips_verification():
    ctx = _ssl_context()
    assert ctx.verify_mode is ssl.CERT_NONE
    assert ctx.check_hostname is False


# -- subscriptions ---------------------------------------------------------------


async def test_subscribe_dispatches_sync_and_async_callbacks(tv, client):
    sync_events: list = []
    async_events: list = []

    async def acb(msg):
        async_events.append(msg)

    await client.subscribe(
        "com.webos.service.tvpower/power/getPowerState", sync_events.append)
    await client.subscribe(
        "com.webos.applicationManager/getForegroundAppInfo", acb)
    for _ in range(200):
        if sync_events and async_events:
            break
        await asyncio.sleep(0.01)
    assert sync_events[0]["payload"]["state"] == "Active"
    assert async_events[0]["payload"]["appId"] == "com.webos.app.hdmi4"


async def test_subscription_callback_exception_ends_reader(tv, client):
    def boom(msg):
        raise RuntimeError("bad callback")

    await client.subscribe("com.webos.service.tvpower/power/getPowerState", boom)
    for _ in range(200):
        if not client.connected:
            break
        await asyncio.sleep(0.01)
    assert not client.connected


async def test_non_json_frame_is_ignored(tv, client):
    await tv.send_raw("this is not json")
    # connection survives; a normal request still round-trips
    payload = await client.request(
        "com.webos.service.tvpower/power/getPowerState")
    assert payload["state"] == "Active"


# -- error-classification branches ----------------------------------------------


async def test_insufficient_permissions_raises_dedicated_error(tv, client):
    tv.error_uris["audio/setVolume"] = {
        "_error_text": "401 insufficient permissions"}
    with pytest.raises(InsufficientPermissions, match="re-pair"):
        await client.request("audio/setVolume", {"volume": 5})


async def test_register_rejected_text_raises_pairing_rejected(tv):
    tv.register_error = "403 rejected by user"
    c = SsapClient("127.0.0.1", use_ssl=False, client_key=VALID_KEY,
                   port=tv.port, timeout=3.0)
    with pytest.raises(PairingRejected):
        await c.connect()


async def test_register_other_error_raises_ssap_error(tv):
    tv.register_error = "some firmware hiccup"
    c = SsapClient("127.0.0.1", use_ssl=False, client_key=VALID_KEY,
                   port=tv.port, timeout=3.0)
    with pytest.raises(SsapError):
        await c.connect()


async def test_ews_close_during_register_is_retriable_connection_error(tv):
    # standby TVs accept TCP but close 1008 "Try Again Later (EWS)" mid-register
    tv.close_on_register = True
    c = SsapClient("127.0.0.1", use_ssl=False, client_key=VALID_KEY,
                   port=tv.port, timeout=3.0)
    with pytest.raises(ConnectionError):
        await c.connect()
    assert not c.connected


async def test_connection_drop_mid_request_fails_pending_future(tv, client):
    tv.close_uris.add("audio/setVolume")
    with pytest.raises(ConnectionError):
        await client.request("audio/setVolume", {"volume": 5})


async def test_http_endpoint_raises_connection_error():
    async def http_handler(reader, writer):
        await reader.read(100)
        writer.write(b"HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(http_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        c = SsapClient("127.0.0.1", use_ssl=False, port=port, timeout=2.0)
        with pytest.raises(ConnectionError):
            await c.connect()
    finally:
        server.close()
        await server.wait_closed()


# -- pointer-input socket wire format --------------------------------------------


async def test_send_buttons_wire_format_and_delay(tv, client):
    await send_buttons(client, ["mute", "volumeup"], delay=0.01)
    assert tv.button_frames == [
        "type:button\nname:MUTE\n\n",
        "type:button\nname:VOLUMEUP\n\n",
    ]


async def test_send_buttons_without_socket_path(tv, client):
    tv.pointer_socket_enabled = False
    with pytest.raises(ConnectionError, match="no pointer socket"):
        await send_buttons(client, ["mute"])


async def test_send_buttons_wss_builds_ssl_context(client, monkeypatch):
    # a wss:// pointer socket exercises the SSL-context branch (31-33)
    async def fake_request(uri, payload=None, **kw):
        return {"socketPath": "wss://127.0.0.1:3001/"}

    sent: list = []

    class FakeWs:
        async def send(self, frame):
            sent.append(frame)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    captured: dict = {}

    def fake_connect(url, ssl=None, open_timeout=None):
        captured["ssl"] = ssl
        return FakeWs()

    import lgtvcompanion.ssap.input_socket as isock
    monkeypatch.setattr(client, "request", fake_request)
    monkeypatch.setattr(isock.websockets, "connect", fake_connect)
    await send_buttons(client, ["mute"])
    assert sent == ["type:button\nname:MUTE\n\n"]
    assert captured["ssl"] is not None          # an SSLContext was built for wss


# -- reader/close edge branches -------------------------------------------------


async def test_clean_connection_close_ends_reader(tv, client):
    # server closes the socket cleanly -> _read_loop's ConnectionClosed branch (174-175)
    await tv.clients[-1].close()
    for _ in range(200):
        if not client.connected:
            break
        await asyncio.sleep(0.01)
    assert not client.connected


async def test_request_send_failure_becomes_connection_error(tv, client):
    import websockets

    async def boom(_frame):
        raise websockets.WebSocketException("send failed")

    monkeypatch_send = client._ws.send
    client._ws.send = boom
    try:
        with pytest.raises(ConnectionError):
            await client.request("audio/setMute", {"mute": True})
    finally:
        client._ws.send = monkeypatch_send


async def test_subscribe_with_payload(tv, client):
    mid = await client.subscribe("some/uri", lambda m: None, {"p": 1})
    assert mid.startswith("sub-")
    # the subscribe frame carried the payload
    for _ in range(200):
        if any(u == "some/uri" for u, _ in tv.requests):
            break
        await asyncio.sleep(0.01)
    uri, payload = next((u, p) for u, p in tv.requests if u == "some/uri")
    assert payload == {"p": 1}


async def test_close_swallows_ws_close_error(tv, client):
    async def boom():
        raise RuntimeError("close failed")

    client._ws.close = boom
    await client.close()          # must not raise (249-250)
    assert not client.connected


async def test_abnormal_close_hits_connection_closed_branch(tv, client):
    # an abnormal close makes the reader raise ConnectionClosed (174-175)
    await tv.clients[-1].close(code=1011, reason="server error")
    for _ in range(200):
        if not client.connected:
            break
        await asyncio.sleep(0.01)
    assert not client.connected
