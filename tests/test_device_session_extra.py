"""Exercise DeviceSession's real (non-dry-run) power actions, connection
retry/WoL, keepalive and settle policy against the fake TV."""

from __future__ import annotations

import asyncio

from lgtvcompanion.config import DeviceConfig
from lgtvcompanion.daemon import devices as devices_mod
from lgtvcompanion.daemon.devices import DeviceSession
from lgtvcompanion.ssap.handshake import KeyStore

from .fake_tv import VALID_KEY
from .harness import make_session


async def test_power_on_from_active_is_noop(tv, tmp_path):
    tv.power_state = "Active"
    s = make_session(tv, tmp_path)
    assert await s.power_on() == "Active"
    assert s.power_state == "Active"
    await s.disconnect()


async def test_power_on_from_screen_off_unblanks(tv, tmp_path):
    tv.power_state = "Screen Off"
    s = make_session(tv, tmp_path)
    assert await s.power_on() == "Active"
    await s.disconnect()


async def test_power_on_from_active_standby_toggles(tv, tmp_path):
    tv.power_state = "Active Standby"
    s = make_session(tv, tmp_path)
    assert await s.power_on() == "Active"
    await s.disconnect()


async def test_power_off_succeeds_on_own_input(tv, tmp_path):
    tv.power_state = "Active"
    tv.foreground_app = "com.webos.app.hdmi4"   # matches source_hdmi_input=4
    s = make_session(tv, tmp_path)
    assert await s.power_off() == "off"
    assert s.power_state == "Active Standby"
    await s.disconnect()


async def test_blank_then_unblank(tv, tmp_path):
    tv.power_state = "Active"
    s = make_session(tv, tmp_path)
    assert await s.blank() == "blanked"
    assert s.power_state == "Screen Off"
    assert await s.unblank() == "on"
    assert s.power_state == "Active"
    await s.disconnect()


async def test_power_off_unreachable_is_already_off(tmp_path):
    store = KeyStore(tmp_path / "keys")
    store.save("tv1", VALID_KEY)
    cfg = DeviceConfig(id="tv1", host="127.0.0.1", ssl=False, retry_attempts=1,
                       backoff_base=0.01, backoff_max=0.01, timeout=0.3)
    s = DeviceSession(cfg, store)   # default port, nothing listening
    assert await s.power_off() == "already-off"


async def test_connect_with_wake_sends_wol(tv, tmp_path, monkeypatch):
    sent: list = []
    monkeypatch.setattr(devices_mod, "send_wol", lambda *a, **k: sent.append((a, k)))
    s = make_session(tv, tmp_path, mac=["00:11:22:33:44:55"], wol_method="broadcast")
    await s.connect(wake=True)
    assert sent  # _wol fired before the first connect attempt
    await s.disconnect()


async def test_settle_off_disconnects(tv, tmp_path):
    s = make_session(tv, tmp_path)
    s.cfg.persistent_connection = "off"
    await s.connect()
    assert s.client.connected
    await s.settle()
    assert not s.client.connected


async def test_settle_keepalive_starts_task(tv, tmp_path):
    s = make_session(tv, tmp_path)
    s.cfg.persistent_connection = "keepalive"
    await s.connect()
    await s.settle()
    assert s._keepalive is not None
    s.stop_keepalive()
    assert s._keepalive is None
    await s.disconnect()


async def test_keepalive_loop_refreshes_power_state(tv, tmp_path, monkeypatch):
    monkeypatch.setattr(devices_mod, "KEEPALIVE_INTERVAL", 0.02)
    tv.power_state = "Active"
    s = make_session(tv, tmp_path)
    await s.connect()
    s.power_state = "Unknown"
    s.start_keepalive()
    await asyncio.sleep(0.08)
    assert s.power_state == "Active"
    s.stop_keepalive()
    await s.disconnect()


async def test_is_reachable_false_when_nothing_listens(tmp_path):
    store = KeyStore(tmp_path / "keys")
    cfg = DeviceConfig(id="tv1", host="127.0.0.1", ssl=True)  # :3001, nothing there
    s = DeviceSession(cfg, store)
    assert await s.is_reachable(timeout=0.3) is False


# -- connection edge branches ---------------------------------------------------

async def test_connect_reraises_key_rejected(tv, tmp_path):
    from lgtvcompanion.ssap.client import KeyRejected
    import pytest
    store = KeyStore(tmp_path / "keys")
    store.save("tv1", "stale-key")            # TV will PROMPT -> KeyRejected
    cfg = DeviceConfig(id="tv1", host="127.0.0.1", ssl=False, timeout=3.0,
                       retry_attempts=2)
    s = DeviceSession(cfg, store)
    orig = s._new_client

    def patched():
        c = orig()
        c.port = tv.port
        return c

    s._new_client = patched
    with pytest.raises(KeyRejected):
        await s.connect()


