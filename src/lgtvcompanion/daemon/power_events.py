"""Minimal logind D-Bus client for the idle-veto ListInhibitors query.

Power transitions (suspend/resume/shutdown/reboot) are handled by short-lived
systemd oneshot units bound to sleep.target / poweroff.target — the proven
legacy pattern — NOT inside this long-running daemon. Two reasons:

  * systemd natively waits for a Before=sleep.target oneshot's ExecStart to
    finish before suspending, giving the TV-off a hard ordering guarantee
    without any delay inhibitor.
  * Holding a delay inhibitor inside the daemon process (whether received as a
    D-Bus UNIX_FD or via a systemd-inhibit child) makes every subsequent
    outbound socket connect fail with EACCES — an opaque interaction that cost
    a full debugging session to pin down. Avoid it entirely.

So the daemon only touches logind to read the idle-inhibitor list, and only
when user-idle mode is enabled. The connection uses negotiate_unix_fd=False.
"""

from __future__ import annotations

import logging

from dbus_fast import BusType
from dbus_fast.aio import MessageBus

log = logging.getLogger(__name__)

LOGIND = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER = "org.freedesktop.login1.Manager"


async def connect_logind_manager():
    """Return the logind Manager proxy (for ListInhibitors), or None on error.

    negotiate_unix_fd is left OFF: we never receive fds here, and enabling it
    poisons the process's outbound socket connects (see module docstring)."""
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM,
                               negotiate_unix_fd=False).connect()
        introspection = await bus.introspect(LOGIND, LOGIND_PATH)
        obj = bus.get_proxy_object(LOGIND, LOGIND_PATH, introspection)
        return obj.get_interface(LOGIND_MANAGER)
    except Exception as e:
        log.warning("could not connect to logind for idle vetoes: %s", e)
        return None
