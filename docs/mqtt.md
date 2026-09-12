# MQTT / Home Assistant bridge

`lgtvc-mqtt` bridges the daemon to an MQTT broker: it publishes each TV's
state and accepts commands, with **Home Assistant MQTT auto-discovery** so your
TVs appear as entities with no manual YAML. It's a separate optional process
(the `aiomqtt` dependency stays out of the core), and just another client of
the daemon's IPC socket.

## Install & enable

```sh
sudo /usr/local/lib/lgtv-companion/bin/pip install 'lgtvcompanion[mqtt]'
sudo lgtvc setup install --mode system    # (re-)renders the lgtvc-mqtt unit
```

Add an `mqtt` block to the config (`/etc/lgtv-companion/config.json`, under
`global`) and enable the service:

```jsonc
"mqtt": {
  "enabled": true,
  "host": "192.168.1.10",
  "port": 1883,
  "username": "lgtvc",
  "password": "…",
  "topic_prefix": "lgtvc",
  "discovery": true,
  "discovery_prefix": "homeassistant"
}
```

```sh
sudo systemctl enable --now lgtvc-mqtt
```

## Home Assistant

With `discovery: true` each TV shows up automatically under a device named after
it, with entities:

| Entity | Type | |
|---|---|---|
| Power | switch | on/off (Wake-on-LAN handles power-on) |
| Screen | switch | blank / unblank the panel |
| HDMI input | select | 1–4 |
| Volume | number | 0–100 |
| Power state | sensor | Active / Screen Off / Active Standby / Unknown |

Availability is tracked per device and for the bridge (LWT), so entities go
*unavailable* if the bridge or daemon stops.

## Topics (for non-HA automation)

State (retained), per device `‹prefix›/‹id›/…`: `power` (ON/OFF), `screen`,
`state`, `input`, `idle`, `availability` (online/offline).

Commands `‹prefix›/‹id›/set/…`:

| Topic | Payload | Effect |
|---|---|---|
| `set/power` | ON/OFF | power on / off |
| `set/screen` | ON/OFF | unblank / blank |
| `set/input` | 1-4 | switch HDMI input |
| `set/volume` | 0-100 | set volume |
| `set/idle` | ON/OFF | force / release idle mode (all devices) |

Example:

```sh
mosquitto_pub -h broker -t lgtvc/tv1/set/power -m OFF
mosquitto_sub -h broker -t 'lgtvc/#' -v
```
