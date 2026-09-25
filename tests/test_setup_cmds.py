"""Unit tests for `lgtvc setup` — systemd unit rendering and the subcommands,
mocked so nothing touches the real system. cmd_pair runs a REAL pairing
conversation against a FakeTv served from a helper thread (cmd_pair calls
asyncio.run itself, so the test stays sync)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from lgtvcompanion import config as config_mod
from lgtvcompanion.cli import setup_cmds

from .fake_tv import VALID_KEY, FakeTv


def _system_install_env(tmp_path, monkeypatch, *, mqtt=False, tray=False):
    unit_dir = tmp_path / "etc-systemd-system"
    unit_dir.mkdir()
    user_unit_dir = tmp_path / "etc-systemd-user"
    sysconf = tmp_path / "etc" / "lgtv-companion" / "config.json"
    monkeypatch.setattr(setup_cmds, "SYSTEM_UNIT_DIR", unit_dir)
    monkeypatch.setattr(setup_cmds, "USER_UNIT_DIR", user_unit_dir)
    monkeypatch.setattr(config_mod, "SYSTEM_CONFIG", sysconf)
    monkeypatch.setattr(setup_cmds.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup_cmds, "_python_bin", lambda: "/venv/bin/python3")
    monkeypatch.setattr(setup_cmds, "_module_available", lambda p, m: mqtt)
    monkeypatch.setattr(setup_cmds, "_tray_available", lambda p: tray)
    ran: list = []
    monkeypatch.setattr(setup_cmds, "_run", lambda cmd, check=True: ran.append(cmd))
    return unit_dir, user_unit_dir, sysconf, ran


def test_system_install_renders_core_units(tmp_path, monkeypatch):
    unit_dir, user_unit_dir, sysconf, ran = _system_install_env(tmp_path, monkeypatch)
    rc = setup_cmds.cmd_install(SimpleNamespace(mode="system", service_user="me"))
    assert rc == 0

    daemon = (unit_dir / setup_cmds.DAEMON_UNIT).read_text()
    assert "/venv/bin/python3 -m lgtvcompanion.daemon.main" in daemon
    assert f"--config {sysconf}" in daemon
    assert "User=me" in daemon  # non-root service user gets a User= line
    assert (unit_dir / setup_cmds.SHUTDOWN_UNIT).exists()
    assert (unit_dir / setup_cmds.SLEEP_UNIT).exists()
    assert (user_unit_dir / setup_cmds.AGENT_UNIT).exists()
    # optional units skipped when their deps aren't importable
    assert not (unit_dir / setup_cmds.MQTT_UNIT).exists()
    assert not (user_unit_dir / setup_cmds.TRAY_UNIT).exists()
    assert any("daemon-reload" in c for c in ran)


def test_system_install_root_user_has_no_user_line(tmp_path, monkeypatch):
    unit_dir, _, _, _ = _system_install_env(tmp_path, monkeypatch)
    setup_cmds.cmd_install(SimpleNamespace(mode="system", service_user="root"))
    daemon = (unit_dir / setup_cmds.DAEMON_UNIT).read_text()
    assert "User=" not in daemon


def test_system_install_adds_tray_and_mqtt_when_available(tmp_path, monkeypatch):
    unit_dir, user_unit_dir, _, _ = _system_install_env(
        tmp_path, monkeypatch, mqtt=True, tray=True)
    setup_cmds.cmd_install(SimpleNamespace(mode="system", service_user="me"))
    assert (unit_dir / setup_cmds.MQTT_UNIT).exists()
    assert (user_unit_dir / setup_cmds.TRAY_UNIT).exists()


def test_system_install_requires_root(monkeypatch):
    monkeypatch.setattr(setup_cmds.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        setup_cmds.cmd_install(SimpleNamespace(mode="system", service_user="me"))


def test_module_available_true_and_false(monkeypatch):
    monkeypatch.setattr(setup_cmds.subprocess, "run", lambda *a, **k: None)
    assert setup_cmds._module_available("py", "os") is True

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "cmd")
    monkeypatch.setattr(setup_cmds.subprocess, "run", boom)
    assert setup_cmds._module_available("py", "nope") is False


def test_cmd_show_without_config(monkeypatch):
    monkeypatch.setattr(config_mod, "find_config", lambda: None)
    assert setup_cmds.cmd_show(SimpleNamespace()) == 1


def test_cmd_show_prints_config(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    path.write_text('{"schema": 1}')
    monkeypatch.setattr(config_mod, "find_config", lambda: path)
    assert setup_cmds.cmd_show(SimpleNamespace()) == 0
    assert "schema" in capsys.readouterr().out


def test_map_display_no_displays(monkeypatch):
    import lgtvcompanion.daemon.topology as topo
    monkeypatch.setattr(topo, "connected_displays", lambda: {})
    rc = setup_cmds.cmd_map_display(SimpleNamespace(device=None, key=None))
    assert rc == 1


def test_map_display_lists_and_flags_lg(monkeypatch, capsys):
    import lgtvcompanion.daemon.topology as topo
    monkeypatch.setattr(topo, "connected_displays",
                        lambda: {"HDMI-A-1": "GSM7766", "DP-1": "DEL1234"})
    rc = setup_cmds.cmd_map_display(SimpleNamespace(device=None, key=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "GSM7766" in out and "<- LG" in out


def test_cmd_import_legacy_writes_dry_run_config(tmp_path, monkeypatch):
    legacy = tmp_path / "lgtvcontrol"
    legacy.mkdir()
    (legacy / "tv_ip").write_text("192.0.2.5\n")
    (legacy / "client.key").write_text("key-xyz\n")
    sysconf = tmp_path / "etc" / "config.json"
    monkeypatch.setattr(config_mod, "SYSTEM_CONFIG", sysconf)
    monkeypatch.setattr(config_mod, "SYSTEM_STATE", tmp_path / "state")

    rc = setup_cmds.cmd_import_legacy(SimpleNamespace(legacy_dir=str(legacy)))
    assert rc == 0
    cfg = config_mod.load(sysconf)
    assert cfg.global_.dry_run is True
    assert cfg.devices[0].host == "192.0.2.5"


def test_cmd_import_legacy_missing_dir(tmp_path):
    with pytest.raises(SystemExit):
        setup_cmds.cmd_import_legacy(
            SimpleNamespace(legacy_dir=str(tmp_path / "nope")))


def test_setup_main_dispatches(monkeypatch):
    monkeypatch.setattr(config_mod, "find_config", lambda: None)
    # `setup show` with no config returns 1 through the argparse dispatcher
    assert setup_cmds.main(["show"]) == 1


# -- cmd_pair against a real FakeTv -------------------------------------------


class _ThreadTv:
    """FakeTv served from a dedicated thread+loop, for sync commands that call
    asyncio.run themselves (cmd_pair)."""

    def __enter__(self):
        self.tv = FakeTv()
        self.loop = asyncio.new_event_loop()
        started = threading.Event()

        def runner():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.tv.start())
            started.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()
        assert started.wait(5), "fake TV thread failed to start"
        return self.tv

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.tv.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        return False


def _patch_pair_env(monkeypatch, tmp_path, tv):
    """Route cmd_pair's SsapClient at the fake TV and its keystore at tmp."""
    import lgtvcompanion.ssap.client as client_mod

    real = client_mod.SsapClient

    class PatchedClient(real):
        def __init__(self, host, **kw):
            kw["use_ssl"] = False
            kw["port"] = tv.port
            super().__init__(host, **kw)

    monkeypatch.setattr(client_mod, "SsapClient", PatchedClient)
    monkeypatch.setattr(setup_cmds.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(config_mod, "user_state_path", lambda: tmp_path / "state")


def test_cmd_pair_with_host_pairs_and_enables_wol(monkeypatch, tmp_path, capsys):
    with _ThreadTv() as tv:
        _patch_pair_env(monkeypatch, tmp_path, tv)
        rc = setup_cmds.cmd_pair(SimpleNamespace(host="127.0.0.1", device="tv1"))
    assert rc == 0
    # the real pairing flow ran: prompt printed, key persisted
    assert "Approve the connection" in capsys.readouterr().out
    key_file = tmp_path / "state" / "keys" / "tv1.key"
    assert key_file.read_text().strip() == VALID_KEY
    # and the TV's WoL setting was enabled via the luna alert trick
    assert any("wolwowlOnOff" in json.dumps(alert) for alert in tv.luna_calls)


def test_cmd_pair_uses_configured_device(monkeypatch, tmp_path):
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(
        id="livingroom", host="127.0.0.1", ssl=False)])
    cfg_path = tmp_path / "config.json"
    config_mod.save(cfg, cfg_path)
    monkeypatch.setattr(config_mod, "find_config", lambda: cfg_path)
    with _ThreadTv() as tv:
        _patch_pair_env(monkeypatch, tmp_path, tv)
        # unknown --device falls back to the first configured device
        rc = setup_cmds.cmd_pair(SimpleNamespace(host=None, device="nope"))
    assert rc == 0
    assert (tmp_path / "state" / "keys" / "livingroom.key").exists()


