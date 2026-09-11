"""Fullscreen-window detection for the idle veto (KDE/KWin, best-effort).

KWin exposes no plain "is the active window fullscreen" property over D-Bus, so
we use its scripting interface: load a one-shot JS snippet that reports the
active window's fullscreen state + resource class by calling back into an
interface the agent exports on the session bus, then unload it.

This is genuinely KDE-Plasma-specific. On gamescope (Game Mode) everything is
fullscreen by construction and the daemon's process-list veto handles running
games; on GNOME/other compositors there is no equivalent, so probe() returns
None and the idle veto leans on MPRIS playback + logind idle inhibitors, which
already cover fullscreen video and most games.
"""

from __future__ import annotations

import asyncio
import logging

from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

log = logging.getLogger(__name__)

KWIN = "org.kde.KWin"
KWIN_SCRIPTING_PATH = "/Scripting"
KWIN_SCRIPTING_IFACE = "org.kde.kwin.Scripting"

AGENT_BUS_NAME = "org.lgtvcompanion.Agent"
AGENT_OBJ_PATH = "/org/lgtvcompanion/Agent"
AGENT_IFACE = "org.lgtvcompanion.Agent"

# One-shot KWin script: report the active window and unload itself. Works across
# KWin 5 (workspace.activeClient / .caption / .fullScreen) and KWin 6
# (workspace.activeWindow / .resourceClass) by trying both shapes. Built with
# str.replace (not % or .format) because the JS body contains {} braces.
_KWIN_SCRIPT_TEMPLATE = """
try {
    var w = (typeof workspace.activeWindow !== "undefined")
        ? workspace.activeWindow : workspace.activeClient;
    var fs = w ? !!w.fullScreen : false;
    var cls = "";
    if (w) { cls = w.resourceClass ? String(w.resourceClass)
                 : (w.caption ? String(w.caption) : ""); }
    callDBus("@NAME@", "@PATH@", "@IFACE@", "Report", fs, cls);
} catch (e) {
    callDBus("@NAME@", "@PATH@", "@IFACE@", "Report", false, "");
}
"""
_KWIN_SCRIPT = (_KWIN_SCRIPT_TEMPLATE
                .replace("@NAME@", AGENT_BUS_NAME)
                .replace("@PATH@", AGENT_OBJ_PATH)
                .replace("@IFACE@", AGENT_IFACE))


class _Reporter(ServiceInterface):
    def __init__(self, on_report):
        super().__init__(AGENT_IFACE)
        self._on_report = on_report

    @method()
    def Report(self, fullscreen: "b", app: "s"):  # noqa: F821,N802,UP037
        # dbus-fast reads these string annotations as D-Bus signatures ("b",
        # "s") — they must stay quoted, so UP037 is intentionally suppressed.
        self._on_report(fullscreen, app)


class FullscreenProbe:
    """Session-bus probe. Call available() once, then probe() per idle check."""

    def __init__(self, bus: MessageBus):
        self.bus = bus
        self._exported = False
        self._latest: tuple[bool, str] | None = None
        self._event = asyncio.Event()

    def _on_report(self, fullscreen: bool, app: str) -> None:
        self._latest = (fullscreen, app or "")
        self._event.set()

    async def available(self) -> bool:
        """True if KWin scripting is on the bus (i.e. a KDE session)."""
        try:
            reply = await self.bus.call(Message(
                destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus", member="NameHasOwner",
                signature="s", body=[KWIN]))
            return bool(reply.body and reply.body[0])
        except Exception:
            return False

    async def _ensure_exported(self) -> None:
        if self._exported:
            return
        await self.bus.request_name(AGENT_BUS_NAME)
        self.bus.export(AGENT_OBJ_PATH, _Reporter(self._on_report))
        self._exported = True

    async def probe(self, timeout: float = 2.0) -> str | None:
        """Return the fullscreen app's resource class, or None if the active
        window isn't fullscreen / KWin is unavailable."""
        try:
            await self._ensure_exported()
            self._event.clear()
            self._latest = None
            script_id = await self._load_script()
            if script_id is None:
                return None
            try:
                await self._run_script(script_id)
                await asyncio.wait_for(self._event.wait(), timeout=timeout)
            finally:
                await self._unload_script(script_id)
        except (TimeoutError, Exception) as e:
            log.debug("fullscreen probe failed: %s", e)
            return None
        if self._latest and self._latest[0]:
            return self._latest[1] or "fullscreen"
        return None

    # -- KWin scripting plumbing ----------------------------------------------

    async def _load_script(self) -> int | None:
        # loadScript(text, pluginName) — passing source directly is supported;
        # KWin treats a non-path string as inline script text.
        reply = await self.bus.call(Message(
            destination=KWIN, path=KWIN_SCRIPTING_PATH,
            interface=KWIN_SCRIPTING_IFACE, member="loadScript",
            signature="ss", body=[_KWIN_SCRIPT, "lgtvc-fullscreen"]))
        if reply.message_type != MessageType.METHOD_RETURN:
            return None
        return int(reply.body[0])

    def _script_path(self, script_id: int) -> str:
        return f"/Scripting/Script{script_id}"

    async def _run_script(self, script_id: int) -> None:
        for iface in ("org.kde.kwin.Script", "org.kde.kwin.Scripting"):
            reply = await self.bus.call(Message(
                destination=KWIN, path=self._script_path(script_id),
                interface=iface, member="run"))
            if reply.message_type == MessageType.METHOD_RETURN:
                return

    async def _unload_script(self, script_id: int) -> None:
        try:
            await self.bus.call(Message(
                destination=KWIN, path=KWIN_SCRIPTING_PATH,
                interface=KWIN_SCRIPTING_IFACE, member="unloadScript",
                signature="s", body=["lgtvc-fullscreen"]))
        except Exception:
            pass
