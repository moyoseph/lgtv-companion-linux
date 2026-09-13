from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from types import SimpleNamespace

import pytest

from lgtvcompanion.config import MqttConfig
from lgtvcompanion.daemon.server import IpcServer
from lgtvcompanion.mqtt import bridge as bridge_mod
from lgtvcompanion.mqtt import discovery as disc
from lgtvcompanion.mqtt import main as mqtt_main
from lgtvcompanion.mqtt.bridge import Bridge, state_topics_for

from .harness import short_sock


# --- pure command parsing ----------------------------------------------------

def test_parse_command_power():
    assert disc.parse_command("lgtvc", "lgtvc/tv1/set/power", "ON") == \
        ("poweron", [], ["tv1"])
    assert disc.parse_command("lgtvc", "lgtvc/tv1/set/power", "OFF") == \
        ("poweroff", [], ["tv1"])


def test_parse_command_input_and_volume():
    assert disc.parse_command("lgtvc", "lgtvc/tv1/set/input", "3") == \
        ("sethdmi", [3], ["tv1"])
    assert disc.parse_command("lgtvc", "lgtvc/tv1/set/volume", "42") == \
        ("volume", [42], ["tv1"])
    assert disc.parse_command("lgtvc", "lgtvc/tv1/set/input", "notanint") is None


def test_parse_command_idle_is_meta_all_devices():
    assert disc.parse_command("lgtvc", "lgtvc/tv1/set/idle", "ON") == \
        ("idle", [], [])


def test_parse_command_rejects_foreign_topics():
    assert disc.parse_command("lgtvc", "lgtvc/tv1/power", "ON") is None       # not a set
    assert disc.parse_command("lgtvc", "other/tv1/set/power", "ON") is None   # prefix
    assert disc.parse_command("lgtvc", "lgtvc/tv1/set/bogus", "x") is None


# --- discovery payloads ------------------------------------------------------

def test_discovery_configs_shape():
    cfgs = disc.discovery_configs("lgtvc", "homeassistant", "tv1", "Living Room")
    topics = {t for t, _ in cfgs}
    assert "homeassistant/switch/lgtvc_tv1_power/config" in topics
    assert "homeassistant/select/lgtvc_tv1_input/config" in topics
    assert "homeassistant/sensor/lgtvc_tv1_state/config" in topics
    for _t, payload in cfgs:
        assert payload["device"]["identifiers"] == ["lgtvc_tv1"]
        assert payload["availability"][0]["topic"] == "lgtvc/tv1/availability"
        assert json.loads(disc.discovery_payload_json(payload))  # serializable
    power = next(p for t, p in cfgs if t.endswith("_power/config"))
    assert power["command_topic"] == "lgtvc/tv1/set/power"
    assert power["state_topic"] == "lgtvc/tv1/power"


# --- state mapping -----------------------------------------------------------

def test_state_topics_for():
    assert state_topics_for({"power_state": "Active", "source_hdmi_input": 4}) == \
        {"power": "ON", "screen": "ON", "state": "Active", "input": "4"}
    assert state_topics_for({"power_state": "Screen Off"}) == \
        {"power": "ON", "screen": "OFF", "state": "Screen Off", "input": ""}
    assert state_topics_for({"power_state": "Active Standby"})["power"] == "OFF"


# --- bridge publish/command with fakes --------------------------------------

class FakeMqtt:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, topic, payload, **kw):
        self.published.append(
            (topic, payload.decode() if isinstance(payload, bytes) else str(payload)))


class FakeIpc:
    def __init__(self, status):
        self._status = status
        self.commands: list[tuple] = []

    async def request(self, cmd, args, devices):
        if cmd == "status":
            return {"ok": True, "results": self._status}
        self.commands.append((cmd, args, devices))
        return {"ok": True, "results": {}}


