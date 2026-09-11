from __future__ import annotations

import pytest

from lgtvcompanion.ssap.client import KeyRejected, SsapClient, SsapError

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
    import asyncio
    results = await asyncio.gather(
        client.request("com.webos.service.tvpower/power/getPowerState"),
        client.request("com.webos.applicationManager/getForegroundAppInfo"),
        client.request("audio/setVolume", {"volume": 7}),
    )
    assert results[0]["state"] == "Active"
    assert results[1]["appId"] == "com.webos.app.hdmi4"
    assert results[2]["volume"] == 7
