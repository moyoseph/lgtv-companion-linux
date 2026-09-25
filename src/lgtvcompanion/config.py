"""Configuration: schema, defaults, load/validate, legacy import."""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SYSTEM_CONFIG = Path("/etc/lgtv-companion/config.json")
SYSTEM_STATE = Path("/var/lib/lgtv-companion")
LEGACY_DIR = Path("/etc/lgtvcontrol")


def user_config_path() -> Path:
    import os
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "lgtv-companion" / "config.json"


def user_state_path() -> Path:
    import os
    base = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    return base / "lgtv-companion"


@dataclass
class IdleConfig:
    enabled: bool = False
    minutes: int = 10                       # 1-240
    action: str = "blank"                   # blank | power_off (on idle timeout)
    mute_speakers: bool = False
    veto_fullscreen: bool = True
    veto_mpris: str = "any"                 # any | foreground_only | off
    # evdev key codes (int) or KEY_* names (str); parse_ignored_keys accepts both
    ignored_keys: list[str | int] = field(default_factory=list)
    process_list: list[dict] = field(default_factory=list)


@dataclass
class RemoteStreamConfig:
    enabled: bool = False
    on_connect: str = "blank"               # off | blank
    on_disconnect: str = "on"               # on | keep_off | restore
    sunshine_log: str = "auto"
    # process-name globs to also treat as an active stream (Parsec, Chrome
    # Remote Desktop, Apollo, Moonlight host, …) — detected by the agent
    processes: list[str] = field(default_factory=list)


@dataclass
class WakeOnInputConfig:
    enabled: bool = True
    cooldown_s: int = 15


@dataclass
class SteamControllerConfig:
    # In gamescope/Game Mode the Steam client claims the controller over hidraw,
    # so it emits no evdev events; the agent reads /dev/hidraw* directly to keep
    # the TV awake. Inert on any system without a Valve (28de) controller.
    enabled: bool = True
    # Controller input may also power ON a fully-off TV (reported to the daemon
    # as a key press). The hidraw detector can't tell buttons from stick
    # movement, so ANY genuine controller input wakes an off TV — set False to
    # keep the controller unblank-only.
    wake: bool = True


@dataclass
class TopologyConfig:
    enabled: bool = False
    keep_on_boot: bool = False


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    topic_prefix: str = "lgtvc"
    discovery: bool = True                  # publish Home Assistant MQTT discovery
    discovery_prefix: str = "homeassistant"
    client_id: str = "lgtvc-bridge"


