from __future__ import annotations

import socket
import sys

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


# -- wol: portable edge branches --------------------------------------------------


def test_magic_packet_rejects_wrong_length():
    with pytest.raises(ValueError, match="bad MAC"):
        magic_packet("00:11:22:33:44")        # 5 octets


def test_send_wol_bad_target_is_swallowed(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="lgtvcompanion.ssap.wol"):
        # unparseable address -> gaierror (an OSError) at sendto, warned not raised
        send_wol(["00:11:22:33:44:55"], "127.0.0.1", method="directed",
                 extra_targets=["999.999.999.999"])
    assert "WoL send" in caplog.text


def test_send_wol_bind_failure_is_swallowed(monkeypatch, caplog):
    import logging
    from lgtvcompanion.ssap import wol as wol_mod
    # an IP this machine does not own -> bind() fails, warned not raised
    monkeypatch.setattr(wol_mod, "interface_ip", lambda name: "203.0.113.7")
    with caplog.at_level(logging.WARNING, logger="lgtvcompanion.ssap.wol"):
        send_wol(["00:11:22:33:44:55"], "127.0.0.1", method="directed",
                 interface="eth0")
    assert "could not bind" in caplog.text


async def test_wol_burst_resends_until_stop(monkeypatch):
    import asyncio
    from lgtvcompanion.ssap import wol as wol_mod
    calls: list = []
    monkeypatch.setattr(wol_mod, "send_wol", lambda *a, **k: calls.append(1))
    stop = asyncio.Event()

    async def setter():
        await asyncio.sleep(0.05)
        stop.set()

    task = asyncio.create_task(setter())
    await asyncio.wait_for(
        wol_mod.wol_burst(["00:11:22:33:44:55"], "127.0.0.1",
                          stop=stop, interval=0.01), timeout=2)
    await task
    assert len(calls) >= 2          # resent at least once before stop


# -- wol: Linux-only real-interface paths (run on the CI badge job) ----------------


linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux SIOCGIF* ioctls")


@linux_only
def test_subnet_broadcast_of_loopback():
    from lgtvcompanion.ssap.wol import subnet_broadcast
    assert subnet_broadcast("127.0.0.1") == "127.255.255.255"


@linux_only
def test_interface_ip_of_loopback():
    from lgtvcompanion.ssap.wol import interface_ip
    assert interface_ip("lo") == "127.0.0.1"


# -- config: remaining validate/import branches ------------------------------------


def test_user_state_path_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert config_mod.user_state_path() == tmp_path / "lgtv-companion"


def test_device_selector_miss_returns_none():
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="h")])
    assert cfg.device("nope") is None


def test_validate_device_field_ranges():
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(
        id="tv1", host="", source_hdmi_input=9, set_hdmi_input_delay=99)])
    joined = " ".join(config_mod.validate(cfg))
    assert "missing host" in joined
    assert "source_hdmi_input" in joined
    assert "set_hdmi_input_delay" in joined


def test_find_config_prefers_system(monkeypatch, tmp_path):
    sysconf = tmp_path / "sys.json"
    config_mod.save(config_mod.Config(
        devices=[config_mod.DeviceConfig(id="tv1", host="h")]), sysconf)
    monkeypatch.setattr(config_mod, "SYSTEM_CONFIG", sysconf)
    assert config_mod.find_config() == sysconf


def test_import_legacy_backoff_and_bad_values(tmp_path, caplog):
    import logging
    legacy = tmp_path / "lgtvcontrol"
    legacy.mkdir()
    (legacy / "tv_ip").write_text("192.0.2.5\n")
    (legacy / "config").write_text("backoff_base = 1.5\ntimeout = zebra\n")
    with caplog.at_level(logging.WARNING, logger="lgtvcompanion.config"):
        cfg, _ = config_mod.import_legacy(legacy)
    assert cfg.devices[0].backoff_base == 1.5
    assert cfg.devices[0].timeout == 10.0      # bad value ignored, default kept
    assert "bad value" in caplog.text


def test_import_windows_remaining_prefs(tmp_path):
    import json as json_mod
    data = {
        "LGTV Companion": {
            "BlankWhenIdleFullscreenDisable": True,
            "KeepTopologyOnBoot": True,
            "BlankWhenIdleProcessList": {"Empty": {"Binary": "", "Running": True}},
        },
        "Device1": {"IP": "192.0.2.9"},
    }
    src = tmp_path / "win.json"
    src.write_text(json_mod.dumps(data))
    cfg, _ = config_mod.import_windows(src)
    assert cfg.global_.idle.veto_fullscreen is True
    assert cfg.global_.topology.keep_on_boot is True
    assert cfg.global_.idle.process_list == []   # empty Binary skipped
