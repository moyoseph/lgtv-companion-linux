from __future__ import annotations

import json

from lgtvcompanion.config import MqttConfig
from lgtvcompanion.mqtt import discovery as disc
from lgtvcompanion.mqtt.bridge import Bridge, state_topics_for


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