@dataclass
class GlobalConfig:
    power_on_timeout: int = 40              # 5-100 s
    power_on_at_boot: bool = True
    idle: IdleConfig = field(default_factory=IdleConfig)
    remote_stream: RemoteStreamConfig = field(default_factory=RemoteStreamConfig)
    topology: TopologyConfig = field(default_factory=TopologyConfig)
    wake_on_input: WakeOnInputConfig = field(default_factory=WakeOnInputConfig)
    steam_controller: SteamControllerConfig = field(
        default_factory=SteamControllerConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    on_lock: str = "none"                   # none | blank | off (on session lock)
    on_unlock: str = "none"                 # none | on (on session unlock)
    # handle suspend/resume/shutdown inside the daemon via logind signals
    # (user-mode installs, where system sleep/poweroff oneshot units aren't
    # available); system installs leave this false and use the oneshot units
    daemon_power_events: bool = False
    external_api: bool = True
    log_level: str = "info"
    update_check: str = "notify"            # notify | off
    offline_mode: bool = False              # privacy: the app makes zero internet calls
    dry_run: bool = False


@dataclass
class DeviceConfig:
    id: str
    host: str
    name: str = ""
    enabled: bool = True
    mac: list[str] = field(default_factory=list)
    ssl: bool = True
    wol_method: str = "subnet"              # broadcast | subnet | directed | auto
    subnet: str = "auto"
    interface: str | None = None            # NIC to send WoL from (multi-NIC/VPN)
    # extra explicit WoL targets (e.g. a remote subnet's broadcast for
    # cross-subnet/VPN wake); appended for every wol_method
    wol_targets: list[str] = field(default_factory=list)
    persistent_connection: str = "keepalive"  # off | keep_open | keepalive
    source_hdmi_input: int | None = None
    check_hdmi_input_when_powering_off: bool = True
    set_hdmi_input: int | None = None
    set_hdmi_input_delay: int = 0           # 0-30 s
    standby_mode: str = "active"
    unique_display_key: str | None = None
    # retry cadence (legacy import may override the defaults)
    retry_attempts: int = 8
    backoff_base: float = 1.0
    backoff_max: float = 8.0
    timeout: float = 10.0


@dataclass
class Config:
    schema: int = 1
    global_: GlobalConfig = field(default_factory=GlobalConfig)
    devices: list[DeviceConfig] = field(default_factory=list)

    def device(self, selector: str) -> DeviceConfig | None:
        for d in self.devices:
            if selector.lower() in (d.id.lower(), d.name.lower()):
                return d
        return None


def _from_dict(cls, data: dict):
    names = {f.name for f in dataclasses.fields(cls)}
    kwargs = {}
    for key, value in data.items():
        attr = "global_" if key == "global" else key
        if attr not in names:
            log.warning("config: unknown key %r ignored", key)
            continue
        if attr == "global_":
            value = _from_dict(GlobalConfig, value)
        elif attr == "idle":
            value = _from_dict(IdleConfig, value)
        elif attr == "remote_stream":
            value = _from_dict(RemoteStreamConfig, value)
        elif attr == "topology":
            value = _from_dict(TopologyConfig, value)
        elif attr == "wake_on_input":
            value = _from_dict(WakeOnInputConfig, value)
        elif attr == "steam_controller":
            value = _from_dict(SteamControllerConfig, value)
        elif attr == "mqtt":
            value = _from_dict(MqttConfig, value)
        elif attr == "devices":
            value = [_from_dict(DeviceConfig, d) for d in value]
        kwargs[attr] = value
    return cls(**kwargs)


def _to_dict(obj) -> dict:
    out = {}
    for f in dataclasses.fields(obj):
        key = "global" if f.name == "global_" else f.name
        value = getattr(obj, f.name)
        if dataclasses.is_dataclass(value):
            value = _to_dict(value)
        elif isinstance(value, list) and value and dataclasses.is_dataclass(value[0]):
            value = [_to_dict(v) for v in value]
        out[key] = value
    return out


def validate(cfg: Config) -> list[str]:
    problems = []
    g = cfg.global_
    if not 5 <= g.power_on_timeout <= 100:
        problems.append(f"power_on_timeout {g.power_on_timeout} outside 5-100")
    if not 1 <= g.idle.minutes <= 240:
        problems.append(f"idle.minutes {g.idle.minutes} outside 1-240")
    if g.on_lock not in ("none", "blank", "off"):
        problems.append(f"on_lock {g.on_lock!r} not none|blank|off")
    if g.on_unlock not in ("none", "on"):
        problems.append(f"on_unlock {g.on_unlock!r} not none|on")
    if g.idle.action not in ("blank", "power_off"):
        problems.append(f"idle.action {g.idle.action!r} not blank|power_off")
    if g.idle.veto_mpris not in ("any", "foreground_only", "off"):
        problems.append(f"idle.veto_mpris {g.idle.veto_mpris!r} not any|foreground_only|off")
    if g.update_check not in ("notify", "off"):
        problems.append(f"update_check {g.update_check!r} not notify|off")
    ids = [d.id for d in cfg.devices]
    if len(ids) != len(set(ids)):
        problems.append("duplicate device ids")
    for d in cfg.devices:
        if not d.host:
            problems.append(f"device {d.id}: missing host")
        if d.wol_method not in ("broadcast", "subnet", "directed", "auto"):
            problems.append(f"device {d.id}: bad wol_method {d.wol_method!r}")
        if d.source_hdmi_input is not None and not 1 <= d.source_hdmi_input <= 4:
            problems.append(f"device {d.id}: source_hdmi_input outside 1-4")
        if not 0 <= d.set_hdmi_input_delay <= 30:
            problems.append(f"device {d.id}: set_hdmi_input_delay outside 0-30")
    return problems


def load(path: Path) -> Config:
    data = json.loads(path.read_text())
    cfg = _from_dict(Config, data)
    problems = validate(cfg)
    if problems:
        raise ValueError(f"invalid config {path}: " + "; ".join(problems))
    return cfg


def save(cfg: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_to_dict(cfg), indent=2) + "\n")


def find_config() -> Path | None:
    for p in (SYSTEM_CONFIG, user_config_path()):
        if p.exists():
            return p
    return None


