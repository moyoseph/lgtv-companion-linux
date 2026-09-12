"""Pure topic + Home Assistant MQTT-discovery helpers (no I/O — unit-tested).

State topics (retained), per device `<prefix>/<id>/…`:
    power         ON | OFF
    state         Active | Screen Off | Active Standby | Unknown
    input         HDMI input number the daemon targets (or "")
    idle          ON | OFF   (daemon idle-mode active)
    availability  online | offline

Command topics `<prefix>/<id>/set/…`:
    power   ON|OFF        -> poweron / poweroff
    screen  ON|OFF        -> screenon / screenoff
    input   1-4           -> sethdmi N
    volume  0-100         -> volume N
    idle    ON|OFF        -> idle / unidle (meta; applies to all)
"""

from __future__ import annotations

import json

BRIDGE = "bridge"


def state_topic(prefix: str, device_id: str, leaf: str) -> str:
    return f"{prefix}/{device_id}/{leaf}"


def availability_topic(prefix: str, device_id: str) -> str:
    return f"{prefix}/{device_id}/availability"


def bridge_availability_topic(prefix: str) -> str:
    return f"{prefix}/{BRIDGE}/availability"


def command_topic(prefix: str, device_id: str, leaf: str) -> str:
    return f"{prefix}/{device_id}/set/{leaf}"


def command_subscription(prefix: str) -> str:
    """Single wildcard covering every device's command topics."""
    return f"{prefix}/+/set/#"


def parse_command(prefix: str, topic: str, payload: str):
    """Map an inbound MQTT command topic+payload to a daemon (cmd, args,
    devices). Returns None if the topic isn't a recognized command."""
    parts = topic.split("/")
    # <prefix>/<id>/set/<leaf>
    if len(parts) != 4 or parts[0] != prefix or parts[2] != "set":
        return None
    device_id, leaf = parts[1], parts[3]
    p = payload.strip()
    up = p.upper()
    if leaf == "power":
        return ("poweron" if up in ("ON", "1", "TRUE") else "poweroff", [], [device_id])
    if leaf == "screen":
        return ("screenon" if up in ("ON", "1", "TRUE") else "screenoff", [], [device_id])
    if leaf == "input":
        try:
            return ("sethdmi", [int(p)], [device_id])
        except ValueError:
            return None
    if leaf == "volume":
        try:
            return ("volume", [int(p)], [device_id])
        except ValueError:
            return None
    if leaf == "idle":
        # meta verbs apply to all managed devices
        return ("idle" if up in ("ON", "1", "TRUE") else "unidle", [], [])
    return None


def _device_block(device_id: str, name: str) -> dict:
    return {
        "identifiers": [f"lgtvc_{device_id}"],
        "name": name or f"LG TV ({device_id})",
        "manufacturer": "LG",
        "model": "webOS TV",
        "via_device": "lgtvc",
    }


def discovery_configs(prefix: str, discovery_prefix: str,
                      device_id: str, name: str) -> list[tuple[str, dict]]:
    """Return (config_topic, payload) pairs for the HA entities of one device."""
    dev = _device_block(device_id, name)
    avail = [{"topic": availability_topic(prefix, device_id)}]
    uid = f"lgtvc_{device_id}"
    out: list[tuple[str, dict]] = []

    out.append((
        f"{discovery_prefix}/switch/{uid}_power/config",
        {"name": "Power", "unique_id": f"{uid}_power", "device": dev,
         "availability": avail,
         "state_topic": state_topic(prefix, device_id, "power"),
         "command_topic": command_topic(prefix, device_id, "power"),
         "payload_on": "ON", "payload_off": "OFF", "icon": "mdi:television"}))

    out.append((
        f"{discovery_prefix}/switch/{uid}_screen/config",
        {"name": "Screen", "unique_id": f"{uid}_screen", "device": dev,
         "availability": avail,
         "state_topic": state_topic(prefix, device_id, "screen"),
         "command_topic": command_topic(prefix, device_id, "screen"),
         "payload_on": "ON", "payload_off": "OFF",
         "icon": "mdi:monitor-shimmer"}))

    out.append((
        f"{discovery_prefix}/select/{uid}_input/config",
        {"name": "HDMI input", "unique_id": f"{uid}_input", "device": dev,
         "availability": avail,
         "state_topic": state_topic(prefix, device_id, "input"),
         "command_topic": command_topic(prefix, device_id, "input"),
         "options": ["1", "2", "3", "4"], "icon": "mdi:hdmi-port"}))

    out.append((
        f"{discovery_prefix}/number/{uid}_volume/config",
        {"name": "Volume", "unique_id": f"{uid}_volume", "device": dev,
         "availability": avail,
         "command_topic": command_topic(prefix, device_id, "volume"),
         "min": 0, "max": 100, "icon": "mdi:volume-high"}))

    out.append((
        f"{discovery_prefix}/sensor/{uid}_state/config",
        {"name": "Power state", "unique_id": f"{uid}_state", "device": dev,
         "availability": avail,
         "state_topic": state_topic(prefix, device_id, "state"),
         "icon": "mdi:information-outline"}))

    return out


def discovery_payload_json(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))
