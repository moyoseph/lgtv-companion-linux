"""Wake the TV on keyboard input while the PC is on but the TV is off.

Port of the proven tv-wake-on-input daemon, upgraded: any EV_KEY press →
`wake_if_needed`, which probes each TV's real power state and powers on the
ones that aren't Active (see Daemon._wake_if_tv_off). The old TCP-reachability
gate lived here, but QuickStart+ TVs keep the API port open in standby, so
"reachable" could not distinguish on from off. This class is now just the
input-side debouncer: cooldown + single-flight.
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
        wake_if_needed: Callable[[str], Awaitable[None]],
        cooldown_s: float = 15.0,
    ):
        self.wake_if_needed = wake_if_needed
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
            await self.wake_if_needed(source)
        except Exception:
            log.exception("wake-on-input failed")
        finally:
            self._busy = False