async def test_bridge_publishes_only_changes():
    b = Bridge(MqttConfig(topic_prefix="lgtvc"), "/tmp/x.sock")
    mqtt = FakeMqtt()
    status = {"idle_active": False,
              "devices": {"tv1": {"name": "TV", "power_state": "Active",
                                  "source_hdmi_input": 4}}}
    await b._publish_state(mqtt, status)
    first = dict(mqtt.published)
    assert first["lgtvc/tv1/power"] == "ON"
    assert first["lgtvc/tv1/state"] == "Active"
    mqtt.published.clear()
    await b._publish_state(mqtt, status)          # unchanged → nothing published
    assert mqtt.published == []
    status["devices"]["tv1"]["power_state"] = "Active Standby"
    await b._publish_state(mqtt, status)
    changed = dict(mqtt.published)
    assert changed["lgtvc/tv1/power"] == "OFF"


async def test_bridge_forwards_command():
    b = Bridge(MqttConfig(topic_prefix="lgtvc"), "/tmp/x.sock")
    ipc = FakeIpc({})
    await b._handle_command(ipc, "lgtvc/tv1/set/power", b"OFF")
    assert ipc.commands == [("poweroff", [], ["tv1"])]
    await b._handle_command(ipc, "lgtvc/tv1/set/bogus", b"x")  # ignored
    assert ipc.commands == [("poweroff", [], ["tv1"])]


def test_mqtt_config_roundtrip(tmp_path):
    from lgtvcompanion import config as config_mod
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="h")])
    cfg.global_.mqtt.enabled = True
    cfg.global_.mqtt.host = "192.168.1.5"
    p = tmp_path / "c.json"
    config_mod.save(cfg, p)
    loaded = config_mod.load(p)
    assert loaded.global_.mqtt.enabled is True
    assert loaded.global_.mqtt.host == "192.168.1.5"


# --- Bridge.run() end-to-end (fake broker, real IPC server) -------------------

