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
from .power_events import PowerEvents, connect_logind_manager
from .server import IpcServer
from .streams import StreamController
from .topology import TopologyWatcher
from .vetoes import VetoEngine
from .wake import WakeOnInput

log = logging.getLogger("lgtvc-daemon")


class Daemon:
    def __init__(self, cfg: config_mod.Config, keystore: KeyStore, socket_path: str,
                 config_path: config_mod.Path | None = None,
                 state_dir: config_mod.Path | None = None):
        self.cfg = cfg
        self._config_path = config_path
        self._state_dir = state_dir or keystore.directory.parent
        self.dry_run = cfg.global_.dry_run
        self.sessions = [
            DeviceSession(d, keystore, dry_run=self.dry_run)
            for d in cfg.devices if d.enabled
        ]
        self.server = IpcServer(socket_path, self.dispatch, on_report=self._on_report)
        self.wake_on_input: WakeOnInput | None = None
        if cfg.global_.wake_on_input.enabled:
            self.wake_on_input = WakeOnInput(
                is_tv_reachable=self._any_tv_reachable,
                wake=self._wake_all,
                cooldown_s=cfg.global_.wake_on_input.cooldown_s)
        self.streams = StreamController(cfg.global_.remote_stream, self._managed)
        self.topology: TopologyWatcher | None = None
        if cfg.global_.topology.enabled:
            self.topology = TopologyWatcher(self._on_topology_change)
        self.vetoes = VetoEngine(cfg.global_.idle)
        self.idle_engine: IdleEngine | None = None
        if cfg.global_.idle.enabled:
            self.idle_engine = IdleEngine(
                minutes=cfg.global_.idle.minutes, vetoes=self.vetoes,
                on_idle=self._idle_blank, on_busy=self._idle_unblank)
        self.power_events: PowerEvents | None = None
        if cfg.global_.daemon_power_events:
            self.power_events = PowerEvents(
                on_suspend=lambda: self._power_all("off"),
                on_resume=self.on_resume,
                on_shutdown=lambda: self._power_all("off"),
                on_reboot=self._noop)
        self._stopping = asyncio.Event()

    async def _noop(self) -> None:
        pass

    async def on_resume(self) -> None:
        for s in self._managed():
            await wait_for_network(s.cfg.host)
        await self._power_all("on")

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
        if "streaming" in report:
            handler = (self.streams.on_connect if report["streaming"]
                       else self.streams.on_disconnect)
            asyncio.get_running_loop().create_task(handler("agent"))
        if "locked" in report:
            asyncio.get_running_loop().create_task(self._on_lock(report["locked"]))
        if report.get("activity") or report.get("input_event"):
            if self.idle_engine is not None:
                self.idle_engine.notify_activity()
            # only key presses wake an off TV; pointer noise shouldn't. Skip if
            # a power operation is already in flight, else a slow/failing wake
            # keeps re-triggering (the TV reads "unreachable" mid-connect).
            if (report.get("key", True) and self.wake_on_input is not None
                    and not any(s.busy for s in self.sessions)):
                self.wake_on_input.notify_input()

    async def _on_lock(self, locked: bool) -> None:
        action = self.cfg.global_.on_lock if locked else self.cfg.global_.on_unlock
        if action == "none":
            return
        verb = {"blank": "blank", "off": "power_off", "on": "power_on"}[action]
        log.info("session %s -> %s", "locked" if locked else "unlocked", action)
        for s in self._managed():
            try:
                await getattr(s, verb)()
            except Exception as e:
                log.error("%s: on-%s action failed: %s",
                          s.cfg.id, "lock" if locked else "unlock", e)

    async def _on_topology_change(self, present_keys: set[str]) -> None:
        for s in self._managed():
            key = s.cfg.unique_display_key
            if not key:
                continue
            try:
                if key in present_keys:
                    result = await s.power_on()
                else:
                    result = await s.power_off()
                log.info("%s: topology %s -> %s", s.cfg.id,
                         "present" if key in present_keys else "absent", result)
            except Exception as e:
                log.error("%s: topology action failed: %s", s.cfg.id, e)
        if self.cfg.global_.topology.keep_on_boot:
            self._save_topology(present_keys)

    def _topology_state_path(self) -> config_mod.Path:
        return self._state_dir / "topology.json"

    def _save_topology(self, present_keys: set[str]) -> None:
        try:
            import json
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._topology_state_path().write_text(json.dumps(sorted(present_keys)))
        except OSError as e:
            log.debug("could not persist topology: %s", e)

    async def _restore_topology(self) -> None:
        """At boot, re-apply the last-seen topology (keep_on_boot) so a
        momentarily-absent display doesn't flip everything off."""
        try:
            import json
            saved = set(json.loads(self._topology_state_path().read_text()))
        except (OSError, ValueError):
            return
        log.info("restoring saved topology: %s", sorted(saved) or "(none)")
        await self._on_topology_change(saved)

    async def _idle_blank(self) -> None:
        self.server.broadcast("SYSTEM_USER_IDLE")
        power_off = self.cfg.global_.idle.action == "power_off"
        for s in self._managed():
            try:
                result = await (s.power_off() if power_off else s.blank())
                if result == "blanked" and self.cfg.global_.idle.mute_speakers \
                        and not self.dry_run:
                    await s.client.request("audio/setMute", {"mute": True})
                log.info("%s: idle %s -> %s", s.cfg.id,
                         "power-off" if power_off else "blank", result)
            except Exception as e:
                log.error("%s: idle action failed: %s", s.cfg.id, e)

    async def _idle_unblank(self) -> None:
        self.server.broadcast("SYSTEM_USER_BUSY")
        power_off = self.cfg.global_.idle.action == "power_off"
        for s in self._managed():
            try:
                # power_off idle → power the TV back on; blank idle → unblank
                result = await (s.power_on() if power_off else s.unblank())
                if not power_off and self.cfg.global_.idle.mute_speakers \
                        and not self.dry_run:
                    await s.client.request("audio/setMute", {"mute": False})
                log.info("%s: idle wake -> %s", s.cfg.id, result)
            except Exception as e:
                log.error("%s: idle wake failed: %s", s.cfg.id, e)

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
        # Suspend/resume/shutdown are handled by the lgtvc-sleep/lgtvc-shutdown
        # oneshot units (system mode) or PowerEvents (user mode); here the
        # daemon powers on at start, and restores a saved topology if asked.
        if self.cfg.global_.topology.enabled and self.cfg.global_.topology.keep_on_boot:
            await self._restore_topology()
            return
        if not self.cfg.global_.power_on_at_boot:
            return
        for s in self._managed():
            await wait_for_network(s.cfg.host)
        await self._power_all("on")

    # -- IPC dispatch ---------------------------------------------------------------

    async def dispatch(self, cmd_name: str, args: list[Any],
                       selectors: list[str]) -> dict:
        if cmd_name == "reload":
            return self.reload()
        if cmd_name == "status":
            return self.status()
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
        if action == "streaming_connect":
            return await self.streams.on_connect("cli")
        if action == "streaming_disconnect":
            return await self.streams.on_disconnect("cli")
        if action == "clear_log":
            return {"log": "journald-managed; use journalctl --vacuum-*"}
        raise ValueError(f"unhandled meta action {action!r}")

    def status(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "idle_enabled": self.idle_engine is not None,
            "idle_active": self.idle_engine.is_idle if self.idle_engine else False,
            "streaming": self.streams.streaming,
            "devices": {
                s.cfg.id: {"name": s.cfg.name, "auto_enabled": s.auto_enabled,
                           "connected": s.client.connected,
                           "power_state": s.power_state,
                           "source_hdmi_input": s.cfg.source_hdmi_input}
                for s in self.sessions},
        }

    def reload(self) -> dict:
        """Re-read config and hot-apply what's safe (per-device tunables, idle
        settings). Device add/remove needs a restart — reported, not applied."""
        if self._config_path is None:
            return {"reloaded": False, "error": "no config path"}
        try:
            new = config_mod.load(self._config_path)
        except (ValueError, OSError) as e:
            return {"reloaded": False, "error": str(e)}
        old_ids = {s.cfg.id for s in self.sessions}
        new_ids = {d.id for d in new.devices if d.enabled}
        restart_needed = old_ids != new_ids
        by_id = {d.id: d for d in new.devices}
        for s in self.sessions:
            if s.cfg.id in by_id:
                nd = by_id[s.cfg.id]
                # hot-apply mutable per-device tunables (host/ssl change needs restart)
                if nd.host != s.cfg.host or nd.ssl != s.cfg.ssl:
                    restart_needed = True
                for f in ("mac", "wol_method", "subnet", "source_hdmi_input",
                          "check_hdmi_input_when_powering_off", "set_hdmi_input",
                          "set_hdmi_input_delay", "standby_mode",
                          "persistent_connection"):
                    setattr(s.cfg, f, getattr(nd, f))
        # idle engine: apply enable/minutes by rebuilding it
        g = new.global_
        self.cfg.global_ = g
        self.vetoes.cfg = g.idle
        if self.idle_engine is not None:
            self.idle_engine.stop()
            self.idle_engine = None
        if g.idle.enabled:
            self.idle_engine = IdleEngine(
                minutes=g.idle.minutes, vetoes=self.vetoes,
                on_idle=self._idle_blank, on_busy=self._idle_unblank)
            self.idle_engine.start()
        if self.wake_on_input is not None:
            self.wake_on_input.cooldown_s = g.wake_on_input.cooldown_s
        log.info("config reloaded (restart_needed=%s)", restart_needed)
        return {"reloaded": True, "restart_needed": restart_needed}

    # -- lifecycle -----------------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stopping.set)
        await self.server.start()
        if self.wake_on_input is not None:
            self.wake_on_input.start()
        if self.idle_engine is not None:
            # logind (negotiate_unix_fd=False) only for the idle-inhibitor veto
            self.vetoes.logind = await connect_logind_manager()
            self.idle_engine.start()
        if self.topology is not None:
            self.topology.start()
        if self.power_events is not None:
            await self.power_events.start()
        sdnotify.ready()
        sdnotify.status("running" + (" (dry-run)" if self.dry_run else ""))
        if self.dry_run:
            log.warning("DRY-RUN mode: no TV commands will be sent")
        asyncio.create_task(self.on_boot())
        log.info("ready%s", " (dry-run)" if self.dry_run else "")
        await self._stopping.wait()
        sdnotify.stopping()
        # plain service stop (upgrade/restart): do not touch TV power
        if self.power_events is not None:
            await self.power_events.stop()
        if self.topology is not None:
            self.topology.stop()
        if self.idle_engine is not None:
            self.idle_engine.stop()
        if self.wake_on_input is not None:
            self.wake_on_input.stop()
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

    daemon = Daemon(cfg, keystore, socket_path,
                    config_path=config_mod.Path(cfg_path), state_dir=state_dir)
    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
