"""Real D-Bus service mocks (dbus-fast ServiceInterface) for the binding tests.

These export the exact interfaces + signals our code introspects and subscribes
to, on a real bus, so `PowerEvents.start()` / `ScreenLockWatcher.start()` do
their genuine introspect + on_<signal> wiring and receive real signals.
"""

from dbus_fast.service import ServiceInterface, method, signal

# NB: no `from __future__ import annotations` — dbus-fast reads the string
# return annotations (D-Bus signatures) at runtime; PEP 563 stringization
# would corrupt them.

LOGIND = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER = "org.freedesktop.login1.Manager"
SCREENSAVER = "org.freedesktop.ScreenSaver"


class Login1ManagerMock(ServiceInterface):
    def __init__(self):
        super().__init__(LOGIND_MANAGER)

    @signal(name="PrepareForSleep")
    def prepare_for_sleep(self, active) -> "b":
        return active

    @signal(name="PrepareForShutdown")
    def prepare_for_shutdown(self, active) -> "b":
        return active

    @signal(name="PrepareForShutdownWithMetadata")
    def prepare_for_shutdown_with_metadata(self, active, metadata) -> "ba{sv}":
        return [active, metadata]

    @method(name="ListInhibitors")
    def list_inhibitors(self) -> "a(ssssuu)":
        return []


class ScreenSaverMock(ServiceInterface):
    def __init__(self):
        super().__init__(SCREENSAVER)

    @signal(name="ActiveChanged")
    def active_changed(self, active) -> "b":
        return active
