"""User-idle engine: blank the TV after inactivity, wake it on activity.

Activity arrives from the session agent (IPC reports) and, where readable,
the daemon's own input monitor. On timeout — and with no veto standing — all
managed TVs get a screen-blank (plus optional speaker mute). Any activity
while idle unblanks immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .vetoes import VetoEngine

log = logging.getLogger(__name__)

VETO_RECHECK_S = 60.0


class IdleEngine:
    def __init__(
        self,
        *,
        minutes: int,
        vetoes: VetoEngine,
        on_idle: Callable[[], Awaitable[None]],
        on_busy: Callable[[], Awaitable[None]],
    ):
        self.timeout_s = minutes * 60
        self.vetoes = vetoes
        self.on_idle = on_idle
        self.on_busy = on_busy
        self.is_idle = False
        self._last_activity = time.monotonic()
        self._task: asyncio.Task | None = None
        self._forced_idle = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="idle-engine")
        log.info("idle engine armed: %d min timeout", self.timeout_s // 60)

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def notify_activity(self) -> None:
        self._last_activity = time.monotonic()
        if self.is_idle and not self._forced_idle:
            asyncio.get_running_loop().create_task(self._go_busy())

    async def force_idle(self) -> None:
        self._forced_idle = True
        await self._go_idle(forced=True)

    async def force_unidle(self) -> None:
        self._forced_idle = False
        await self._go_busy()

    async def _loop(self) -> None:
        while True:
            if self.is_idle:
                await asyncio.sleep(1.0)
                continue
            remaining = self.timeout_s - (time.monotonic() - self._last_activity)
            if remaining > 0:
                await asyncio.sleep(min(remaining, 30.0))
                continue
            reason = await self.vetoes.check()
            if reason:
                log.debug("idle vetoed: %s", reason)
                await asyncio.sleep(VETO_RECHECK_S)
                continue
            await self._go_idle()

    async def _go_idle(self, forced: bool = False) -> None:
        if self.is_idle:
            return
        self.is_idle = True
        log.info("user idle%s — blanking", " (forced)" if forced else "")
        try:
            await self.on_idle()
        except Exception:
            log.exception("idle action failed")

    async def _go_busy(self) -> None:
        if not self.is_idle:
            return
        self.is_idle = False
        self._last_activity = time.monotonic()
        log.info("user busy — unblanking")
        try:
            await self.on_busy()
        except Exception:
            log.exception("busy action failed")
