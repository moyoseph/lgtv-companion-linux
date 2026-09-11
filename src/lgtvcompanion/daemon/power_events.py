"""logind integration: suspend/resume/shutdown signals + delay inhibitors."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from dbus_fast import BusType
from dbus_fast.aio import MessageBus

log = logging.getLogger(__name__)

LOGIND = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER = "org.freedesktop.login1.Manager"


class PowerEvents:
    """Subscribes to PrepareForSleep / PrepareForShutdown(WithMetadata) and
    holds a delay inhibitor so TV commands can run before the network drops.

    Callbacks (all async): on_suspend, on_resume, on_shutdown, on_reboot.
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
        self._manager = None
        self._inhibit_fd: int | None = None
        self._shutdown_type: str | None = None
        self._saw_metadata = False

    async def start(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        introspection = await self._bus.introspect(LOGIND, LOGIND_PATH)
        obj = self._bus.get_proxy_object(LOGIND, LOGIND_PATH, introspection)
        self._manager = obj.get_interface(LOGIND_MANAGER)

        self._manager.on_prepare_for_sleep(self._prepare_for_sleep)
        try:
            self._manager.on_prepare_for_shutdown_with_metadata(
                self._prepare_for_shutdown_with_metadata)
            log.debug("subscribed to PrepareForShutdownWithMetadata")
        except AttributeError:
            log.info("PrepareForShutdownWithMetadata unavailable (systemd <255); "
                     "reboot detection relies on the Conflicts=reboot.target unit")
        self._manager.on_prepare_for_shutdown(self._prepare_for_shutdown)

        await self._take_inhibitor()
        log.info("logind power events armed (delay inhibitor held)")

    async def _take_inhibitor(self) -> None:
        if self._inhibit_fd is not None:
            return
        try:
            fd = await self._manager.call_inhibit(
                "sleep:shutdown", "LGTV Companion",
                "Synchronizing TV power state", "delay")
            # dbus-fast unmarshals UNIX_FD as the raw fd integer
            self._inhibit_fd = fd
        except Exception as e:
            log.warning("could not take delay inhibitor: %s", e)

    def _release_inhibitor(self) -> None:
        if self._inhibit_fd is not None:
            try:
                os.close(self._inhibit_fd)
            except OSError:
                pass
            self._inhibit_fd = None

    # -- signal handlers (sync entry, schedule async work) --------------------

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
        stype = getattr(stype, "value", stype)  # unwrap Variant
        self._shutdown_type = str(stype) if stype else None
        log.info("shutdown starting, type=%s", self._shutdown_type)
        if self._shutdown_type == "reboot":
            asyncio.create_task(self._run_then_release(self.on_reboot))
        else:
            asyncio.create_task(self._run_then_release(self.on_shutdown))

    def _prepare_for_shutdown(self, start: bool) -> None:
        if not start or self._saw_metadata:
            return  # metadata variant already handled (fires alongside)
        log.info("shutdown starting (no metadata signal) — treating as poweroff")
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