async def test_connect_retries_then_gives_up(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(devices_mod, "send_wol", lambda *a, **k: None)
    store = KeyStore(tmp_path / "keys")
    store.save("tv1", VALID_KEY)
    cfg = DeviceConfig(id="tv1", host="127.0.0.1", ssl=False, timeout=0.2,
                       retry_attempts=2, backoff_base=0.01, backoff_max=0.02,
                       mac=["00:11:22:33:44:55"])
    s = DeviceSession(cfg, store)             # default port 3000: nothing there
    with pytest.raises(ConnectionError):
        await s.connect(wake=True)


# -- keepalive branches ---------------------------------------------------------

async def test_keepalive_marks_unknown_when_disconnected(tv, tmp_path, monkeypatch):
    monkeypatch.setattr(devices_mod, "KEEPALIVE_INTERVAL", 0.02)
    s = make_session(tv, tmp_path)            # not connected
    s.power_state = "Active"
    s.start_keepalive()
    try:
        for _ in range(200):
            if s.power_state == "Unknown":
                break
            await asyncio.sleep(0.01)
    finally:
        s.stop_keepalive()
    assert s.power_state == "Unknown"


async def test_keepalive_closes_on_ping_failure(tv, tmp_path, monkeypatch):
    monkeypatch.setattr(devices_mod, "KEEPALIVE_INTERVAL", 0.02)
    s = make_session(tv, tmp_path)
    await s.connect()

    async def boom(_client):
        raise RuntimeError("ping failed")

    monkeypatch.setattr(devices_mod.power, "get_power_state", boom)
    s.start_keepalive()
    # the loop closes the client and RETURNS on failure; await it to completion
    # (deterministic — no cancel race) so the final `return` is exercised
    await asyncio.wait_for(s._keepalive, timeout=2)
    assert not s.client.connected
    assert s.power_state == "Unknown"


# -- dry-run / unreachable blank+unblank ----------------------------------------

async def test_blank_unblank_dry_run(tv, tmp_path):
    s = make_session(tv, tmp_path)
    s.dry_run = True
    assert await s.blank() == "dry-run"
    assert await s.unblank() == "dry-run"


async def test_blank_unreachable_is_already_off(tmp_path):
    store = KeyStore(tmp_path / "keys")
    store.save("tv1", VALID_KEY)
    cfg = DeviceConfig(id="tv1", host="127.0.0.1", ssl=False, retry_attempts=1,
                       backoff_base=0.01, backoff_max=0.01, timeout=0.3)
    s = DeviceSession(cfg, store)             # nothing listening
    assert await s.blank() == "already-off"


async def test_is_reachable_true_with_listener(tmp_path):
    import contextlib
    import pytest
    # is_reachable connects to the fixed LG port (3001 for ssl); bind it if free
    async def handler(reader, writer):
        writer.close()

    try:
        server = await asyncio.start_server(handler, "127.0.0.1", 3001)
    except OSError:
        pytest.skip("port 3001 unavailable")
    try:
        store = KeyStore(tmp_path / "keys")
        cfg = DeviceConfig(id="tv1", host="127.0.0.1", ssl=True)
        s = DeviceSession(cfg, store)
        assert await s.is_reachable(timeout=1.0) is True
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()


# -- execute dispatch branches --------------------------------------------------

async def test_execute_power_command(tv, tmp_path):
    from lgtvcompanion.ssap.commands import lookup
    tv.power_state = "Active"
    s = make_session(tv, tmp_path)
    assert await s.execute(lookup("poweron"), []) == "Active"
    await s.disconnect()


async def test_execute_unknown_power_action_raises(tmp_path):
    import pytest
    from lgtvcompanion.ssap.commands import Command, Kind
    store = KeyStore(tmp_path / "keys")
    s = DeviceSession(DeviceConfig(id="tv1", host="127.0.0.1"), store)
    cmd = Command(name="weird", kind=Kind.POWER, action="bogus")
    with pytest.raises(ValueError, match="unknown power action"):
        await s.execute(cmd, [])


async def test_execute_luna_raw_command(tv, tmp_path):
    from lgtvcompanion.ssap.commands import lookup
    s = make_session(tv, tmp_path)
    assert await s.execute(lookup("servicemenu_tpc_enable"), []) == \
        {"returnValue": True}
    assert tv.luna_calls
    await s.disconnect()


async def test_execute_unhandled_kind_raises(tv, tmp_path):
    import pytest
    from lgtvcompanion.ssap.commands import lookup
    s = make_session(tv, tmp_path)
    with pytest.raises(ValueError, match="cannot execute"):
        await s.execute(lookup("idle"), [])   # META reaches execute -> final raise
    await s.disconnect()
