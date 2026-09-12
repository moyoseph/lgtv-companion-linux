"""Session lock detection for the "blank/off the TV when the screen locks"
feature (upstream #288).

Watches the session-bus `org.freedesktop.ScreenSaver` `ActiveChanged(bool)`
signal — exposed by KDE, GNOME and most freedesktop-compliant lockers — and
reports lock state to the daemon, which applies the on_lock/on_unlock policy.
Gamescope Game Mode has no locker, so this simply never fires.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from dbus_fast import Message
from dbus_fast.aio import MessageBus

log = logging.getLogger(__name__)

SCREENSAVER = "org.freedesktop.ScreenSaver"
# KDE exposes both; GNOME uses the fully-qualified path. Try in order.
SCREENSAVER_PATHS = ("/org/freedesktop/ScreenSaver", "/ScreenSaver")


class ScreenLockWatcher:
    """Subscribe to ScreenSaver ActiveChanged; call on_change(locked: bool)."""

    def __init__(self, bus: MessageBus, on_change: Callable[[bool], None]):
        self.bus = bus
        self.on_change = on_change
        self._iface = None

    async def available(self) -> bool:
        try:
            reply = await self.bus.call(Message(
                destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus", member="NameHasOwner",
                signature="s", body=[SCREENSAVER]))
            return bool(reply.body and reply.body[0])
        except Exception:
            return False

    async def start(self) -> bool:
        """Subscribe to ActiveChanged. Returns True if wired up."""
        for path in SCREENSAVER_PATHS:
            try:
                introspection = await self.bus.introspect(SCREENSAVER, path)
                obj = self.bus.get_proxy_object(SCREENSAVER, path, introspection)
                iface = obj.get_interface(SCREENSAVER)
                iface.on_active_changed(self._on_active_changed)
                self._iface = iface
                log.info("watching screen lock via %s %s", SCREENSAVER, path)
                return True
            except Exception as e:
                log.debug("ScreenSaver at %s unavailable: %s", path, e)
        return False

    def _on_active_changed(self, active: bool) -> None:
        self.on_change(bool(active))
