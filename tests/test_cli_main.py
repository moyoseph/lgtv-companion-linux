"""lgtvc --direct execution against the FakeTv, and the main() entry-point
routing matrix."""

from __future__ import annotations

import os

import pytest

from lgtvcompanion import config as config_mod
from lgtvcompanion.cli import main as cli_main
from lgtvcompanion.config import DeviceConfig
from lgtvcompanion.ssap.handshake import KeyStore

from .fake_tv import VALID_KEY
from .harness import short_sock


def _patch_direct(monkeypatch, tv, tmp_path, *, save_key=True):
    """Point run_direct's DeviceSessions at the FakeTv and its keystore at tmp."""
    import lgtvcompanion.daemon.devices as dev_mod

    real = dev_mod.DeviceSession

    class Patched(real):
        def __init__(self, cfg, keystore, **kw):
            cfg.ssl = False                     # FakeTv speaks plain ws
            super().__init__(cfg, keystore, **kw)
            self.client.port = tv.port
            orig = self._new_client

            def patched():
                c = orig()
                c.port = tv.port
                return c

            self._new_client = patched

    monkeypatch.setattr(dev_mod, "DeviceSession", Patched)
    monkeypatch.setattr(config_mod, "SYSTEM_STATE", tmp_path / "no-sys-state")
    monkeypatch.setattr(config_mod, "user_state_path", lambda: tmp_path / "state")
    if save_key:
        KeyStore(tmp_path / "state" / "keys").save("tv1", VALID_KEY)


def _cfg():
    return config_mod.Config(devices=[DeviceConfig(
        id="tv1", host="127.0.0.1", ssl=False, timeout=3.0)])


# -- run_direct -----------------------------------------------------------------


async def test_run_direct_config_branch(tv, tmp_path, monkeypatch, capsys):
    _patch_direct(monkeypatch, tv, tmp_path)
    inv = cli_main.parse_tokens(["-volume", "25"])
    rc = await cli_main.run_direct(inv, _cfg(), None, None)
    assert rc == 0
    assert '"volume":25' in capsys.readouterr().out


async def test_run_direct_adhoc_host_and_key(tv, tmp_path, monkeypatch, capsys):
    _patch_direct(monkeypatch, tv, tmp_path, save_key=False)
    inv = cli_main.parse_tokens(["-volume", "7"])
    rc = await cli_main.run_direct(inv, None, "127.0.0.1", VALID_KEY)
    assert rc == 0
    assert '"volume":7' in capsys.readouterr().out


async def test_run_direct_meta_needs_daemon(tv, tmp_path, monkeypatch, capsys):
    _patch_direct(monkeypatch, tv, tmp_path)
    rc = await cli_main.run_direct(
        cli_main.parse_tokens(["-idle"]), _cfg(), None, None)
    assert rc == 1
    assert "needs the daemon" in capsys.readouterr().err


async def test_run_direct_reports_tv_error(tv, tmp_path, monkeypatch, capsys):
    _patch_direct(monkeypatch, tv, tmp_path)
    tv.error_uris["audio/setVolume"] = {"errorCode": "500"}
    rc = await cli_main.run_direct(
        cli_main.parse_tokens(["-volume", "9"]), _cfg(), None, None)
    assert rc == 1
    assert "error" in capsys.readouterr().out


async def test_run_direct_no_matching_devices(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_mod, "SYSTEM_STATE", tmp_path / "no-sys-state")
    monkeypatch.setattr(config_mod, "user_state_path", lambda: tmp_path / "state")
    inv = cli_main.parse_tokens(["-poweron", "bogus-device"])
    rc = await cli_main.run_direct(inv, _cfg(), None, None)
    assert rc == 1
    assert "no devices" in capsys.readouterr().err


async def test_run_direct_pairs_when_no_stored_key(tv, tmp_path, monkeypatch, capsys):
    # no stored key -> the FakeTv PROMPT flow completes and the command still runs
    _patch_direct(monkeypatch, tv, tmp_path, save_key=False)
    inv = cli_main.parse_tokens(["-volume", "3"])
    rc = await cli_main.run_direct(inv, _cfg(), None, None)
    assert rc == 0
    assert '"volume":3' in capsys.readouterr().out


# -- main() routing matrix ---------------------------------------------------------


def test_main_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        cli_main.main([])
    assert e.value.code == 0
    assert "Commands:" in capsys.readouterr().out


def test_main_unknown_command_exits_two(capsys):
    with pytest.raises(SystemExit) as e:
        cli_main.main(["-frobnicate"])
    assert e.value.code == 2
    assert "unknown command" in capsys.readouterr().err


def test_main_dispatches_setup(monkeypatch):
    import lgtvcompanion.cli.setup_cmds as setup_cmds
    seen: list = []
    monkeypatch.setattr(setup_cmds, "main", lambda argv: seen.append(argv) or 0)
    with pytest.raises(SystemExit) as e:
        cli_main.main(["setup", "show"])
    assert e.value.code == 0
    assert seen == [["show"]]


def test_main_events_and_status_route(monkeypatch):
    seen: list = []

    async def fake_events(sock):
        seen.append(("events", sock))
        return 0

    async def fake_verb(verb, sock):
        seen.append((verb, sock))
        return 0

    monkeypatch.setattr(cli_main, "run_events", fake_events)
    monkeypatch.setattr(cli_main, "run_daemon_verb", fake_verb)
    with pytest.raises(SystemExit):
        cli_main.main(["events", "--socket", "/tmp/x.sock"])
    with pytest.raises(SystemExit):
        cli_main.main(["status", "--socket", "/tmp/x.sock"])
    assert seen == [("events", "/tmp/x.sock"), ("status", "/tmp/x.sock")]


def test_main_routes_to_daemon_when_socket_exists(monkeypatch, tmp_path):
    sock = short_sock()
    open(sock, "w").close()                    # os.path.exists() is the check
    seen: list = []

    async def fake_via_daemon(inv, socket_path):
        seen.append(socket_path)
        return 0

    monkeypatch.setattr(cli_main, "run_via_daemon", fake_via_daemon)
    monkeypatch.setattr(config_mod, "find_config", lambda: None)
    with pytest.raises(SystemExit) as e:
        cli_main.main(["-poweron", "--socket", sock])
    assert e.value.code == 0
    assert seen == [sock]
    os.unlink(sock)


def test_main_routes_direct_with_config_and_wait_network(
        monkeypatch, tmp_path):
    # --wait-network resolves the first configured host (127.0.0.1 -> instant),
    # then the absence of a daemon socket routes to run_direct.
    path = tmp_path / "config.json"
    config_mod.save(_cfg(), path)
    seen: list = []

    async def fake_direct(inv, cfg, host, key):
        seen.append((cfg.devices[0].id, host, key))
        return 0

    monkeypatch.setattr(cli_main, "run_direct", fake_direct)
    with pytest.raises(SystemExit) as e:
        cli_main.main(["--wait-network", "--config", str(path),
                       "--socket", str(tmp_path / "absent.sock"), "-poweron"])
    assert e.value.code == 0
    assert seen == [("tv1", None, None)]


async def test_run_direct_meta_stops_chain(tv, tmp_path, monkeypatch, capsys):
    _patch_direct(monkeypatch, tv, tmp_path)
    # -idle is META (needs the daemon); it must stop the chain before -mute runs
    rc = await cli_main.run_direct(
        cli_main.parse_tokens(["-idle", "-mute"]), _cfg(), None, None)
    assert rc == 1
    assert "needs the daemon" in capsys.readouterr().err
    assert not any(uri == "audio/setMute" for uri, _ in tv.requests)