def test_cmd_pair_no_config_exits(monkeypatch):
    monkeypatch.setattr(config_mod, "find_config", lambda: None)
    with pytest.raises(SystemExit):
        setup_cmds.cmd_pair(SimpleNamespace(host=None, device="tv1"))


def test_cmd_pair_no_devices_exits(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    config_mod.save(config_mod.Config(devices=[]), cfg_path)
    monkeypatch.setattr(config_mod, "find_config", lambda: cfg_path)
    with pytest.raises(SystemExit):
        setup_cmds.cmd_pair(SimpleNamespace(host=None, device="tv1"))


# -- cmd_import_windows --------------------------------------------------------


def test_cmd_import_windows_end_to_end(monkeypatch, tmp_path):
    from .test_import_windows import WINDOWS_CONFIG

    src = tmp_path / "win.json"
    src.write_text(json.dumps(WINDOWS_CONFIG))
    target = tmp_path / "cfg" / "config.json"
    monkeypatch.setattr(setup_cmds.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(config_mod, "user_config_path", lambda: target)
    monkeypatch.setattr(config_mod, "user_state_path", lambda: tmp_path / "state")

    rc = setup_cmds.cmd_import_windows(SimpleNamespace(file=str(src)))
    assert rc == 0
    loaded = config_mod.load(target)
    assert loaded.device("device1").host == "192.168.1.42"
    key_file = tmp_path / "state" / "keys" / "device1.key"
    assert key_file.read_text().strip() == "abc123sessionkey"


def test_cmd_import_windows_no_keys(monkeypatch, tmp_path, capsys):
    from .test_import_windows import WINDOWS_CONFIG

    data = json.loads(json.dumps(WINDOWS_CONFIG))
    del data["Device1"]["SessionKey"]
    src = tmp_path / "win.json"
    src.write_text(json.dumps(data))
    monkeypatch.setattr(setup_cmds.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(config_mod, "user_config_path",
                        lambda: tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(config_mod, "user_state_path", lambda: tmp_path / "state")
    assert setup_cmds.cmd_import_windows(SimpleNamespace(file=str(src))) == 0
    assert "no session keys" in capsys.readouterr().out


def test_cmd_import_windows_missing_file(tmp_path):
    with pytest.raises(SystemExit):
        setup_cmds.cmd_import_windows(SimpleNamespace(file=str(tmp_path / "no.json")))


# -- cmd_migrate_legacy / cmd_rollback_legacy ----------------------------------


def _migrate_env(monkeypatch, tmp_path, *, dry_run=True):
    sysconf = tmp_path / "etc" / "config.json"
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="h")])
    cfg.global_.dry_run = dry_run
    config_mod.save(cfg, sysconf)
    monkeypatch.setattr(config_mod, "SYSTEM_CONFIG", sysconf)
    monkeypatch.setattr(setup_cmds.os, "geteuid", lambda: 0)
    ran: list = []
    monkeypatch.setattr(setup_cmds, "_run", lambda cmd, check=True: ran.append(cmd))
    return sysconf, ran


def test_migrate_legacy_flips_dry_run_and_swaps_units(monkeypatch, tmp_path):
    sysconf, ran = _migrate_env(monkeypatch, tmp_path)
    rc = setup_cmds.cmd_migrate_legacy(
        SimpleNamespace(legacy_user="bob", keep_dry_run=False))
    assert rc == 0
    assert config_mod.load(sysconf).global_.dry_run is False
    joined = [" ".join(c) for c in ran]
    assert any("disable" in c and "lgtv-startup.service" in c for c in joined)
    assert any("--machine" in c and "bob@.host" in c for c in joined)
    assert any(c.startswith("systemctl enable") for c in joined)
    assert any("restart" in c and setup_cmds.DAEMON_UNIT in c for c in joined)


def test_migrate_legacy_keep_dry_run(monkeypatch, tmp_path):
    sysconf, _ = _migrate_env(monkeypatch, tmp_path)
    rc = setup_cmds.cmd_migrate_legacy(
        SimpleNamespace(legacy_user=None, keep_dry_run=True))
    assert rc == 0
    assert config_mod.load(sysconf).global_.dry_run is True


def test_migrate_legacy_needs_root(monkeypatch):
    monkeypatch.setattr(setup_cmds.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        setup_cmds.cmd_migrate_legacy(SimpleNamespace(legacy_user=None,
                                                      keep_dry_run=False))


def test_rollback_legacy_reenables_legacy_units(monkeypatch, tmp_path):
    _, ran = _migrate_env(monkeypatch, tmp_path)
    rc = setup_cmds.cmd_rollback_legacy(SimpleNamespace(legacy_user="bob"))
    assert rc == 0
    joined = [" ".join(c) for c in ran]
    assert any("disable" in c and setup_cmds.DAEMON_UNIT in c for c in joined)
    assert any("enable" in c and "lgtv-startup.service" in c for c in joined)
    assert any("--machine" in c for c in joined)


def test_rollback_legacy_needs_root(monkeypatch):
    monkeypatch.setattr(setup_cmds.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        setup_cmds.cmd_rollback_legacy(SimpleNamespace(legacy_user=None))


# -- cmd_map_display --device paths --------------------------------------------


def _display_env(monkeypatch, tmp_path, displays):
    import lgtvcompanion.daemon.topology as topo
    monkeypatch.setattr(topo, "connected_displays", lambda: dict(displays))
    cfg_path = tmp_path / "config.json"
    config_mod.save(config_mod.Config(
        devices=[config_mod.DeviceConfig(id="tv1", host="h")]), cfg_path)
    monkeypatch.setattr(config_mod, "find_config", lambda: cfg_path)
    return cfg_path


def test_map_display_autopicks_single_lg(monkeypatch, tmp_path):
    cfg_path = _display_env(monkeypatch, tmp_path,
                            {"card0-HDMI-A-1": "GSM-abcd-1", "DP-1": "DEL-1"})
    rc = setup_cmds.cmd_map_display(SimpleNamespace(device="tv1", key=None))
    assert rc == 0
    assert config_mod.load(cfg_path).devices[0].unique_display_key == "GSM-abcd-1"


def test_map_display_ambiguous_lg_needs_key(monkeypatch, tmp_path):
    _display_env(monkeypatch, tmp_path,
                 {"HDMI-A-1": "GSM-1", "HDMI-A-2": "GSM-2"})
    assert setup_cmds.cmd_map_display(
        SimpleNamespace(device="tv1", key=None)) == 1


def test_map_display_no_lg_needs_key(monkeypatch, tmp_path):
    _display_env(monkeypatch, tmp_path, {"DP-1": "DEL-1"})
    assert setup_cmds.cmd_map_display(
        SimpleNamespace(device="tv1", key=None)) == 1


def test_map_display_explicit_key(monkeypatch, tmp_path):
    cfg_path = _display_env(monkeypatch, tmp_path, {"DP-1": "DEL-1"})
    rc = setup_cmds.cmd_map_display(SimpleNamespace(device="tv1", key="DEL-1"))
    assert rc == 0
    assert config_mod.load(cfg_path).devices[0].unique_display_key == "DEL-1"


def test_map_display_unknown_device_exits(monkeypatch, tmp_path):
    _display_env(monkeypatch, tmp_path, {"HDMI-A-1": "GSM-1"})
    with pytest.raises(SystemExit):
        setup_cmds.cmd_map_display(SimpleNamespace(device="bogus", key=None))


def test_map_display_no_config_exits(monkeypatch, tmp_path):
    import lgtvcompanion.daemon.topology as topo
    monkeypatch.setattr(topo, "connected_displays", lambda: {"H": "GSM-1"})
    monkeypatch.setattr(config_mod, "find_config", lambda: None)
    with pytest.raises(SystemExit):
        setup_cmds.cmd_map_display(SimpleNamespace(device="tv1", key=None))


# -- cmd_import_legacy without a client key ------------------------------------


def test_cmd_import_legacy_without_key(monkeypatch, tmp_path, capsys):
    legacy = tmp_path / "lgtvcontrol"
    legacy.mkdir()
    (legacy / "tv_ip").write_text("192.0.2.5\n")
    monkeypatch.setattr(config_mod, "SYSTEM_CONFIG", tmp_path / "etc" / "c.json")
    monkeypatch.setattr(config_mod, "SYSTEM_STATE", tmp_path / "state")
    assert setup_cmds.cmd_import_legacy(
        SimpleNamespace(legacy_dir=str(legacy))) == 0
    assert "no legacy client.key" in capsys.readouterr().out


# -- user install with the optional extras present -----------------------------


def test_user_install_renders_tray_and_mqtt_units(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup_cmds.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(setup_cmds.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(setup_cmds, "_python_bin", lambda: "/venv/bin/python3")
    monkeypatch.setattr(setup_cmds, "_tray_available", lambda p: True)
    monkeypatch.setattr(setup_cmds, "_module_available", lambda p, m: True)
    monkeypatch.setattr(setup_cmds, "_run", lambda cmd, check=True: None)

    rc = setup_cmds.cmd_install(SimpleNamespace(mode="user", service_user="me"))
    assert rc == 0
    unit_dir = home / ".config" / "systemd" / "user"
    assert (unit_dir / setup_cmds.TRAY_UNIT).exists()
    assert (unit_dir / setup_cmds.MQTT_UNIT).exists()


# -- real helper paths ----------------------------------------------------------


def test_python_bin_is_current_interpreter():
    assert setup_cmds._python_bin() == sys.executable


def test_module_available_real_subprocess():
    assert setup_cmds._module_available(sys.executable, "json") is True
    assert setup_cmds._module_available(sys.executable, "not_a_module_xyz") is False


def test_run_prints_and_executes(capsys):
    setup_cmds._run([sys.executable, "-c", "pass"])
    assert capsys.readouterr().out.startswith(f"+ {sys.executable}")


def test_tray_available_delegates(monkeypatch):
    monkeypatch.setattr(setup_cmds, "_module_available", lambda p, m: True)
    assert setup_cmds._tray_available("py") is True


def test_offline_mode_subcommand_toggles(monkeypatch, tmp_path, capsys):
    cfg_path = tmp_path / "config.json"
    config_mod.save(config_mod.Config(
        devices=[config_mod.DeviceConfig(id="tv1", host="h")]), cfg_path)
    monkeypatch.setattr(config_mod, "find_config", lambda: cfg_path)
    # default state
    setup_cmds.cmd_offline_mode(SimpleNamespace(state=None))
    assert capsys.readouterr().out.strip() == "off"
    # turn on -> persisted + restart reminder
    setup_cmds.cmd_offline_mode(SimpleNamespace(state="on"))
    assert "restart" in capsys.readouterr().out.lower()
    assert config_mod.load(cfg_path).global_.offline_mode is True
    setup_cmds.cmd_offline_mode(SimpleNamespace(state=None))
    assert capsys.readouterr().out.strip() == "on"


def test_offline_mode_subcommand_no_config(monkeypatch):
    monkeypatch.setattr(config_mod, "find_config", lambda: None)
    with pytest.raises(SystemExit):
        setup_cmds.cmd_offline_mode(SimpleNamespace(state="on"))


def test_steam_controller_subcommand_toggles(monkeypatch, tmp_path, capsys):
    cfg_path = tmp_path / "config.json"
    config_mod.save(config_mod.Config(
        devices=[config_mod.DeviceConfig(id="tv1", host="h")]), cfg_path)
    monkeypatch.setattr(config_mod, "find_config", lambda: cfg_path)
    setup_cmds.cmd_steam_controller(SimpleNamespace(state=None, wake=None))
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "on"                                # bare state first (scripts)
    assert out[1] == "wake = on"                         # both default on
    setup_cmds.cmd_steam_controller(SimpleNamespace(state="off", wake=None))
    assert "restart" in capsys.readouterr().out.lower()
    assert config_mod.load(cfg_path).global_.steam_controller.enabled is False


def test_steam_controller_wake_flag(monkeypatch, tmp_path, capsys):
    cfg_path = tmp_path / "config.json"
    config_mod.save(config_mod.Config(
        devices=[config_mod.DeviceConfig(id="tv1", host="h")]), cfg_path)
    monkeypatch.setattr(config_mod, "find_config", lambda: cfg_path)
    setup_cmds.cmd_steam_controller(SimpleNamespace(state=None, wake="off"))
    out = capsys.readouterr().out
    assert "steam_controller.wake = off" in out
    saved = config_mod.load(cfg_path).global_.steam_controller
    assert saved.wake is False
    assert saved.enabled is True                         # untouched
    # positional + --wake combined
    setup_cmds.cmd_steam_controller(SimpleNamespace(state="off", wake="on"))
    saved = config_mod.load(cfg_path).global_.steam_controller
    assert (saved.enabled, saved.wake) == (False, True)


def test_steam_controller_subcommand_no_config(monkeypatch):
    monkeypatch.setattr(config_mod, "find_config", lambda: None)
    with pytest.raises(SystemExit):
        setup_cmds.cmd_steam_controller(SimpleNamespace(state="on", wake=None))


def test_hidraw_scan_lists_nodes(monkeypatch, capsys):
    from lgtvcompanion.cli import setup_cmds as sc
    from lgtvcompanion.daemon import hidraw as hraw
    monkeypatch.setattr("glob.glob", lambda pat: ["/dev/hidraw3", "/dev/hidraw4"])
    monkeypatch.setattr(sc.os, "access", lambda p, mode: True)
    info = {"/dev/hidraw3": (0x28DE, 0x1302, "Steam Controller"),
            "/dev/hidraw4": (0x045E, 0x028E, "X-Box pad")}
    monkeypatch.setattr(hraw, "hidraw_info", lambda p: info[p])
    rc = sc.cmd_hidraw_scan(SimpleNamespace(seconds=0.0))
    out = capsys.readouterr().out
    assert rc == 0
    assert "28de:1302" in out and "Valve" in out       # the Steam Controller
    assert "045e:028e" in out                          # the other pad, listed too
    assert "--seconds" in out                          # hint to capture


def test_hidraw_scan_no_nodes(monkeypatch, capsys):
    from lgtvcompanion.cli import setup_cmds as sc
    monkeypatch.setattr("glob.glob", lambda pat: [])
    assert sc.cmd_hidraw_scan(SimpleNamespace(seconds=0.0)) == 0
    assert "no /dev/hidraw" in capsys.readouterr().out
