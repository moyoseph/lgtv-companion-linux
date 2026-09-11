from __future__ import annotations

from lgtvcompanion import config as config_mod
from lgtvcompanion.daemon.main import Daemon
from lgtvcompanion.ssap.handshake import KeyStore


def _daemon(tmp_path, cfg):
    path = tmp_path / "config.json"
    config_mod.save(cfg, path)
    ks = KeyStore(tmp_path / "keys")
    return Daemon(cfg, ks, str(tmp_path / "ipc.sock"), config_path=path), path


def _cfg(**dev):
    d = {"id": "tv1", "host": "10.0.0.6"}
    d.update(dev)
    return config_mod.Config(devices=[config_mod.DeviceConfig(**d)])


def test_reload_hot_applies_device_tunables(tmp_path):
    cfg = _cfg(source_hdmi_input=4, wol_method="subnet")
    daemon, path = _daemon(tmp_path, cfg)
    # edit config on disk, then reload
    cfg.devices[0].source_hdmi_input = 2
    cfg.devices[0].wol_method = "auto"
    config_mod.save(cfg, path)
    result = daemon.reload()
    assert result["reloaded"] is True
    assert result["restart_needed"] is False
    assert daemon.sessions[0].cfg.source_hdmi_input == 2
    assert daemon.sessions[0].cfg.wol_method == "auto"


def test_reload_flags_restart_on_host_change(tmp_path):
    daemon, path = _daemon(tmp_path, _cfg())
    disk = config_mod.load(path)          # fresh objects, like the GUI editing the file
    disk.devices[0].host = "10.0.0.9"
    config_mod.save(disk, path)
    assert daemon.reload()["restart_needed"] is True


def test_reload_flags_restart_on_device_add(tmp_path):
    daemon, path = _daemon(tmp_path, _cfg())
    disk = config_mod.load(path)
    disk.devices.append(config_mod.DeviceConfig(id="tv2", host="10.0.0.7"))
    config_mod.save(disk, path)
    assert daemon.reload()["restart_needed"] is True


async def test_reload_toggles_idle_engine(tmp_path):
    cfg = _cfg()
    cfg.global_.idle.enabled = False
    daemon, path = _daemon(tmp_path, cfg)
    assert daemon.idle_engine is None
    disk = config_mod.load(path)
    disk.global_.idle.enabled = True
    disk.global_.idle.minutes = 5
    config_mod.save(disk, path)
    daemon.reload()                       # starts the idle task (needs a loop)
    assert daemon.idle_engine is not None
    assert daemon.idle_engine.timeout_s == 5 * 60
    daemon.idle_engine.stop()


def test_status_report(tmp_path):
    daemon, _ = _daemon(tmp_path, _cfg(name="LG OLED"))
    st = daemon.status()
    assert "tv1" in st["devices"]
    assert st["devices"]["tv1"]["name"] == "LG OLED"
    assert st["idle_enabled"] is False
