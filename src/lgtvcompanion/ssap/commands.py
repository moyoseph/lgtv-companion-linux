"""The command table: single source of truth driving CLI parsing, daemon IPC
dispatch, settings GUI and docs.

The luna settings surface (~74 commands incl. per-HDMI variants) is generated
from lg_api_commands.json, vendored verbatim from upstream LGTV Companion
(Common/lg_api_commands.h, MIT) — same names, categories, ranges and enums.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from typing import Any

from .keys import canonical_button

LUNA_SET_SYSTEM_SETTINGS = "luna://com.webos.settingsservice/setSystemSettings"
LUNA_SET_DEVICE_INFO = "luna://com.webos.service.eim/setDeviceInfo"
LUNA_SET_CURVE_PRESET = "luna://com.webos.service.rollingscreen/changeCurve"
LUNA_ADJUST_CURVE_PRESET = "luna://com.webos.service.rollingscreen/updateCurvature"
LUNA_SET_TPC = "luna://com.webos.service.oledepl/setTemporalPeakControl"
LUNA_SET_GSR = "luna://com.webos.service.oledepl/setGlobalStressReduction"


class Kind(Enum):
    POWER = "power"            # routed through the power-state machine
    REQUEST = "request"        # plain ssap:// request
    LUNA_SETTING = "luna"      # single settingsservice write (generated table)
    LUNA_RAW = "luna_raw"      # bespoke luna call(s) via a builder function
    BUTTON = "button"          # pointer-input socket button
    META = "meta"              # daemon-side verb (no TV traffic by itself)


@dataclass(frozen=True)
class Arg:
    name: str

    def parse(self, token: str) -> Any:
        return token


@dataclass(frozen=True)
class IntArg(Arg):
    lo: int | None = None
    hi: int | None = None

    def parse(self, token: str) -> int:
        try:
            v = int(token)
        except ValueError:
            raise ValueError(f"{self.name}: expected an integer, got {token!r}") from None
        if (self.lo is not None and v < self.lo) or (self.hi is not None and v > self.hi):
            raise ValueError(f"{self.name}: {v} outside [{self.lo}, {self.hi}]")
        return v


@dataclass(frozen=True)
class EnumArg(Arg):
    values: tuple[str, ...] = ()

    def parse(self, token: str) -> str:
        for v in self.values:
            if v.lower() == token.lower():
                return v
        raise ValueError(f"{self.name}: {token!r} not one of {', '.join(self.values)}")


@dataclass(frozen=True)
class JsonArg(Arg):
    def parse(self, token: str) -> Any:
        try:
            return json.loads(token)
        except ValueError:
            raise ValueError(f"{self.name}: invalid JSON: {token!r}") from None


@dataclass(frozen=True)
class ButtonArg(Arg):
    def parse(self, token: str) -> str:
        return canonical_button(token)


@dataclass(frozen=True)
class Command:
    name: str
    kind: Kind
    help: str = ""
    args: tuple[Arg, ...] = ()
    optional_args: int = 0          # how many trailing args may be omitted
    uri: str | None = None          # Kind.REQUEST
    payload_key: str | None = None  # REQUEST: wrap single arg as {payload_key: arg}
    static_payload: dict | None = None
    luna_category: str | None = None   # Kind.LUNA_SETTING
    luna_setting: str | None = None
    luna_format: str = "string"        # "string" | "int" (upstream ValFormat)
    action: str | None = None       # Kind.POWER / Kind.META verb

    @property
    def min_args(self) -> int:
        return len(self.args) - self.optional_args


ONOFF = ("off", "on")


def _power(name: str, action: str, help_: str) -> Command:
    return Command(name=name, kind=Kind.POWER, action=action, help=help_)


def _request(name: str, uri: str, help_: str, *, args: tuple[Arg, ...] = (),
             payload_key: str | None = None, static_payload: dict | None = None,
             optional_args: int = 0) -> Command:
    return Command(name=name, kind=Kind.REQUEST, uri=uri, help=help_, args=args,
                   payload_key=payload_key, static_payload=static_payload,
                   optional_args=optional_args)


def _raw(name: str, help_: str, *, args: tuple[Arg, ...] = (),
         optional_args: int = 0) -> Command:
    return Command(name=name, kind=Kind.LUNA_RAW, help=help_, args=args,
                   optional_args=optional_args)


def _meta(name: str, action: str, help_: str, *, args: tuple[Arg, ...] = ()) -> Command:
    return Command(name=name, kind=Kind.META, action=action, help=help_, args=args)


def _generated_luna_commands() -> list[Command]:
    with resources.files("lgtvcompanion.data").joinpath("lg_api_commands.json").open() as f:
        table = json.load(f)
    out: list[Command] = []
    for name, spec in table.items():
        help_ = spec.get("LogMessage", name).replace("[#ARG#]", "<value>")
        if "Argument" in spec:
            arg: Arg = EnumArg("value", tuple(spec["Argument"].split()))
        else:
            arg = IntArg("value", spec["Min"], spec["Max"])
        out.append(Command(
            name=name.lower(), kind=Kind.LUNA_SETTING, help=help_, args=(arg,),
            luna_category=spec["Category"], luna_setting=spec["Setting"],
            luna_format=spec.get("ValFormat", "string"),
        ))
    return out


_TABLE: list[Command] = [
    # --- power / screen ---
    _power("poweron", "power_on", "Power on device(s), waking via WoL if needed"),
    _power("poweroff", "power_off", "Power off device(s) (HDMI-guarded)"),
    _power("screenon", "unblank", "Unblank the screen (and power on if needed)"),
    _power("screenoff", "blank", "Blank the screen (panel emitters off)"),

    # --- HDMI input ---
    _request("sethdmi", "system.launcher/launch", "Set active HDMI input 1-4",
             args=(IntArg("input", 1, 4),)),
    *[
        Command(name=f"sethdmi{n}", kind=Kind.REQUEST, uri="system.launcher/launch",
                static_payload={"id": f"com.webos.app.hdmi{n}"},
                help=f"Set active HDMI input {n}")
        for n in (1, 2, 3, 4)
    ],
    _raw("set_input_type", "-set_input_type [HDMI_x] [icon] [label]",
         args=(Arg("input"), Arg("icon"), Arg("label"))),

    # --- audio (direct ssap; luna sound settings come from the table) ---
    _request("mute", "audio/setMute", "Mute TV speakers", static_payload={"mute": True}),
    _request("unmute", "audio/setMute", "Unmute TV speakers",
             static_payload={"mute": False}),
    _request("volume", "audio/setVolume", "Set volume 0-100",
             args=(IntArg("volume", 0, 100),), payload_key="volume"),

    # --- buttons / service menu ---
    Command(name="button", kind=Kind.BUTTON, args=(ButtonArg("name"),),
            help="Send a remote button (LEFT, HOME, INFO, 0-9, …)"),
    Command(name="button_nocheck", kind=Kind.BUTTON, args=(Arg("name"),),
            help="Send a button name without validation"),
    _request("freesyncinfo", "system.launcher/launch",
             "Show the FreeSync/FPS information panel",
             static_payload={"id": "com.webos.app.tvhotkey",
                             "params": {"activateType": "freesync-info"}}),
    _request("servicemenu", "system.launcher/launch",
             "Open the service menu (code 0413)",
             static_payload={"id": "com.webos.app.factorywin",
                             "params": {"irKey": "inStart"}}),
    _raw("servicemenu_legacy_enable", "Enable the legacy service menu"),
    _raw("servicemenu_legacy_disable", "Disable the legacy service menu"),
    _raw("servicemenu_tpc_enable", "Enable Temporal Peak Control (service menu)"),
    _raw("servicemenu_tpc_disable", "Disable Temporal Peak Control (service menu)"),
    _raw("servicemenu_gsr_enable", "Enable Global Stress Reduction (service menu)"),
    _raw("servicemenu_gsr_disable", "Disable Global Stress Reduction (service menu)"),

    # --- LG Flex curvature ---
    _raw("set_curve_preset", "-set_curve_preset [flat|1-3]",
         args=(EnumArg("preset", ("flat", "0", "1", "2", "3")),)),
    _raw("adjust_curve_preset", "-adjust_curve_preset [1-3] [0-100]",
         args=(EnumArg("preset", ("1", "2", "3")), IntArg("percent", 0, 100))),
    _raw("set_curvature", "-set_curvature [flat|0-100]", args=(Arg("value"),)),

    # --- generic / advanced ---
    _request("request", "", "Send a raw ssap:// request: -request <endpoint>",
             args=(Arg("endpoint"),)),
    _request("request_with_param", "", "-request_with_param <endpoint> <json>",
             args=(Arg("endpoint"), JsonArg("payload"))),
    _request("start_app", "system.launcher/launch", "Launch an app by id",
             args=(Arg("appid"),), payload_key="id"),
    _request("start_app_with_param", "system.launcher/launch",
             "-start_app_with_param <appid> <json>",
             args=(Arg("appid"), JsonArg("params"))),
    _request("close_app", "system.launcher/close", "Close an app by id",
             args=(Arg("appid"),), payload_key="id"),
    _request("get_system_settings", "settings/getSystemSettings",
             '-get_system_settings <category> ["key1","key2"]',
             args=(Arg("category"), JsonArg("keys")), optional_args=1),
    _raw("settings_picture", "Write raw picture settings JSON",
         args=(JsonArg("settings"),)),
    _raw("settings_other", "Write raw other settings JSON",
         args=(JsonArg("settings"),)),
    _raw("settings_options", "Write raw option settings JSON",
         args=(JsonArg("settings"),)),

    # --- management / daemon verbs ---
    _meta("autoenable", "auto_enable", "Re-enable automatic management for device(s)"),
    _meta("autodisable", "auto_disable", "Disable automatic management until restart"),
    _meta("idle", "force_idle", "Force user-idle mode (blank) now"),
    _meta("unidle", "force_unidle", "Leave user-idle mode now"),
    _meta("streaming_connect", "streaming_connect", "Signal a remote stream started"),
    _meta("streaming_disconnect", "streaming_disconnect", "Signal a remote stream ended"),
    _meta("clearlog", "clear_log", "Truncate the daemon log"),

    *_generated_luna_commands(),
]

COMMANDS: dict[str, Command] = {c.name: c for c in _TABLE}


def lookup(token: str) -> Command | None:
    """Case-insensitive lookup of a `-command` token (dash already stripped)."""
    return COMMANDS.get(token.lower())


def build_request(cmd: Command, args: list[Any]) -> tuple[str, dict | None]:
    """For Kind.REQUEST: return (uri, payload)."""
    if cmd.name == "request":
        return args[0], None
    if cmd.name == "request_with_param":
        return args[0], args[1]
    if cmd.name == "start_app_with_param":
        return cmd.uri or "", {"id": args[0], "params": args[1]}
    if cmd.name == "get_system_settings":
        payload: dict = {"category": args[0]}
        if len(args) > 1:
            payload["keys"] = args[1]
        return cmd.uri or "", payload
    if cmd.name == "sethdmi":
        return cmd.uri or "", {"id": f"com.webos.app.hdmi{args[0]}"}
    payload = dict(cmd.static_payload) if cmd.static_payload else {}
    if cmd.payload_key is not None and args:
        payload[cmd.payload_key] = args[0]
    return cmd.uri or "", (payload or None)


def build_luna_setting(cmd: Command, args: list[Any]) -> tuple[str, dict]:
    """For Kind.LUNA_SETTING: return (category, settings).

    Upstream semantics: values are sent as strings unless ValFormat is "int".
    Per-HDMI settings (Setting like "gameMode_hdmi1") nest as
    {"gameMode": {"hdmi1": value}}.
    """
    assert cmd.luna_category and cmd.luna_setting
    value: Any = int(args[0]) if cmd.luna_format == "int" else str(args[0])
    setting = cmd.luna_setting
    if "_hdmi" in setting:
        base, _, num = setting.partition("_hdmi")
        return cmd.luna_category, {base: {f"hdmi{num}": value}}
    return cmd.luna_category, {setting: value}


def build_luna_raw(cmd: Command, args: list[Any]) -> list[tuple[str, dict]]:
    """For Kind.LUNA_RAW: return a list of (luna_uri, params) calls."""
    name = cmd.name
    if name == "set_input_type":
        return [(LUNA_SET_DEVICE_INFO,
                 {"id": args[0], "icon": f"{args[1]}.png", "label": args[2]})]
    if name.startswith("servicemenu_legacy_"):
        # note the inversion: enabling the full menu clears the flag
        flag = name.endswith("_disable")
        return [(LUNA_SET_SYSTEM_SETTINGS,
                 {"category": "other", "settings": {"svcMenuFlag": flag}})]
    if name.startswith("servicemenu_tpc_"):
        return [(LUNA_SET_TPC, {"enable": name.endswith("_enable")})]
    if name.startswith("servicemenu_gsr_"):
        return [(LUNA_SET_GSR, {"enable": name.endswith("_enable")})]
    if name == "set_curve_preset":
        preset = "flat" if args[0] in ("flat", "0") else f"curvature{args[0]}"
        return [(LUNA_SET_CURVE_PRESET,
                 {"type": preset, "reason": "com.pal.app.settings"})]
    if name == "adjust_curve_preset":
        return [(LUNA_ADJUST_CURVE_PRESET,
                 {"type": f"curvature{args[0]}", "value": f"{args[1]}%"})]
    if name == "set_curvature":
        if str(args[0]).lower() in ("flat", "0"):
            return [(LUNA_SET_CURVE_PRESET,
                     {"type": "flat", "reason": "com.pal.app.settings"})]
        percent = max(0, min(100, int(args[0])))
        return [
            (LUNA_ADJUST_CURVE_PRESET,
             {"type": "curvature1", "value": f"{percent}%"}),
            (LUNA_SET_CURVE_PRESET,
             {"type": "curvature1", "reason": "com.pal.app.settings"}),
        ]
    if name in ("settings_picture", "settings_other", "settings_options"):
        category = {"settings_picture": "picture", "settings_other": "other",
                    "settings_options": "option"}[name]
        return [(LUNA_SET_SYSTEM_SETTINGS,
                 {"category": category, "settings": args[0]})]
    raise ValueError(f"no luna builder for {name!r}")


Builder = Callable[[Command, list[Any]], Any]
