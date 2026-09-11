"""Wake the TV on keyboard input while the PC is on but the TV is off.

Port of the proven tv-wake-on-input daemon: any EV_KEY press → if the TV's
API port is unreachable (TV off / network-asleep), run the power-on sequence.
No-op when the TV is already on, so it never yanks another HDMI source.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .inputdev import EV_KEY, KEY_PRESS, InputMonitor

log = logging.getLogger(__name__)


class WakeOnInput:
    def __init__(
        self,
        *,
        is_tv_reachable: Callable[[], Awaitable[bool]],
        wake: Callable[[], Awaitable[None]],
        cooldown_s: float = 15.0,
    ):
        self.is_tv_reachable = is_tv_reachable
        self.wake = wake
        self.cooldown_s = cooldown_s
        self._monitor = InputMonitor(self._on_event)
        self._last_attempt = 0.0
        self._busy = False

    def start(self) -> None:
        self._monitor.start()

    def stop(self) -> None:
        self._monitor.stop()

    def _on_event(self, path: str, etype: int, code: int, value: int) -> None:
        if etype != EV_KEY or value != KEY_PRESS:
            return
        self.notify_input(path)

    def notify_input(self, source: str = "agent") -> None:
        """Key-press signal — from the local monitor or from the session
        agent over IPC (SELinux blocks the system service from /dev/input, so
        in practice the agent is the usual source)."""
        now = time.monotonic()
        if self._busy or now - self._last_attempt < self.cooldown_s:
            return
        self._last_attempt = now
        asyncio.get_running_loop().create_task(self._maybe_wake(source))

    async def _maybe_wake(self, source: str) -> None:
        self._busy = True
        try:
            if await self.is_tv_reachable():
                return
            log.info("key press on %s while TV unreachable — waking TV", source)
            await self.wake()
        except Exception:
            log.exception("wake-on-input failed")
        finally:
            self._busy = False
