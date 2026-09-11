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
    mute_speakers: bool = False
    veto_fullscreen: bool = True
    veto_mpris: str = "any"                 # any | foreground_only | off
    ignored_keys: list[str] = field(default_factory=list)
    process_list: list[dict] = field(default_factory=list)


@dataclass
class RemoteStreamConfig:
    enabled: bool = False
    on_connect: str = "blank"               # off | blank
    on_disconnect: str = "on"               # on | keep_off | restore
    sunshine_log: str = "auto"


@dataclass
class WakeOnInputConfig:
    enabled: bool = True
    cooldown_s: int = 15


@dataclass
class TopologyConfig:
    enabled: bool = False
    keep_on_boot: bool = False


@dataclass
class GlobalConfig:
    power_on_timeout: int = 40              # 5-100 s
    power_on_at_boot: bool = True
    idle: IdleConfig = field(default_factory=IdleConfig)
    remote_stream: RemoteStreamConfig = field(default_factory=RemoteStreamConfig)
    topology: TopologyConfig = field(default_factory=TopologyConfig)
    wake_on_input: WakeOnInputConfig = field(default_factory=WakeOnInputConfig)
    external_api: bool = True
    log_level: str = "info"
    update_check: str = "notify"            # notify | off
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
    interface: str | None = None
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
