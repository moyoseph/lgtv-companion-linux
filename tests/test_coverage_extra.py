"""Focused unit tests for the pure-logic helpers (output formatting, WoL packet
targeting, network readiness, sd_notify, update-check, config validation) that
don't need a TV, D-Bus, or systemd."""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from lgtvcompanion import config as config_mod
from lgtvcompanion import sdnotify
from lgtvcompanion.agent import update
from lgtvcompanion.cli.output import _find_key, format_result
from lgtvcompanion.daemon import network
from lgtvcompanion.ssap import wol as wol_mod

# -- cli/output ---------------------------------------------------------------


def test_format_default_is_compact():
    assert format_result({"a": 1, "b": 2}) == '{"a":1,"b":2}'


def test_format_friendly_is_indented():
    out = format_result({"a": 1}, "friendly")
    assert "\n" in out and '"a": 1' in out


def test_format_key_scalar():
    assert format_result({"x": {"y": 5}}, "key", "y") == "5"


def test_format_key_container_is_compact_json():
    assert format_result({"x": {"y": 5}}, "key", "x") == '{"y":5}'
    assert format_result({"x": [1, 2]}, "key", "x") == "[1,2]"


def test_format_key_missing_returns_empty_string():
    assert format_result({"a": 1}, "key", "nope") == ""


def test_find_key_depth_first_through_lists():
    assert _find_key([{"a": 1}, {"deep": {"target": 9}}], "target") == 9
    assert _find_key({"a": {"b": 1}}, "missing") is None


# -- ssap/wol -----------------------------------------------------------------


class _FakeSock:
    def __init__(self, sends, binds):
        self._sends, self._binds = sends, binds

    def setsockopt(self, *a):
        pass

    def bind(self, addr):
        self._binds.append(addr)

    def sendto(self, data, addr):
        self._sends.append((data, addr))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_socket(monkeypatch):
    sends: list = []
    binds: list = []
    monkeypatch.setattr(wol_mod.socket, "socket",
                        lambda *a, **k: _FakeSock(sends, binds))
    return sends, binds


def test_send_wol_broadcast_hits_global(monkeypatch):
    sends, _ = _patch_socket(monkeypatch)
    wol_mod.send_wol(["aa:bb:cc:dd:ee:ff"], "1.2.3.4", method="broadcast")
    assert [addr for _, addr in sends] == [("255.255.255.255", wol_mod.WOL_PORT)]


def test_send_wol_auto_covers_broadcast_subnet_and_unicast(monkeypatch):
    sends, _ = _patch_socket(monkeypatch)
    wol_mod.send_wol(["aa:bb:cc:dd:ee:ff"], "1.2.3.4",
                     method="auto", subnet_override="1.2.3.255")
    targets = {addr[0] for _, addr in sends}
    assert targets == {"255.255.255.255", "1.2.3.255", "1.2.3.4"}


def test_send_wol_dedupes_repeated_targets(monkeypatch):
    sends, _ = _patch_socket(monkeypatch)
    # subnet_override == tv_ip collapses two targets into one
    wol_mod.send_wol(["aa:bb:cc:dd:ee:ff"], "1.2.3.4",
                     method="auto", subnet_override="1.2.3.4")
    assert len(sends) == 2  # 255.255.255.255 + 1.2.3.4


def test_send_wol_extra_targets_appended(monkeypatch):
    sends, _ = _patch_socket(monkeypatch)
    wol_mod.send_wol(["aa:bb:cc:dd:ee:ff"], "1.2.3.4",
                     method="directed", extra_targets=["9.9.9.255"])
    assert {addr[0] for _, addr in sends} == {"1.2.3.4", "9.9.9.255"}


def test_send_wol_binds_to_interface_source_ip(monkeypatch):
    sends, binds = _patch_socket(monkeypatch)
    monkeypatch.setattr(wol_mod, "interface_ip", lambda name: "9.9.9.9")
    wol_mod.send_wol(["aa:bb:cc:dd:ee:ff"], "1.2.3.4",
                     method="directed", interface="eth0")
    assert binds == [("9.9.9.9", 0)]


def test_subnet_and_interface_fall_back_off_linux(monkeypatch):
    monkeypatch.setattr(wol_mod.sys, "platform", "darwin")
    assert wol_mod.subnet_broadcast("1.2.3.4") == "255.255.255.255"
    assert wol_mod.interface_ip("eth0") is None


def test_interface_ip_empty_name_is_none():
    assert wol_mod.interface_ip("") is None


async def test_wol_burst_stops_when_event_set(monkeypatch):
    calls: list = []
    monkeypatch.setattr(wol_mod, "send_wol", lambda *a, **k: calls.append(1))
    stop = asyncio.Event()
    stop.set()
    await wol_mod.wol_burst(["aa:bb:cc:dd:ee:ff"], "1.2.3.4",
                            stop=stop, interval=0.01)
    assert len(calls) == 1


