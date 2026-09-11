from __future__ import annotations

import json

from lgtvcompanion.config import import_windows

WINDOWS_CONFIG = {
    "LGTV Companion": {
        "Version": 3,
        "PowerOnTimeOut": 25,
        "BlankWhenIdle": True,
        "BlankWhenIdleDelay": 15,
        "MuteSpeakers": True,
        "ExternalAPI": True,
        "RemoteStream": True,
        "RemoteStreamPowerOff": False,
        "RemoteStreamEndMode": 2,
        "AdhereDisplayTopology": True,
        "IgnoredKeysList": [175, 174, 91],
        "BlankWhenIdleProcessList": {
            "Steam": {"Binary": "steam*", "Running": True},
            "MPC": {"Binary": "mpc-hc64.exe", "Running": True, "Fullscreen": True},
        },
        "TimingShutdown": 1,
        "UpdaterMode": 2,
    },
    "Device1": {
        "Name": "[LG] webOS TV OLED65C14LB",
        "IP": "192.168.1.42",
        "MAC": ["aa:bb:cc:dd:ee:ff"],
        "Enabled": True,
        "NewSockConnect": True,
        "WOL": 3,
        "Subnet": "255.255.255.0",
        "PersistentConnectionLevel": 2,
        "SourceHdmiInput": 2,
        "CheckHdmiInputWhenPoweringOff": True,
        "SetHdmiInput": True,
        "SetHdmiInputDelay": 5,
        "SessionKey": "abc123sessionkey",
        "UniqueDeviceKey": "MONITOR\\LGD1234",
        "NicLuid": 123456789,
    },
    "Device2": {
        "Name": "Bedroom",
        "IP": "192.168.1.43",
        "MAC": "11:22:33:44:55:66",
        "WOL": 1,
    },
}


def test_import_windows_full(tmp_path):
    src = tmp_path / "config.json"
    src.write_text(json.dumps(WINDOWS_CONFIG))
    cfg, keys = import_windows(src)

    g = cfg.global_
    assert g.power_on_timeout == 25
    assert g.idle.enabled is True
    assert g.idle.minutes == 15
    assert g.idle.mute_speakers is True
    assert g.remote_stream.enabled is True
    assert g.remote_stream.on_connect == "blank"
    assert g.remote_stream.on_disconnect == "restore"
    assert g.topology.enabled is True
    # VK 175/174 map to evdev volume keys; VK 91 (Win key) is dropped
    assert sorted(g.idle.ignored_keys) == [114, 115]
    assert {e["match"] for e in g.idle.process_list} == {"steam*", "mpc-hc64.exe"}

    d1 = cfg.device("device1")
    assert d1.host == "192.168.1.42"
    assert d1.name == "OLED65C14LB"          # prefix stripped
    assert d1.mac == ["aa:bb:cc:dd:ee:ff"]
    assert d1.wol_method == "subnet"
    assert d1.subnet == "255.255.255.0"
    assert d1.persistent_connection == "keepalive"
    assert d1.source_hdmi_input == 2
    assert d1.set_hdmi_input == 2            # falls back to SourceHdmiInput
    assert d1.set_hdmi_input_delay == 5
    assert d1.unique_display_key == "MONITOR\\LGD1234"
    assert d1.interface is None              # NicLuid dropped

    d2 = cfg.device("device2")
    assert d2.mac == ["11:22:33:44:55:66"]   # bare string wrapped
    assert d2.wol_method == "broadcast"
    assert d2.name == "Bedroom"

    assert keys == {"device1": "abc123sessionkey"}


def test_import_windows_roundtrips_through_save_load(tmp_path):
    src = tmp_path / "config.json"
    src.write_text(json.dumps(WINDOWS_CONFIG))
    cfg, _ = import_windows(src)
    from lgtvcompanion import config as config_mod
    out = tmp_path / "converted.json"
    config_mod.save(cfg, out)
    loaded = config_mod.load(out)
    assert loaded.device("device1").source_hdmi_input == 2
