"""The command table: single source of truth driving CLI parsing, daemon IPC
dispatch, settings GUI and docs.

v0.1 ships the power/input/audio/generic subset; the full ~100-command surface
(picture luna settings, buttons, ambient, service menu) lands in v0.2 as
additional table rows — no parser or dispatcher changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Kind(Enum):
    POWER = "power"            # routed through the power-state machine
    REQUEST = "request"        # plain ssap:// request
    LUNA_SETTING = "luna"      # settings write via the createAlert trick
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
    action: str | None = None       # Kind.POWER / Kind.META verb
    since: str = "0.1"

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


def _luna(name: str, category: str, setting: str, help_: str,
          *, args: tuple[Arg, ...], since: str = "0.2") -> Command:
    return Command(name=name, kind=Kind.LUNA_SETTING, luna_category=category,
                   luna_setting=setting, help=help_, args=args, since=since)


def _meta(name: str, action: str, help_: str, *, args: tuple[Arg, ...] = ()) -> Command:
    return Command(name=name, kind=Kind.META, action=action, help=help_, args=args)


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

    # --- audio ---
    _request("mute", "audio/setMute", "Mute TV speakers", static_payload={"mute": True}),
    _request("unmute", "audio/setMute", "Unmute TV speakers", static_payload={"mute": False}),
    _request("volume", "audio/setVolume", "Set volume 0-100",
             args=(IntArg("volume", 0, 100),), payload_key="volume"),
    _luna("soundmode", "sound", "soundMode", "Set sound mode",
          args=(EnumArg("mode", ("aiSoundPlus", "aiSound", "standard", "news", "music",
                                 "movie", "sports", "game")),)),
    _luna("soundoutput", "sound", "soundOutput", "Set sound output device",
          args=(EnumArg("output", ("tv_speaker", "external_arc", "external_optical",
                                   "bt_soundbar", "mobile_phone", "lineout", "headphone",
                                   "tv_speaker_bluetooth", "tv_external_speaker",
                                   "tv_speaker_headphone", "wisa_speaker")),)),
    _luna("autovolume", "sound", "autoVolume", "Auto volume off/on",
          args=(EnumArg("state", ONOFF),)),

    # --- picture (v0.1 carries the flagship few; the rest are v0.2 rows) ---
    _luna("backlight", "picture", "backlight", "Set backlight 0-100",
          args=(IntArg("value", 0, 100),)),
    _luna("brightness", "picture", "brightness", "Set brightness 0-100",
          args=(IntArg("value", 0, 100),)),
    _luna("contrast", "picture", "contrast", "Set contrast 0-100",
          args=(IntArg("value", 0, 100),)),
    _luna("energysaving", "picture", "energySaving", "Energy saving mode",
          args=(EnumArg("mode", ("auto", "off", "min", "med", "max", "screen_off")),)),

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

    # --- management / daemon verbs ---
    _meta("autoenable", "auto_enable", "Re-enable automatic management for device(s)"),
    _meta("autodisable", "auto_disable", "Disable automatic management until restart"),
    _meta("idle", "force_idle", "Force user-idle mode (blank) now"),
    _meta("unidle", "force_unidle", "Leave user-idle mode now"),
    _meta("streaming_connect", "streaming_connect", "Signal a remote stream started"),
    _meta("streaming_disconnect", "streaming_disconnect", "Signal a remote stream ended"),
    _meta("clearlog", "clear_log", "Truncate the daemon log"),
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
