"""Unit tests for `lgtvc setup` — systemd unit rendering and the simpler
subcommands, all mocked so nothing touches the real system."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from lgtvcompanion import config as config_mod
from lgtvcompanion.cli import setup_cmds


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
