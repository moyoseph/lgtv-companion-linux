"""logind D-Bus: idle-veto ListInhibitors + (user-mode) power-transition signals.

**System installs** handle suspend/resume/shutdown with short-lived systemd
oneshot units bound to sleep.target / poweroff.target — systemd waits for the
Before=sleep.target ExecStart, giving a hard TV-off ordering guarantee. That's
the default and `PowerEvents` below is NOT used there.

**User installs** can't bind those system power targets from a `--user` unit, so
the long-running user daemon handles the transitions itself via logind signals
(`PowerEvents`, enabled by `daemon_power_events` in the config). A `systemd-
inhibit` subprocess holds a delay lock so the TV-off completes before the
network drops. (Holding this inhibitor was once suspected of the EACCES-on-
connect bug, but the real cause was exec'ing the pip console-script entry point;
under `python -m` it's fine.) The bus uses negotiate_unix_fd=False throughout —
enabling fd negotiation *does* poison outbound connects.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable

from dbus_fast import BusType
from dbus_fast.aio import MessageBus

log = logging.getLogger(__name__)

LOGIND = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER = "org.freedesktop.login1.Manager"


async def connect_logind_manager():
    """Return the logind Manager proxy (for ListInhibitors), or None on error.

    negotiate_unix_fd is left OFF: we never receive fds here, and enabling it
    poisons the process's outbound socket connects."""
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM,
                               negotiate_unix_fd=False).connect()
        introspection = await bus.introspect(LOGIND, LOGIND_PATH)
        obj = bus.get_proxy_object(LOGIND, LOGIND_PATH, introspection)
        return obj.get_interface(LOGIND_MANAGER)
    except Exception as e:
        log.warning("could not connect to logind for idle vetoes: %s", e)
        return None


class PowerEvents:
    """Subscribes to logind PrepareForSleep / PrepareForShutdown(WithMetadata)
    and holds a delay inhibitor so TV commands finish before the network drops.
    Used only for user-mode installs (see module docstring). Callbacks are async.
    """

    def __init__(
        self,
        *,
        on_suspend: Callable[[], Awaitable[None]],
        on_resume: Callable[[], Awaitable[None]],
        on_shutdown: Callable[[], Awaitable[None]],
        on_reboot: Callable[[], Awaitable[None]],
    ):
        self.on_suspend = on_suspend
        self.on_resume = on_resume
        self.on_shutdown = on_shutdown
        self.on_reboot = on_reboot
        self._bus: MessageBus | None = None
        self.manager: object = None
        self._inhibitor: asyncio.subprocess.Process | None = None
        self._saw_metadata = False

    async def start(self) -> None:
        self._bus = await MessageBus(
            bus_type=BusType.SYSTEM, negotiate_unix_fd=False).connect()
        introspection = await self._bus.introspect(LOGIND, LOGIND_PATH)
        obj = self._bus.get_proxy_object(LOGIND, LOGIND_PATH, introspection)
        # dbus-fast generates on_<signal>/call_<method> members from introspection
        mgr = obj.get_interface(LOGIND_MANAGER)
        self.manager = mgr
        mgr.on_prepare_for_sleep(self._prepare_for_sleep)  # type: ignore[attr-defined]
        try:
            mgr.on_prepare_for_shutdown_with_metadata(  # type: ignore[attr-defined]
                self._prepare_for_shutdown_with_metadata)
        except AttributeError:
            log.info("PrepareForShutdownWithMetadata unavailable (systemd <255)")
        mgr.on_prepare_for_shutdown(self._prepare_for_shutdown)  # type: ignore[attr-defined]
        await self._take_inhibitor()
        log.info("in-daemon logind power events armed (user mode)")

    async def _take_inhibitor(self) -> None:
        if self._inhibitor is not None and self._inhibitor.returncode is None:
            return
        inhibit_bin = shutil.which("systemd-inhibit")
        if inhibit_bin is None:
            log.warning("systemd-inhibit not found — suspend may race network teardown")
            return
        try:
            self._inhibitor = await asyncio.create_subprocess_exec(
                inhibit_bin, "--what=sleep:shutdown", "--who=LGTV Companion",
                "--why=Synchronizing TV power state", "--mode=delay",
                "sleep", "infinity",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        except OSError as e:
            log.warning("could not take delay inhibitor: %s", e)

    def _release_inhibitor(self) -> None:
        if self._inhibitor is not None and self._inhibitor.returncode is None:
            try:
                self._inhibitor.terminate()
            except ProcessLookupError:
                pass
        self._inhibitor = None

    def _prepare_for_sleep(self, start: bool) -> None:
        if start:
            asyncio.create_task(self._run_then_release(self.on_suspend))
        else:
            asyncio.create_task(self._resumed())

    def _prepare_for_shutdown_with_metadata(self, start: bool, metadata: dict) -> None:
        self._saw_metadata = True
        if not start:
            return
        stype = metadata.get("type")
        stype = getattr(stype, "value", stype)
        if str(stype) == "reboot":
            asyncio.create_task(self._run_then_release(self.on_reboot))
        else:
            asyncio.create_task(self._run_then_release(self.on_shutdown))

    def _prepare_for_shutdown(self, start: bool) -> None:
        if not start or self._saw_metadata:
            return
        asyncio.create_task(self._run_then_release(self.on_shutdown))

    async def _run_then_release(self, callback: Callable[[], Awaitable[None]]) -> None:
        try:
            await callback()
        except Exception:
            log.exception("power-event callback failed")
        finally:
            self._release_inhibitor()

    async def _resumed(self) -> None:
        await self._take_inhibitor()
        try:
            await self.on_resume()
        except Exception:
            log.exception("resume callback failed")

    async def stop(self) -> None:
        self._release_inhibitor()
        if self._bus is not None:
            self._bus.disconnect()
