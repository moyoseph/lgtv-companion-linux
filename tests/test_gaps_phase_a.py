"""Phase A coverage-gap tests: small, scattered branches reachable with the
existing harness (no Qt, no real D-Bus)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from .fake_bus import FakeBus, Reply
from .harness import FakeSession, short_sock

# ---------------------------------------------------------------------------
# ssap/commands.py
# ---------------------------------------------------------------------------


def test_int_and_enum_arg_reject_bad_tokens():
    from lgtvcompanion.ssap.commands import EnumArg, IntArg
    with pytest.raises(ValueError, match="integer"):
        IntArg("v", 0, 9).parse("abc")
    with pytest.raises(ValueError, match="not one of"):
        EnumArg("v", ("a", "b")).parse("z")


def test_build_request_start_app_with_param():
    from lgtvcompanion.ssap.commands import COMMANDS, build_request
    uri, payload = build_request(COMMANDS["start_app_with_param"],
                                 ["netflix", {"k": 1}])
    assert payload == {"id": "netflix", "params": {"k": 1}}


def test_build_luna_raw_curvature_flat_and_unknown():
    from lgtvcompanion.ssap.commands import (
        COMMANDS,
        Command,
        Kind,
        build_luna_raw,
    )
    calls = build_luna_raw(COMMANDS["set_curvature"], ["flat"])
    assert calls[0][1]["type"] == "flat"
    with pytest.raises(ValueError, match="no luna builder"):
        build_luna_raw(Command(name="nope", kind=Kind.LUNA_RAW), [])


def test_is_valid_button():
    from lgtvcompanion.ssap.keys import is_valid_button
    assert is_valid_button("home") is True
    assert is_valid_button("not-a-button") is False


# ---------------------------------------------------------------------------
# ssap/luna.py — createAlert with no alertId
# ---------------------------------------------------------------------------


async def test_luna_send_warns_without_alert_id(tv, client, monkeypatch, caplog):
    import logging

    from lgtvcompanion.ssap import luna

    async def no_alert(uri, payload=None, **kw):
        return {}                              # no alertId

    monkeypatch.setattr(client, "request", no_alert)
    with caplog.at_level(logging.WARNING, logger="lgtvcompanion.ssap.luna"):
        await luna.luna_send(client, "luna://x", {})
    assert "no alertId" in caplog.text


# ---------------------------------------------------------------------------
# activity.py
# ---------------------------------------------------------------------------


def test_parse_ignored_keys_digit_string():
    from lgtvcompanion.activity import parse_ignored_keys
    assert parse_ignored_keys(["42"]) == {42}


def test_activity_filter_ignores_unknown_event_type():
    from lgtvcompanion.activity import ActivityFilter
    assert ActivityFilter().is_activity("dev", 99, 0, 0) is False


# ---------------------------------------------------------------------------
# mqtt/discovery.py parse_command
# ---------------------------------------------------------------------------


def test_parse_command_screen_and_bad_volume():
    from lgtvcompanion.mqtt.discovery import parse_command
    assert parse_command("lgtvc", "lgtvc/tv1/set/screen", "ON") == \
        ("screenon", [], ["tv1"])
    assert parse_command("lgtvc", "lgtvc/tv1/set/screen", "off") == \
        ("screenoff", [], ["tv1"])
    assert parse_command("lgtvc", "lgtvc/tv1/set/volume", "abc") is None


# ---------------------------------------------------------------------------
# mqtt/main.py
# ---------------------------------------------------------------------------


def _mqtt_config(tmp_path, enabled):
    from lgtvcompanion import config as config_mod
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="h")])
    cfg.global_.mqtt.enabled = enabled
    path = tmp_path / "config.json"
    config_mod.save(cfg, path)
    return path


def test_mqtt_main_exits_without_config(monkeypatch):
    from lgtvcompanion import config as config_mod
    from lgtvcompanion.mqtt import main as mqtt_main
    monkeypatch.setattr(config_mod, "find_config", lambda: None)
    with pytest.raises(SystemExit):
        mqtt_main.main()


def test_mqtt_main_exits_when_disabled(monkeypatch, tmp_path):
    from lgtvcompanion import config as config_mod
    from lgtvcompanion.mqtt import main as mqtt_main
    path = _mqtt_config(tmp_path, enabled=False)
    monkeypatch.setattr(config_mod, "find_config", lambda: path)
    with pytest.raises(SystemExit):
        mqtt_main.main()


def test_mqtt_main_missing_aiomqtt(monkeypatch):
    import sys
    from lgtvcompanion.mqtt import main as mqtt_main
    monkeypatch.setitem(sys.modules, "aiomqtt", None)   # import -> ImportError
    with pytest.raises(SystemExit, match="aiomqtt"):
        mqtt_main.main()


def test_mqtt_main_swallows_keyboard_interrupt(monkeypatch, tmp_path):
    from lgtvcompanion import config as config_mod
    from lgtvcompanion.mqtt import main as mqtt_main
    path = _mqtt_config(tmp_path, enabled=True)
    monkeypatch.setattr(config_mod, "find_config", lambda: path)

    def boom(_coro):
        _coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(mqtt_main.asyncio, "run", boom)
    mqtt_main.main()                            # returns cleanly


async def test_mqtt_run_retry_loop(monkeypatch):
    from lgtvcompanion.mqtt import main as mqtt_main
    monkeypatch.setattr(mqtt_main, "RECONNECT_DELAY", 0.01)
    state = {"n": 0}
    reran = asyncio.Event()

    class FakeBridge:
        def __init__(self, cfg, sock):
            pass

        async def run(self):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("disconnected")
            reran.set()
            await asyncio.sleep(10)             # park on the 2nd run

    monkeypatch.setattr(mqtt_main, "Bridge", FakeBridge)
    task = asyncio.create_task(mqtt_main._run(SimpleNamespace(), "/tmp/s"))
    try:
        await asyncio.wait_for(reran.wait(), timeout=2)
        assert state["n"] == 2                  # re-looped after the failure
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# daemon/streams.py error/edge branches
# ---------------------------------------------------------------------------


def _stream_cfg(**kw):
    from lgtvcompanion.config import RemoteStreamConfig
    cfg = RemoteStreamConfig(enabled=True)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


async def test_stream_connect_action_error():
    from lgtvcompanion.daemon.streams import StreamController
    s = FakeSession("tv1", fail=("blank",))
    c = StreamController(_stream_cfg(on_connect="blank"), lambda: [s])
    result = await c.on_connect()
    assert "error" in result["tv1"]


async def test_stream_disconnect_while_not_connected():
    from lgtvcompanion.daemon.streams import StreamController
    c = StreamController(_stream_cfg(), lambda: [FakeSession("tv1")])
    assert await c.on_disconnect() == {"streaming": "not-connected"}


async def test_stream_disconnect_power_on_error():
    from lgtvcompanion.daemon.streams import StreamController
    s = FakeSession("tv1", fail=("on",))
    c = StreamController(_stream_cfg(on_connect="blank", on_disconnect="on"),
                         lambda: [s])
    await c.on_connect()
    result = await c.on_disconnect()
    assert "error" in result["tv1"]


# ---------------------------------------------------------------------------
# daemon/vetoes.py
# ---------------------------------------------------------------------------


def _idle_cfg(**kw):
    base = dict(veto_fullscreen=False, veto_mpris="off", process_list=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_running_processes_scans_proc(tmp_path, monkeypatch):
    from lgtvcompanion.daemon import vetoes
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    (proc / "123" / "comm").write_text("firefox\n")
    (proc / "456").mkdir()
    (proc / "456" / "comm").mkdir()            # a dir -> read_text OSError, skipped
    real = vetoes.Path

    class PathShim:
        def __call__(self, p):
            return proc if p == "/proc" else real(p)

    monkeypatch.setattr(vetoes, "Path", PathShim())
    assert "firefox" in vetoes.running_processes()


async def test_veto_process_list_empty_pattern():
    from lgtvcompanion.daemon.vetoes import VetoEngine
    v = VetoEngine(_idle_cfg(process_list=[{"match": "", "flags": ["running"]}]))
    assert await v.check() is None


async def test_veto_logind_list_inhibitors_error():
    from lgtvcompanion.daemon.vetoes import VetoEngine

    class BadManager:
        async def call_list_inhibitors(self):
            raise RuntimeError("dbus boom")

    v = VetoEngine(_idle_cfg(), logind_manager=BadManager())
    assert await v.check() is None


async def test_veto_logind_no_idle_block_inhibitor():
    from lgtvcompanion.daemon.vetoes import VetoEngine

    class Manager:
        async def call_list_inhibitors(self):
            return [("sleep", "who", "why", "delay", 0, 0)]

    v = VetoEngine(_idle_cfg(), logind_manager=Manager())
    assert await v.check() is None


# ---------------------------------------------------------------------------
# daemon/idle.py
# ---------------------------------------------------------------------------


def _engine(monkeypatch, **kw):
    from lgtvcompanion.daemon.idle import IdleEngine
    from lgtvcompanion.daemon.vetoes import VetoEngine
    v = VetoEngine(_idle_cfg(**kw.pop("veto", {})))
    if "veto_state" in kw:
        v.update_agent_state(kw.pop("veto_state"))

    async def noop():
        pass

    e = IdleEngine(minutes=1, vetoes=v,
                   on_idle=kw.get("on_idle", noop), on_busy=kw.get("on_busy", noop))
    return e


async def test_idle_go_idle_and_go_busy_early_returns(monkeypatch):
    e = _engine(monkeypatch)
    await e._go_idle()
    await e._go_idle()                          # already idle -> early return (81)
    assert e.is_idle
    await e._go_busy()
    await e._go_busy()                          # not idle -> early return (91)
    assert not e.is_idle


async def test_idle_action_exceptions_are_logged(monkeypatch):
    async def boom():
        raise RuntimeError("action failed")

    e = _engine(monkeypatch, on_idle=boom, on_busy=boom)
    await e.force_idle()                        # on_idle raises -> logged (86-87)
    await e.force_unidle()                      # on_busy raises -> logged (97-98)


async def test_idle_loop_veto_recheck(monkeypatch):
    from lgtvcompanion.daemon import idle as idle_mod
    monkeypatch.setattr(idle_mod, "VETO_RECHECK_S", 0.02)
    e = _engine(monkeypatch, veto={"veto_fullscreen": True},
                veto_state={"fullscreen": "mpv"})
    e.timeout_s = 0.0                           # idle immediately, but vetoed
    e.start()
    try:
        await asyncio.sleep(0.1)
        assert not e.is_idle                    # veto kept it awake (76)
    finally:
        e.stop()


async def test_idle_loop_stays_asleep_when_idle(monkeypatch):
    e = _engine(monkeypatch)
    e.timeout_s = 0.0
    e.start()
    try:
        for _ in range(200):
            if e.is_idle:
                break
            await asyncio.sleep(0.01)
        assert e.is_idle
        await asyncio.sleep(1.1)                # loop takes the is_idle branch (67)
        assert e.is_idle
    finally:
        e.stop()


# ---------------------------------------------------------------------------
# daemon/network.py
# ---------------------------------------------------------------------------


def test_has_route_bad_host_is_false():
    from lgtvcompanion.daemon import network
    assert network._has_route("no such host at all") is False


async def test_wait_for_network_retries_then_succeeds(monkeypatch):
    from lgtvcompanion.daemon import network
    calls = {"n": 0}

    def flaky(host):
        calls["n"] += 1
        return calls["n"] > 1                   # False once, then True

    monkeypatch.setattr(network, "_has_route", flaky)
    assert await network.wait_for_network("h", timeout=5, interval=0.01) is True
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# daemon/server.py edge branches
# ---------------------------------------------------------------------------


async def test_server_unlinks_stale_socket():
    import socket as socket_mod

    from lgtvcompanion.daemon.server import IpcServer
    path = short_sock()
    stale = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    stale.bind(path)
    stale.close()                               # leaves the socket file behind
    server = IpcServer(path, lambda *a: None)
    await server.start()                        # must unlink + rebind (33)
    await server.stop()


async def test_server_stop_closes_leftover_subscriber():
    from lgtvcompanion.daemon.server import IpcServer

    class StubWriter:                           # hashable (identity), unlike SimpleNamespace
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    server = IpcServer(short_sock(), lambda *a: None)
    await server.start()
    w = StubWriter()
    server._subscribers.add(w)
    await server.stop()                         # closes the stub subscriber (46)
    assert w.closed


def test_server_broadcast_evicts_dead_writer():
    from lgtvcompanion.daemon.server import IpcServer
    server = IpcServer(short_sock(), lambda *a: None)

    class DeadWriter:
        def write(self, data):
            raise ConnectionError("gone")

    dead = DeadWriter()
    server._subscribers.add(dead)
    server.broadcast("SYSTEM_SUSPEND")          # write raises -> evicted (56-59)
    assert dead not in server._subscribers


# ---------------------------------------------------------------------------
# agent/update.py
# ---------------------------------------------------------------------------


def test_installed_version_package_not_found(monkeypatch):
    from lgtvcompanion.agent import update
    monkeypatch.setattr(update.metadata, "version",
                        lambda name: (_ for _ in ()).throw(
                            update.metadata.PackageNotFoundError()))
    assert update._installed_version() == "0"


async def test_update_checker_notifies_when_newer(monkeypatch):
    from lgtvcompanion.agent import update
    monkeypatch.setattr(update, "_fetch_latest_tag", lambda: "v99.0.0")
    monkeypatch.setattr(update, "CHECK_INTERVAL", 0.01)
    notified = asyncio.Event()

    uc = update.UpdateChecker(None)

    async def fake_notify(summary, body):
        notified.set()

    uc._notify = fake_notify
    uc.start()
    try:
        await asyncio.wait_for(notified.wait(), timeout=2)
    finally:
        uc.stop()


async def test_update_notify_sends_and_swallows_errors():
    from lgtvcompanion.agent import update
    sent = []
    ok = update.UpdateChecker(FakeBus({"Notify": lambda m: sent.append(m) or Reply()}))
    await ok._notify("summary", "body")
    assert sent                                 # real Notify call issued (86-92)

    def raising(_msg):
        raise RuntimeError("no notifications daemon")

    bad = update.UpdateChecker(FakeBus({"Notify": raising}))
    await bad._notify("summary", "body")        # swallowed (93-94)


# ---------------------------------------------------------------------------
# agent/mpris.py lazy bus connect
# ---------------------------------------------------------------------------


async def test_mpris_get_bus_lazy_connects(monkeypatch):
    from lgtvcompanion.agent import mpris
    fake = FakeBus()

    class FakeMessageBus:
        def __init__(self, *a, **k):
            pass

        async def connect(self):
            return fake

    monkeypatch.setattr(mpris, "MessageBus", FakeMessageBus)
    monkeypatch.setattr(mpris, "_bus", None)
    got = await mpris._get_bus()
    assert got is fake


def test_mqtt_main_offline_refuses_cloud_broker(monkeypatch, tmp_path):
    from lgtvcompanion import config as config_mod
    from lgtvcompanion.mqtt import main as mqtt_main
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="h")])
    cfg.global_.mqtt.enabled = True
    cfg.global_.mqtt.host = "broker.example.com"    # public
    cfg.global_.offline_mode = True
    path = tmp_path / "config.json"
    config_mod.save(cfg, path)
    monkeypatch.setattr(config_mod, "find_config", lambda: path)
    with pytest.raises(SystemExit, match="offline_mode"):
        mqtt_main.main()


def test_mqtt_main_offline_allows_lan_broker(monkeypatch, tmp_path):
    from lgtvcompanion import config as config_mod
    from lgtvcompanion.mqtt import main as mqtt_main
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="h")])
    cfg.global_.mqtt.enabled = True
    cfg.global_.mqtt.host = "10.0.0.9"              # LAN broker is fine offline
    cfg.global_.offline_mode = True
    path = tmp_path / "config.json"
    config_mod.save(cfg, path)
    monkeypatch.setattr(config_mod, "find_config", lambda: path)
    ran = {}
    monkeypatch.setattr(mqtt_main.asyncio, "run",
                        lambda coro: (coro.close(), ran.setdefault("ran", True)))
    mqtt_main.main()                                # passes the guard, reaches run
    assert ran.get("ran") is True
