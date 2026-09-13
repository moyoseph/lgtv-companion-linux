from __future__ import annotations

import socket

import pytest

from lgtvcompanion import config as config_mod
from lgtvcompanion.ssap import luna
from lgtvcompanion.ssap.wol import magic_packet, send_wol


def test_magic_packet_bytes():
    pkt = magic_packet("00:11:22:33:44:55")
    assert len(pkt) == 102
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("001122334455")
    assert pkt[6:] == bytes.fromhex("001122334455") * 16
    assert magic_packet("00-11-22-33-44-55") == pkt


def test_magic_packet_rejects_garbage():
    with pytest.raises(ValueError):
        magic_packet("not-a-mac")


def test_send_wol_directed_reaches_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(2)
    port = listener.getsockname()[1]
    # patch the WOL port for the test by sending directly
    from lgtvcompanion.ssap import wol as wol_mod
    old_port = wol_mod.WOL_PORT
    wol_mod.WOL_PORT = port
    try:
        send_wol(["aa:bb:cc:dd:ee:ff"], "127.0.0.1", method="directed")
        data, _ = listener.recvfrom(200)
    finally:
        wol_mod.WOL_PORT = old_port
        listener.close()
    assert data == magic_packet("aa:bb:cc:dd:ee:ff")


async def test_luna_send_uses_alert_injection(tv, client):
    await luna.set_system_setting(client, "picture", {"backlight": 66})
    assert len(tv.luna_calls) == 1
    alert = tv.luna_calls[0]
    assert alert["onclose"]["uri"] == luna.LUNA_SET_SYSTEM_SETTINGS
    assert alert["onclose"]["params"] == {
        "category": "picture", "settings": {"backlight": 66}}
    # and the alert was closed right after
    uris = [u for u, _ in tv.requests]
    assert uris[-1] == "system.notifications/closeAlert"


def test_config_roundtrip(tmp_path):
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(
        id="tv1", host="10.0.0.6", mac=["00:11:22:33:44:55"],
        source_hdmi_input=4)])
    path = tmp_path / "config.json"
    config_mod.save(cfg, path)
    loaded = config_mod.load(path)
    assert loaded.devices[0].host == "10.0.0.6"
    assert loaded.devices[0].source_hdmi_input == 4
    assert loaded.global_.power_on_timeout == 40


def test_config_validation_catches_problems(tmp_path):
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(
        id="tv1", host="10.0.0.6", wol_method="carrier-pigeon")])
    path = tmp_path / "config.json"
    config_mod.save(cfg, path)
    with pytest.raises(ValueError, match="wol_method"):
        config_mod.load(path)


def test_import_legacy_full_tree(tmp_path):
    legacy = tmp_path / "lgtvcontrol"
    legacy.mkdir()
    (legacy / "tv_ip").write_text("10.0.0.6\n")
    (legacy / "tv_mac").write_text("00:11:22:33:44:55\n")
    (legacy / "client.key").write_text("legacy-key-abc\n")
    (legacy / "box_input").write_text("com.webos.app.hdmi2\n")
    (legacy / "config").write_text(
        "timeout = 12\nretry_attempts = 5\n# comment\nbackoff_max = 6\n")

    cfg, key = config_mod.import_legacy(legacy)
    dev = cfg.devices[0]
    assert key == "legacy-key-abc"
    assert dev.host == "10.0.0.6"
    assert dev.mac == ["00:11:22:33:44:55"]
    assert dev.source_hdmi_input == 2
    assert dev.set_hdmi_input == 2
    assert dev.timeout == 12
    assert dev.retry_attempts == 5
    assert dev.backoff_max == 6
    assert dev.backoff_base == 1.0  # untouched default


def test_import_legacy_minimal_tree(tmp_path):
    legacy = tmp_path / "lgtvcontrol"
    legacy.mkdir()
    (legacy / "tv_ip").write_text("10.0.0.9\n")
    cfg, key = config_mod.import_legacy(legacy)
    assert key is None
    assert cfg.devices[0].host == "10.0.0.9"
    assert cfg.devices[0].source_hdmi_input == 4  # proven default
