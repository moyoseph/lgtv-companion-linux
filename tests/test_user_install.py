from __future__ import annotations

from types import SimpleNamespace

import pytest

from lgtvcompanion import ipc
from lgtvcompanion.cli import setup_cmds


def test_default_socket_prefers_user_runtime_when_no_system_socket(monkeypatch, tmp_path):
    monkeypatch.delenv("LGTVC_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    # system socket doesn't exist → user runtime path
    monkeypatch.setattr(ipc.os.path, "exists", lambda p: False)
    assert ipc.default_socket() == f"{tmp_path}/lgtv-companion/ipc.sock"


def test_default_socket_env_wins(monkeypatch):
    monkeypatch.setenv("LGTVC_SOCKET", "/tmp/custom.sock")
    assert ipc.default_socket() == "/tmp/custom.sock"


def test_default_socket_system_when_present(monkeypatch):
    monkeypatch.delenv("LGTVC_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(ipc.os.path, "exists", lambda p: True)
    assert ipc.default_socket() == ipc.DEFAULT_SOCKET


def test_user_install_renders_user_units(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup_cmds.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(setup_cmds.os, "geteuid", lambda: 1000)  # not root
    monkeypatch.setattr(setup_cmds, "_python_bin", lambda: "/venv/bin/python3")
    monkeypatch.setattr(setup_cmds, "_tray_available", lambda p: False)
    monkeypatch.setattr(setup_cmds, "_module_available", lambda p, m: False)
    ran = []
    monkeypatch.setattr(setup_cmds, "_run", lambda cmd, check=True: ran.append(cmd))

    rc = setup_cmds.cmd_install(SimpleNamespace(mode="user", service_user="me"))
    assert rc == 0
    unit = home / ".config" / "systemd" / "user" / "lgtvc-daemon.service"
    text = unit.read_text()
    assert "python3 -m lgtvcompanion.daemon.main" in text
    assert "WantedBy=default.target" in text
    # daemon_power_events seeded true in the user config
    from lgtvcompanion import config as config_mod
    cfg = config_mod.load(config_mod.user_config_path())
    assert cfg.global_.daemon_power_events is True
    # enabled via systemctl --user
    assert any("--user" in c and "enable" in c for c in ran)


def test_user_install_refuses_root(monkeypatch):
    monkeypatch.setattr(setup_cmds.os, "geteuid", lambda: 0)
    with pytest.raises(SystemExit):
        setup_cmds.cmd_install(SimpleNamespace(mode="user", service_user="root"))
