"""lgtvc-daemon: asyncio supervisor wiring devices, power events, wake, IPC."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Any

from .. import config as config_mod
from .. import ipc, sdnotify
from ..ssap.commands import Kind, lookup
from ..ssap.handshake import KeyStore
from .devices import DeviceSession
from .idle import IdleEngine
from .network import wait_for_network
from .power_events import PowerEvents
from .server import IpcServer
from .vetoes import VetoEngine
from .wake import WakeOnInput

log = logging.getLogger("lgtvc-daemon")


class Daemon:
    def __init__(self, cfg: config_mod.Config, keystore: KeyStore, socket_path: str):
        self.cfg = cfg
        self.dry_run = cfg.global_.dry_run
        self.sessions = [
            DeviceSession(d, keystore, dry_run=self.dry_run)
            for d in cfg.devices if d.enabled
        ]
        self.server = IpcServer(socket_path, self.dispatch, on_report=self._on_report)
        self.power_events = PowerEvents(
            on_suspend=self.on_suspend, on_resume=self.on_resume,
            on_shutdown=self.on_shutdown, on_reboot=self.on_reboot)
        self.wake_on_input: WakeOnInput | None = None
        if cfg.global_.wake_on_input.enabled:
            self.wake_on_input = WakeOnInput(
                is_tv_reachable=self._any_tv_reachable,
                wake=self._wake_all,
                cooldown_s=cfg.global_.wake_on_input.cooldown_s)
        self.vetoes = VetoEngine(cfg.global_.idle)
        self.idle_engine: IdleEngine | None = None
        if cfg.global_.idle.enabled:
            self.idle_engine = IdleEngine(
                minutes=cfg.global_.idle.minutes, vetoes=self.vetoes,
                on_idle=self._idle_blank, on_busy=self._idle_unblank)
        self._stopping = asyncio.Event()

    # -- device selection ------------------------------------------------------

    def _managed(self) -> list[DeviceSession]:
        return [s for s in self.sessions if s.auto_enabled]

    def _select(self, selectors: list[str]) -> list[DeviceSession]:
        if not selectors:
            return list(self.sessions)
        out = []
        for sel in selectors:
            for s in self.sessions:
                if sel.lower() in (s.cfg.id.lower(), s.cfg.name.lower()):
                    out.append(s)
                    break
            else:
                raise ValueError(f"unknown device: {sel!r}")
        return out

    def _on_report(self, report: dict) -> None:
        if "mpris_playing" in report or "fullscreen" in report:
            self.vetoes.update_agent_state(report)
        if report.get("activity") or report.get("input_event"):
            if self.idle_engine is not None:
                self.idle_engine.notify_activity()
            # only key presses wake an off TV; pointer noise shouldn't
            if report.get("key", True) and self.wake_on_input is not None:
                self.wake_on_input.notify_input()

    async def _idle_blank(self) -> None:
        self.server.broadcast("SYSTEM_USER_IDLE")
        for s in self._managed():
            try:
                result = await s.blank()
                if result == "blanked" and self.cfg.global_.idle.mute_speakers \
                        and not self.dry_run:
                    await s.client.request("audio/setMute", {"mute": True})
                log.info("%s: idle blank -> %s", s.cfg.id, result)
            except Exception as e:
                log.error("%s: idle blank failed: %s", s.cfg.id, e)

    async def _idle_unblank(self) -> None:
        self.server.broadcast("SYSTEM_USER_BUSY")
        for s in self._managed():
            try:
                result = await s.unblank()
                if self.cfg.global_.idle.mute_speakers and not self.dry_run:
                    await s.client.request("audio/setMute", {"mute": False})
                log.info("%s: idle unblank -> %s", s.cfg.id, result)
            except Exception as e:
                log.error("%s: idle unblank failed: %s", s.cfg.id, e)

    async def _any_tv_reachable(self) -> bool:
        for s in self._managed():
            if await s.is_reachable():
                return True
        return False

    async def _wake_all(self) -> None:
        await self._power_all("on")

    # -- power orchestration -----------------------------------------------------

    async def _power_all(self, action: str) -> None:
        sessions = self._managed()
        if not sessions:
            return
        results = await asyncio.gather(
            *[getattr(s, "power_on" if action == "on" else "power_off")()
              for s in sessions],
            return_exceptions=True)
        for s, r in zip(sessions, results, strict=True):
            if isinstance(r, BaseException):
                log.error("%s: power %s failed: %s", s.cfg.id, action, r)
            else:
                log.info("%s: power %s -> %s", s.cfg.id, action, r)

    async def on_boot(self) -> None:
        if not self.cfg.global_.power_on_at_boot:
            return
        for s in self._managed():
            await wait_for_network(s.cfg.host)
        await self._power_all("on")

    async def on_suspend(self) -> None:
        log.info("suspend: powering TVs off")
        self.server.broadcast("SYSTEM_SUSPEND")
        await asyncio.wait_for(self._power_all("off"), timeout=4.0)

    async def on_resume(self) -> None:
        log.info("resume: powering TVs on")
        self.server.broadcast("SYSTEM_RESUME")
        for s in self._managed():
            await wait_for_network(s.cfg.host)
        await self._power_all("on")

    async def on_shutdown(self) -> None:
        log.info("shutdown: powering TVs off")
        self.server.broadcast("SYSTEM_SHUTDOWN")
        await asyncio.wait_for(self._power_all("off"), timeout=4.0)

    async def on_reboot(self) -> None:
        log.info("reboot: leaving TVs on")
        self.server.broadcast("SYSTEM_REBOOT")

    # -- IPC dispatch ---------------------------------------------------------------

    async def dispatch(self, cmd_name: str, args: list[Any],
                       selectors: list[str]) -> dict:
        cmd = lookup(cmd_name)
        if cmd is None:
            raise ValueError(f"unknown command: {cmd_name!r}")
        sessions = self._select(selectors)
        if cmd.kind == Kind.META:
            return await self._meta(cmd.action or "", sessions)
        results: dict[str, Any] = {}
        for s in sessions:
            try:
                results[s.cfg.id] = await s.execute(cmd, args)
            except Exception as e:
                results[s.cfg.id] = {"error": str(e)}
        return results

    async def _meta(self, action: str, sessions: list[DeviceSession]) -> dict:
        if action == "auto_enable":
            for s in sessions:
                s.auto_enabled = True
            return {s.cfg.id: "auto-enabled" for s in sessions}
        if action == "auto_disable":
            for s in sessions:
                s.auto_enabled = False
            return {s.cfg.id: "auto-disabled" for s in sessions}
        if action == "force_idle":
            if self.idle_engine is not None:
                await self.idle_engine.force_idle()
                return {"idle": "forced"}
            self.server.broadcast("SYSTEM_USER_IDLE")
            return {s.cfg.id: await s.blank() for s in sessions}
        if action == "force_unidle":
            if self.idle_engine is not None:
                await self.idle_engine.force_unidle()
                return {"idle": "released"}
            self.server.broadcast("SYSTEM_USER_BUSY")
            return {s.cfg.id: await s.unblank() for s in sessions}
        if action in ("streaming_connect", "streaming_disconnect"):
            # remote-stream reactions land in v0.3; expose the hook now
            log.info("external streaming signal: %s", action)
            return {"streaming": action}
        if action == "clear_log":
            return {"log": "journald-managed; use journalctl --vacuum-*"}
        raise ValueError(f"unhandled meta action {action!r}")

    # -- lifecycle -----------------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stopping.set)
        await self.server.start()
        await self.power_events.start()
        self.vetoes.logind = self.power_events.manager
        if self.wake_on_input is not None:
            self.wake_on_input.start()
        if self.idle_engine is not None:
            self.idle_engine.start()
        sdnotify.ready()
        sdnotify.status("running" + (" (dry-run)" if self.dry_run else ""))
        if self.dry_run:
            log.warning("DRY-RUN mode: no TV commands will be sent")
        asyncio.create_task(self.on_boot())
        await self._stopping.wait()
        sdnotify.stopping()
        # plain service stop (upgrade/restart): do not touch TV power
        if self.idle_engine is not None:
            self.idle_engine.stop()
        if self.wake_on_input is not None:
            self.wake_on_input.stop()
        await self.power_events.stop()
        await self.server.stop()
        for s in self.sessions:
            await s.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="LGTV Companion daemon")
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--socket", default=None, help="IPC socket path")
    args = parser.parse_args()

    cfg_path = args.config or config_mod.find_config()
    if cfg_path is None:
        sys.exit("no config found — run: lgtvc setup (or pass --config)")
    cfg = config_mod.load(config_mod.Path(cfg_path))

    logging.basicConfig(
        level=getattr(logging, cfg.global_.log_level.upper(), logging.INFO),
        format="%(name)s: %(message)s")

    if os.geteuid() == 0 or str(cfg_path) == str(config_mod.SYSTEM_CONFIG):
        state_dir = config_mod.SYSTEM_STATE
    else:
        state_dir = config_mod.user_state_path()
    keystore = KeyStore(state_dir / "keys")

    socket_path = args.socket or os.environ.get("LGTVC_SOCKET", ipc.DEFAULT_SOCKET)
    runtime_dir = os.environ.get("RUNTIME_DIRECTORY")
    if args.socket is None and runtime_dir:
        socket_path = os.path.join(runtime_dir.split(":")[0], "ipc.sock")

    daemon = Daemon(cfg, keystore, socket_path)
    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