def is_lan_host(host: str) -> bool:
    """True if `host` is definitely on the local network, WITHOUT resolving DNS
    (resolving a name is itself a trip off-box). Used by offline_mode to allow a
    LAN broker while refusing anything that could reach the internet.

    Accepts loopback / private / link-local IPs, `localhost`, and the common
    local domain suffixes; refuses bare or public hostnames (which would need a
    resolver and might point anywhere)."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        h = host.lower().rstrip(".")
        return h == "localhost" or h.endswith(
            (".local", ".lan", ".home", ".internal"))


def import_legacy(legacy_dir: Path = LEGACY_DIR, *, device_id: str = "tv1") -> tuple[Config, str | None]:
    """Build a Config from the proven /etc/lgtvcontrol setup.

    Returns (config, client_key). The caller stores the key in the key store —
    it is never written into the config file.
    """
    tv_ip = (legacy_dir / "tv_ip").read_text().strip()
    mac_file = legacy_dir / "tv_mac"
    macs = [mac_file.read_text().strip()] if mac_file.exists() else []
    box_input_file = legacy_dir / "box_input"
    box_input_app = ""
    if box_input_file.exists():
        box_input_app = box_input_file.read_text().strip()
    hdmi = 4
    if box_input_app.startswith("com.webos.app.hdmi"):
        hdmi = int(box_input_app.removeprefix("com.webos.app.hdmi") or 4)

    dev = DeviceConfig(
        id=device_id, host=tv_ip, name="LG TV", mac=macs,
        source_hdmi_input=hdmi, set_hdmi_input=hdmi,
    )

    # legacy config file: key = value tunables
    legacy_conf = legacy_dir / "config"
    if legacy_conf.exists():
        for raw in legacy_conf.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip().lower(), value.strip()
            try:
                if key == "timeout":
                    dev.timeout = float(value)
                elif key == "retry_attempts":
                    dev.retry_attempts = int(value)
                elif key == "backoff_base":
                    dev.backoff_base = float(value)
                elif key == "backoff_max":
                    dev.backoff_max = float(value)
            except ValueError:
                log.warning("legacy config: bad value %r for %s — ignored", value, key)

    key_file = legacy_dir / "client.key"
    client_key = key_file.read_text().strip() if key_file.exists() else None

    return Config(devices=[dev]), client_key


# Windows virtual-key codes -> evdev key codes (media keys only; anything
# else in IgnoredKeysList is dropped with a warning)
_VK_TO_EVDEV = {
    173: 113,  # VK_VOLUME_MUTE  -> KEY_MUTE
    174: 114,  # VK_VOLUME_DOWN  -> KEY_VOLUMEDOWN
    175: 115,  # VK_VOLUME_UP    -> KEY_VOLUMEUP
    176: 163,  # VK_MEDIA_NEXT   -> KEY_NEXTSONG
    177: 165,  # VK_MEDIA_PREV   -> KEY_PREVIOUSSONG
    178: 166,  # VK_MEDIA_STOP   -> KEY_STOPCD
    179: 164,  # VK_MEDIA_PLAY   -> KEY_PLAYPAUSE
}

_WOL_METHOD = {1: "broadcast", 2: "directed", 3: "subnet", 4: "auto"}
_PERSISTENT = {0: "off", 1: "keep_open", 2: "keepalive"}
_END_MODE = {0: "on", 1: "keep_off", 2: "restore"}


def import_windows(path: Path) -> tuple[Config, dict[str, str]]:
    """Convert an upstream LGTV Companion config.json.

    Returns (config, {device_id: session_key}). Session keys go to the key
    store, never into our config file. Windows-only settings (NicLuid,
    TimingShutdown, UpdaterMode, locale word lists, VWL flags) are dropped —
    their Linux replacements are automatic (see docs/parity.md).
    """
    data = json.loads(path.read_text())
    prefs = data.get("LGTV Companion", {})
    cfg = Config()
    keys: dict[str, str] = {}

    g = cfg.global_
    if isinstance(prefs.get("PowerOnTimeOut"), int):
        g.power_on_timeout = max(5, min(100, prefs["PowerOnTimeOut"]))
    if isinstance(prefs.get("BlankWhenIdle"), bool):
        g.idle.enabled = prefs["BlankWhenIdle"]
    if isinstance(prefs.get("BlankWhenIdleDelay"), int):
        g.idle.minutes = max(1, min(240, prefs["BlankWhenIdleDelay"]))
    if isinstance(prefs.get("MuteSpeakers"), bool):
        g.idle.mute_speakers = prefs["MuteSpeakers"]
    if isinstance(prefs.get("BlankWhenIdleFullscreenDisable"), bool):
        g.idle.veto_fullscreen = prefs["BlankWhenIdleFullscreenDisable"]
    if isinstance(prefs.get("ExternalAPI"), bool):
        g.external_api = prefs["ExternalAPI"]
    if isinstance(prefs.get("RemoteStream"), bool):
        g.remote_stream.enabled = prefs["RemoteStream"]
    if isinstance(prefs.get("RemoteStreamPowerOff"), bool):
        g.remote_stream.on_connect = "off" if prefs["RemoteStreamPowerOff"] else "blank"
    if isinstance(prefs.get("RemoteStreamEndMode"), int):
        g.remote_stream.on_disconnect = _END_MODE.get(
            prefs["RemoteStreamEndMode"], "on")
    if isinstance(prefs.get("AdhereDisplayTopology"), bool):
        g.topology.enabled = prefs["AdhereDisplayTopology"]
    if isinstance(prefs.get("KeepTopologyOnBoot"), bool):
        g.topology.keep_on_boot = prefs["KeepTopologyOnBoot"]
    for vk in prefs.get("IgnoredKeysList", []) or []:
        if vk in _VK_TO_EVDEV:
            g.idle.ignored_keys.append(_VK_TO_EVDEV[vk])
        else:
            log.warning("import-windows: ignored key VK=%s has no evdev mapping "
                        "— dropped", vk)
    process_list = prefs.get("BlankWhenIdleProcessList", {}) or {}
    for friendly, entry in process_list.items():
        binary = entry.get("Binary", "")
        if not binary:
            continue
        flags = [f.lower() for f in
                 ("Running", "Fullscreen", "Foreground", "VideoWakeLock")
                 if entry.get(f)]
        g.idle.process_list.append(
            {"match": binary.lower(), "flags": flags or ["running"],
             "comment": friendly})

    for node, value in data.items():
        if node == "LGTV Companion" or not isinstance(value, dict):
            continue
        dev = DeviceConfig(id=node.lower(), host=value.get("IP", ""))
        name = value.get("Name", "")
        dev.name = name.removeprefix("[LG] webOS TV ") or node
        mac = value.get("MAC")
        dev.mac = [mac] if isinstance(mac, str) else list(mac or [])
        if isinstance(value.get("Enabled"), bool):
            dev.enabled = value["Enabled"]
        if isinstance(value.get("NewSockConnect"), bool):
            dev.ssl = value["NewSockConnect"]
        dev.wol_method = _WOL_METHOD.get(value.get("WOL", 4), "auto")
        subnet = value.get("Subnet")
        dev.subnet = subnet if isinstance(subnet, str) and subnet else "auto"
        dev.persistent_connection = _PERSISTENT.get(
            value.get("PersistentConnectionLevel", 0), "off")
        src = value.get("SourceHdmiInput") or value.get(
            "OnlyTurnOffIfCurrentHDMIInputNumberIs")
        if isinstance(src, int):
            dev.source_hdmi_input = max(1, min(4, src))
        check = value.get("CheckHdmiInputWhenPoweringOff",
                          value.get("HDMIinputcontrol"))
        if isinstance(check, bool):
            dev.check_hdmi_input_when_powering_off = check
        set_input = value.get("SetHdmiInput", value.get("SetHDMIInputOnResume"))
        if set_input:
            n = value.get("SetHDMIInputOnResumeToNumber", dev.source_hdmi_input)
            if isinstance(n, int):
                dev.set_hdmi_input = max(1, min(4, n))
        if isinstance(value.get("SetHdmiInputDelay"), int):
            dev.set_hdmi_input_delay = max(0, min(30, value["SetHdmiInputDelay"]))
        if isinstance(value.get("UniqueDeviceKey"), str):
            dev.unique_display_key = value["UniqueDeviceKey"]
        if value.get("NicLuid"):
            log.warning("import-windows: %s: NicLuid dropped — set 'interface' "
                        "to a NIC name if you need source binding", node)
        session_key = value.get("SessionKey")
        if isinstance(session_key, str) and session_key:
            keys[dev.id] = session_key
        cfg.devices.append(dev)

    return cfg, keys
