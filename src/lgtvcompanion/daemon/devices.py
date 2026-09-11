"""Per-device session management: connect/retry/WoL, keepalive, dispatch."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import DeviceConfig
from ..ssap import power
from ..ssap.client import KeyRejected, SsapClient
from ..ssap.commands import Command, Kind, build_request
from ..ssap.handshake import KeyStore
from ..ssap.luna import set_system_setting
from ..ssap.wol import send_wol

log = logging.getLogger(__name__)

KEEPALIVE_INTERVAL = 60.0


class DeviceSession:
    def __init__(self, cfg: DeviceConfig, keystore: KeyStore, *, dry_run: bool = False):
        self.cfg = cfg
        self.keystore = keystore
        self.dry_run = dry_run
        self.auto_enabled = True   # -autodisable flips this until restart
        self.client = self._new_client()
        self._keepalive: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def _new_client(self) -> SsapClient:
        return SsapClient(
            self.cfg.host,
            use_ssl=self.cfg.ssl,
            client_key=self.keystore.load(self.cfg.id),
            timeout=self.cfg.timeout,
            on_new_key=lambda key: self.keystore.save(self.cfg.id, key),
        )

    # -- connection ---------------------------------------------------------

    def _wol(self) -> None:
        if self.cfg.mac:
            send_wol(
                self.cfg.mac, self.cfg.host, method=self.cfg.wol_method,
                subnet_override=None if self.cfg.subnet == "auto" else self.cfg.subnet,
            )

    async def connect(self, *, wake: bool = False, attempts: int | None = None) -> None:
        """Connect (and register) with retry/backoff. wake=True sends WoL
        before the first attempt and again on every retry — a WiFi TV waking
        from standby misses early packets while its radio re-associates."""
        async with self._lock:
            if self.client.connected:
                return
            attempts = attempts or self.cfg.retry_attempts
            if wake:
                self._wol()
            last_err: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    self.client = self._new_client()
                    await self.client.connect()
                    return
                except KeyRejected:
                    raise
                except (OSError, ConnectionError, TimeoutError) as e:
                    last_err = e
                if attempt < attempts:
                    if wake:
                        self._wol()
                    delay = min(self.cfg.backoff_max,
                                self.cfg.backoff_base * 2 ** (attempt - 1))
                    log.info("%s: attempt %d/%d failed (%s) — retrying in %.0fs",
                             self.cfg.id, attempt, attempts, last_err, delay)
                    await asyncio.sleep(delay)
            raise ConnectionError(
                f"{self.cfg.id}: unreachable after {attempts} attempts: {last_err}")

    async def try_connect_quick(self) -> bool:
        """Single fast attempt — used before power-off/blank where an
        unreachable TV simply means it is already off."""
        try:
            await self.connect(attempts=1)
            return True
        except (ConnectionError, KeyRejected):
            return False

    async def disconnect(self) -> None:
        self.stop_keepalive()
        await self.client.close()

    async def settle(self) -> None:
        """Apply the persistent-connection policy after a burst of work."""
        mode = self.cfg.persistent_connection
        if mode == "off":
            await self.disconnect()
        elif mode == "keepalive":
            self.start_keepalive()

    def start_keepalive(self) -> None:
        if self._keepalive is None or self._keepalive.done():
            self._keepalive = asyncio.create_task(
                self._keepalive_loop(), name=f"keepalive-{self.cfg.id}")

    def stop_keepalive(self) -> None:
        if self._keepalive is not None:
            self._keepalive.cancel()
            self._keepalive = None

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if not self.client.connected:
                return
            try:
                await power.get_power_state(self.client)
            except (ConnectionError, TimeoutError, Exception) as e:
                log.debug("%s: keepalive ping failed: %s", self.cfg.id, e)
                await self.client.close()
                return

    # -- high-level actions ---------------------------------------------------

    async def power_on(self, *, timeout: float | None = None,
                       set_input: bool = True) -> str:
        if self.dry_run:
            log.info("[dry-run] %s: would power ON (+input hdmi%s)",
                     self.cfg.id, self.cfg.set_hdmi_input if set_input else "-")
            return "dry-run"
        await self.connect(wake=True)
        state = await power.power_on(
            self.client,
            timeout=timeout or 40.0,
            set_hdmi_input=self.cfg.set_hdmi_input if set_input else None,
            set_hdmi_input_delay=self.cfg.set_hdmi_input_delay,
        )
        await self.settle()
        return state.value

    async def power_off(self, *, force: bool = False) -> str:
        if self.dry_run:
            log.info("[dry-run] %s: would power OFF (guard=%s)",
                     self.cfg.id, self.cfg.check_hdmi_input_when_powering_off)
            return "dry-run"
        if not await self.try_connect_quick():
            return "already-off"
        done = await power.power_off(
            self.client,
            source_hdmi_input=self.cfg.source_hdmi_input,
            check_hdmi_input=self.cfg.check_hdmi_input_when_powering_off,
            standby_mode=self.cfg.standby_mode,
            force=force,
        )
        await self.disconnect()
        return "off" if done else "refused-wrong-input"

    async def blank(self) -> str:
        if self.dry_run:
            log.info("[dry-run] %s: would BLANK screen", self.cfg.id)
            return "dry-run"
        if not await self.try_connect_quick():
            return "already-off"
        done = await power.blank_screen(
            self.client,
            source_hdmi_input=self.cfg.source_hdmi_input,
            check_hdmi_input=self.cfg.check_hdmi_input_when_powering_off,
        )
        await self.settle()
        return "blanked" if done else "refused-wrong-input"

    async def unblank(self) -> str:
        if self.dry_run:
            log.info("[dry-run] %s: would UNBLANK screen", self.cfg.id)
            return "dry-run"
        await self.connect(wake=True)
        await power.unblank_screen(self.client)
        await self.settle()
        return "on"

    async def is_reachable(self, timeout: float = 1.0) -> bool:
        port = 3001 if self.cfg.ssl else 3000
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.cfg.host, port), timeout=timeout)
            writer.close()
            return True
        except (OSError, TimeoutError):
            return False

    # -- command dispatch -----------------------------------------------------

    async def execute(self, cmd: Command, args: list[Any]) -> Any:
        if cmd.kind == Kind.POWER:
            return await {
                "power_on": self.power_on,
                "power_off": self.power_off,
                "blank": self.blank,
                "unblank": self.unblank,
            }[cmd.action]()
        if self.dry_run:
            log.info("[dry-run] %s: would run -%s %s", self.cfg.id, cmd.name, args)
            return "dry-run"
        await self.connect()
        try:
            if cmd.kind == Kind.REQUEST:
                uri, payload = build_request(cmd, args)
                return await self.client.request(uri, payload)
            if cmd.kind == Kind.LUNA_SETTING:
                assert cmd.luna_category and cmd.luna_setting
                await set_system_setting(
                    self.client, cmd.luna_category, {cmd.luna_setting: args[0]})
                return {"returnValue": True}
            raise ValueError(f"cannot execute {cmd.name} on a device")
        finally:
            await self.settle()
