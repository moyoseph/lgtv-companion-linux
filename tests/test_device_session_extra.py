"""Exercise DeviceSession's real (non-dry-run) power actions, connection
retry/WoL, keepalive and settle policy against the fake TV."""

from __future__ import annotations

import asyncio

from lgtvcompanion.config import DeviceConfig
from lgtvcompanion.daemon import devices as devices_mod
from lgtvcompanion.daemon.devices import DeviceSession
from lgtvcompanion.ssap.handshake import KeyStore

from .fake_tv import VALID_KEY
from .test_session_and_ipc import make_session


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