class FakeAiomqttClient:
    """Async-context-manager stand-in for aiomqtt.Client: records publishes and
    subscribes; .messages yields one command then blocks until cancelled."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.published: list[tuple[str, object, bool]] = []
        self.subscribed: list[str] = []
        self._blocked = asyncio.Event()  # never set; run_task cancel unblocks
        self.messages = self._message_gen()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    async def subscribe(self, topic):
        self.subscribed.append(topic)

    async def _message_gen(self):
        # bridge does str(message.topic), so a plain str topic matches
        yield SimpleNamespace(topic="lgtvc/tv1/set/power", payload=b"OFF")
        await self._blocked.wait()


async def test_bridge_run_end_to_end(monkeypatch):
    import aiomqtt

    clients: list[FakeAiomqttClient] = []

    def factory(**kwargs):
        c = FakeAiomqttClient(**kwargs)
        clients.append(c)
        return c

    monkeypatch.setattr(aiomqtt, "Client", factory)

    calls: list[tuple] = []

    async def dispatcher(cmd, args, devices):
        calls.append((cmd, args, devices))
        if cmd == "status":
            return {"idle_active": False,
                    "devices": {"tv1": {"name": "TV", "power_state": "Active",
                                        "source_hdmi_input": 4}}}
        return {}

    sock = short_sock()
    server = IpcServer(sock, dispatcher)
    await server.start()
    b = Bridge(MqttConfig(topic_prefix="lgtvc"), sock)
    run_task = asyncio.create_task(b.run())
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while ("poweroff", [], ["tv1"]) not in calls:
            assert loop.time() < deadline, f"command never reached daemon: {calls}"
            await asyncio.sleep(0.01)

        client = clients[0]
        assert client.kwargs["hostname"] == "localhost"
        assert client.kwargs["will"].topic == "lgtvc/bridge/availability"
        assert ("lgtvc/bridge/availability", b"online", True) in client.published
        assert client.subscribed == ["lgtvc/+/set/#"]
        topics = [t for t, _p, _r in client.published]
        assert "homeassistant/switch/lgtvc_tv1_power/config" in topics
        assert ("lgtvc/tv1/availability", b"online", True) in client.published
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=2.0)
        await server.stop()


# --- _announce / _poll_loop / _status / _handle_command edge cases ------------

async def test_announce_publishes_discovery_and_availability():
    b = Bridge(MqttConfig(topic_prefix="lgtvc"), "/tmp/x.sock")
    mqtt = FakeMqtt()
    ipc = FakeIpc({"devices": {"tv1": {"name": "Living Room",
                                       "power_state": "Active"}}})
    await b._announce(mqtt, ipc)
    published = dict(mqtt.published)
    for topic, payload in disc.discovery_configs(
            "lgtvc", "homeassistant", "tv1", "Living Room"):
        assert published[topic] == disc.discovery_payload_json(payload)
    assert published["lgtvc/tv1/availability"] == "online"


async def test_announce_disabled_publishes_nothing():
    b = Bridge(MqttConfig(topic_prefix="lgtvc", discovery=False), "/tmp/x.sock")
    mqtt = FakeMqtt()
    await b._announce(mqtt, FakeIpc({}))
    assert mqtt.published == []


async def test_poll_loop_survives_status_failure(monkeypatch):
    monkeypatch.setattr(bridge_mod, "POLL_INTERVAL", 0.01)

    class FlakyIpc:
        def __init__(self):
            self.calls = 0

        async def request(self, cmd, args, devices):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("ipc down")
            return {"ok": True,
                    "results": {"idle_active": False,
                                "devices": {"tv1": {"power_state": "Active"}}}}

    b = Bridge(MqttConfig(topic_prefix="lgtvc"), "/tmp/x.sock")
    mqtt = FakeMqtt()
    ipc = FlakyIpc()
    task = asyncio.create_task(b._poll_loop(mqtt, ipc))
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while ("lgtvc/tv1/power", "ON") not in mqtt.published:
            assert loop.time() < deadline, "poll loop died after one failure"
            await asyncio.sleep(0.01)
        assert ipc.calls >= 2  # first attempt raised, loop kept going
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)


async def test_status_not_ok_returns_empty():
    class NotOkIpc:
        async def request(self, cmd, args, devices):
            return {"ok": False, "error": "boom"}

    b = Bridge(MqttConfig(), "/tmp/x.sock")
    assert await b._status(NotOkIpc()) == {}


async def test_handle_command_ipc_error_swallowed():
    class BrokenIpc:
        async def request(self, cmd, args, devices):
            raise ConnectionError("daemon gone")

    b = Bridge(MqttConfig(topic_prefix="lgtvc"), "/tmp/x.sock")
    await b._handle_command(BrokenIpc(), "lgtvc/tv1/set/power", b"ON")  # no raise


# --- mqtt/main.py entry point --------------------------------------------------

def test_mqtt_main_exits_without_config(monkeypatch):
    monkeypatch.setattr(mqtt_main.config_mod, "find_config", lambda: None)
    with pytest.raises(SystemExit, match="no config found"):
        mqtt_main.main()


def test_mqtt_main_exits_when_disabled(monkeypatch, tmp_path):
    from lgtvcompanion import config as config_mod
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="h")])
    p = tmp_path / "config.json"
    config_mod.save(cfg, p)  # mqtt.enabled defaults to False
    monkeypatch.setattr(config_mod, "find_config", lambda: p)
    with pytest.raises(SystemExit, match="mqtt.enabled is false"):
        mqtt_main.main()


def test_mqtt_main_socket_override_reaches_run(monkeypatch, tmp_path):
    from lgtvcompanion import config as config_mod
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="h")])
    cfg.global_.mqtt.enabled = True
    p = tmp_path / "config.json"
    config_mod.save(cfg, p)
    monkeypatch.setattr(config_mod, "find_config", lambda: p)
    monkeypatch.setattr(sys, "argv", ["lgtvc-mqtt", "--socket", "/tmp/x"])
    recorded = {}

    async def fake_run(mqtt_cfg, socket_path):
        recorded["cfg"] = mqtt_cfg
        recorded["socket"] = socket_path

    monkeypatch.setattr(mqtt_main, "_run", fake_run)
    mqtt_main.main()  # real asyncio.run drives the faked _run
    assert recorded["socket"] == "/tmp/x"
    assert recorded["cfg"].enabled is True