async def test_wol_burst_honours_duration(monkeypatch):
    calls: list = []
    monkeypatch.setattr(wol_mod, "send_wol", lambda *a, **k: calls.append(1))
    await wol_mod.wol_burst(["aa:bb:cc:dd:ee:ff"], "1.2.3.4",
                            duration=0.02, interval=0.01)
    assert len(calls) >= 1


# -- daemon/network -----------------------------------------------------------


def test_has_route_to_loopback():
    assert network._has_route("127.0.0.1") is True


async def test_wait_for_network_returns_true_when_reachable(monkeypatch):
    monkeypatch.setattr(network, "_has_route", lambda h: True)
    assert await network.wait_for_network("1.2.3.4") is True


async def test_wait_for_network_gives_up_after_timeout(monkeypatch):
    monkeypatch.setattr(network, "_has_route", lambda h: False)
    assert await network.wait_for_network(
        "1.2.3.4", timeout=0.0, interval=0.01) is False


# -- sdnotify -----------------------------------------------------------------


def test_sdnotify_sends_over_unix_socket(monkeypatch):
    # AF_UNIX paths are length-capped (~104 chars), so avoid pytest's deep
    # tmp_path and use a short unique path in the system temp dir.
    import os
    import tempfile
    sock_path = os.path.join(tempfile.gettempdir(), f"lgtvc-{os.getpid()}.sock")
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(sock_path)
    srv.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
    try:
        sdnotify.ready()
        assert srv.recv(64) == b"READY=1"
        sdnotify.stopping()
        assert srv.recv(64) == b"STOPPING=1"
        sdnotify.status("hello")
        assert srv.recv(64) == b"STATUS=hello"
    finally:
        srv.close()
        if os.path.exists(sock_path):
            os.unlink(sock_path)


def test_sdnotify_is_noop_without_env(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    sdnotify.notify("READY=1")  # must not raise


def test_sdnotify_swallows_dead_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "does-not-exist.sock"))
    sdnotify.ready()  # connect fails → swallowed, no raise


# -- agent/update -------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_latest_tag_parses_json(monkeypatch):
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda req, timeout=0: _FakeResp(b'{"tag_name": "v9.9.9"}'))
    assert update._fetch_latest_tag() == "v9.9.9"


def test_fetch_latest_tag_returns_none_on_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(update.urllib.request, "urlopen", boom)
    assert update._fetch_latest_tag() is None


def test_parse_and_is_newer_edges():
    assert update._parse("v1.2.3") == (1, 2, 3)
    assert update._parse("2.0") == (2, 0)
    assert update.is_newer("v0.3.0", "0.2.9") is True
    assert update.is_newer("v0.2.0", "0.2.0") is False


def test_installed_version_is_a_string():
    v = update._installed_version()
    assert isinstance(v, str) and v


async def test_update_notify_without_bus_is_noop():
    checker = update.UpdateChecker(bus=None)
    await checker._notify("summary", "body")  # bus is None → returns quietly


def test_update_checker_stop_without_task():
    update.UpdateChecker(bus=None).stop()  # no task → must not raise


# -- config validation --------------------------------------------------------


def _cfg():
    return config_mod.Config(
        devices=[config_mod.DeviceConfig(id="tv1", host="192.0.2.10")])


def test_validate_flags_bad_enums():
    cfg = _cfg()
    cfg.global_.on_lock = "explode"
    cfg.global_.on_unlock = "maybe"
    cfg.global_.idle.action = "vanish"
    cfg.global_.idle.veto_mpris = "sometimes"
    cfg.global_.update_check = "always"
    joined = " ".join(config_mod.validate(cfg))
    for token in ("on_lock", "on_unlock", "idle.action",
                  "veto_mpris", "update_check"):
        assert token in joined


def test_validate_flags_out_of_range():
    cfg = _cfg()
    cfg.global_.power_on_timeout = 999
    cfg.global_.idle.minutes = 0
    joined = " ".join(config_mod.validate(cfg))
    assert "power_on_timeout" in joined and "idle.minutes" in joined


def test_validate_flags_duplicate_ids():
    cfg = config_mod.Config(devices=[
        config_mod.DeviceConfig(id="tv1", host="192.0.2.10"),
        config_mod.DeviceConfig(id="tv1", host="192.0.2.11"),
    ])
    assert any("duplicate" in p for p in config_mod.validate(cfg))


def test_load_rejects_bad_enum(tmp_path):
    cfg = _cfg()
    cfg.global_.idle.action = "vanish"
    path = tmp_path / "config.json"
    config_mod.save(cfg, path)
    with pytest.raises(ValueError, match="idle.action"):
        config_mod.load(path)


def test_unknown_keys_are_tolerated(tmp_path):
    cfg = _cfg()
    path = tmp_path / "config.json"
    config_mod.save(cfg, path)
    data = json.loads(path.read_text())
    data["mystery_root_key"] = 42
    data["devices"][0]["mystery_device_key"] = "surprise"
    path.write_text(json.dumps(data))
    loaded = config_mod.load(path)  # unknown keys warned + dropped, not fatal
    assert loaded.devices[0].host == "192.0.2.10"


def test_valid_config_has_no_problems():
    assert config_mod.validate(_cfg()) == []
