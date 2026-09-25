"""Per-device session management: connect/retry/WoL, keepalive, dispatch."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import DeviceConfig
from ..ssap import power
from ..ssap.client import KeyRejected, SsapClient, SsapError
from ..ssap.commands import (
    Command,
    Kind,
    build_luna_raw,
    build_luna_setting,
    build_request,
)
from ..ssap.handshake import KeyStore
from ..ssap.input_socket import send_buttons
from ..ssap.luna import luna_send, set_system_setting
from ..ssap.wol import send_wol

log = logging.getLogger(__name__)

KEEPALIVE_INTERVAL = 60.0
# Wake-on-input probe budget. Short: it sits on the key-press → WoL path when
# the TV is fully off (silent packet drop -> connect timeout), but long enough
# for TLS + SSAP register on a live-but-slow TV. Never cfg.timeout (10 s).
PROBE_TIMEOUT = 3.0


class DeviceSession:
    def __init__(self, cfg: DeviceConfig, keystore: KeyStore, *, dry_run: bool = False):
        self.cfg = cfg
        self.keystore = keystore
        self.dry_run = dry_run
        self.auto_enabled = True   # -autodisable flips this until restart
        self.power_state = "Unknown"  # last observed; surfaced in `status` for MQTT/HA
        self.client = self._new_client()
        self._keepalive: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # Coalesce duplicate high-level operations: a second poweron while one
        # is already running joins the in-flight task instead of launching a
        # parallel connect+WoL loop (which otherwise floods the network — the
        # storm that tripped a transient EACCES during the first cutover).
        self._inflight: dict[str, asyncio.Task] = {}

    @property
    def busy(self) -> bool:
        return bool(self._inflight)

    def _coalesce(self, key: str, coro_factory):
        existing = self._inflight.get(key)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.ensure_future(coro_factory())
        self._inflight[key] = task
        task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
        return task

    def _new_client(self, *, timeout: float | None = None) -> SsapClient:
        return SsapClient(
            self.cfg.host,
            use_ssl=self.cfg.ssl,
            client_key=self.keystore.load(self.cfg.id),
            timeout=timeout if timeout is not None else self.cfg.timeout,
            on_new_key=lambda key: self.keystore.save(self.cfg.id, key),
        )

    # -- connection ---------------------------------------------------------

    def _wol(self) -> None:
        if self.cfg.mac:
            send_wol(
                self.cfg.mac, self.cfg.host, method=self.cfg.wol_method,
                subnet_override=None if self.cfg.subnet == "auto" else self.cfg.subnet,
                interface=self.cfg.interface,
                extra_targets=self.cfg.wol_targets or None,
            )

    async def connect(self, *, wake: bool = False, attempts: int | None = None) -> None:
        """Connect (and register) with retry/backoff. wake=True sends WoL
        before the first attempt and on alternate retries — a WiFi TV waking
        from standby misses early packets while its radio re-associates, but
        re-sending on every attempt just floods the LAN with broadcasts."""
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
                    if wake and attempt % 2 == 1:   # 1st, 3rd, 5th… retry only
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
                self.power_state = "Unknown"
                return
            try:
                state = await power.get_power_state(self.client)
                self.power_state = state.value
            except (ConnectionError, TimeoutError, Exception) as e:
                log.debug("%s: keepalive ping failed: %s", self.cfg.id, e)
                self.power_state = "Unknown"
                await self.client.close()
                return

    # -- high-level actions (coalesced: on/unblank share the "on" slot, and
    #    off/blank share "off", so redundant concurrent requests join a single
    #    in-flight operation instead of each running its own WoL+connect loop) -

    async def power_on(self, *, timeout: float | None = None,
                       set_input: bool = True) -> str:
        return await self._coalesce(
            "on", lambda: self._power_on(timeout=timeout, set_input=set_input))

    async def power_off(self, *, force: bool = False) -> str:
        return await self._coalesce("off", lambda: self._power_off(force=force))

    async def blank(self) -> str:
        return await self._coalesce("off", self._blank)

    async def unblank(self) -> str:
        return await self._coalesce("on", self._unblank)

    async def _power_on(self, *, timeout: float | None = None,
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
        self.power_state = state.value
        await self.settle()
        return state.value

    async def _power_off(self, *, force: bool = False) -> str:
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
        if done:
            self.power_state = "Active Standby"
        await self.disconnect()
        return "off" if done else "refused-wrong-input"

    async def _blank(self) -> str:
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
        if done:
            self.power_state = "Screen Off"
        await self.settle()
        return "blanked" if done else "refused-wrong-input"

    async def _unblank(self) -> str:
        if self.dry_run:
            log.info("[dry-run] %s: would UNBLANK screen", self.cfg.id)
            return "dry-run"
        await self.connect(wake=True)
        await power.unblank_screen(self.client)
        self.power_state = "Active"
        await self.settle()
        return "on"

    async def probe_power_state(self, *, timeout: float = PROBE_TIMEOUT) -> str:
        """Live "is the TV actually on?" probe for the wake-on-input gate.

        A bare TCP probe cannot work here: QuickStart+ TVs keep the API port
        accepting in Active Standby (they reject the SSAP register with close
        1008 "Try Again Later", normalized to ConnectionError), so only a real
        power-state query can tell "on" from "standby-but-reachable".

        Returns a PowerState.value ("Active", "Active Standby", "Screen Off",
        "Suspend", "Unknown"), or "Unreachable" (TCP refused/timeout, or the
        standby register rejection), or "KeyRejected" (TV on, stale pairing
        key — the caller must NOT wake, or every probe pops a pairing prompt).
        Single attempt, no WoL; when the session isn't connected a throwaway
        client is used and always closed, so the probe is side-effect-free
        (a concurrent connect()/keepalive is undisturbed — see power_on)."""
        if self.client.connected:
            try:
                state = await asyncio.wait_for(
                    power.get_power_state(self.client), timeout)
            except (ConnectionError, TimeoutError, SsapError):
                self.power_state = "Unknown"
                await self.client.close()
                return "Unreachable"
            self.power_state = state.value
            return state.value
        probe = self._new_client(timeout=timeout)
        try:
            await probe.connect()
            state = await power.get_power_state(probe)
        except KeyRejected:
            return "KeyRejected"
        except (ConnectionError, TimeoutError, SsapError, OSError):
            return "Unreachable"
        finally:
            await probe.close()
        self.power_state = state.value
        return state.value

    # -- command dispatch -----------------------------------------------------

    async def execute(self, cmd: Command, args: list[Any]) -> Any:
        if cmd.kind == Kind.POWER:
            power_actions = {
                "power_on": self.power_on,
                "power_off": self.power_off,
                "blank": self.blank,
                "unblank": self.unblank,
            }
            action = power_actions.get(cmd.action or "")
            if action is None:
                raise ValueError(f"unknown power action {cmd.action!r}")
            return await action()  # type: ignore[operator]  # union of () -> Awaitable
        if self.dry_run:
            log.info("[dry-run] %s: would run -%s %s", self.cfg.id, cmd.name, args)
            return "dry-run"
        await self.connect()
        try:
            if cmd.kind == Kind.REQUEST:
                uri, payload = build_request(cmd, args)
                return await self.client.request(uri, payload)
            if cmd.kind == Kind.LUNA_SETTING:
                category, settings = build_luna_setting(cmd, args)
                await set_system_setting(self.client, category, settings)
                return {"returnValue": True}
            if cmd.kind == Kind.LUNA_RAW:
                for luna_uri, params in build_luna_raw(cmd, args):
                    await luna_send(self.client, luna_uri, params)
                return {"returnValue": True}
            if cmd.kind == Kind.BUTTON:
                await send_buttons(self.client, [args[0]])
                return {"returnValue": True, "button": args[0]}
            raise ValueError(f"cannot execute {cmd.name} on a device")
        finally:
            await self.settle()
