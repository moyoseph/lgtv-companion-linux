"""Idle-veto providers: reasons NOT to blank the screen on user idle."""

from __future__ import annotations

import fnmatch
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

AGENT_REPORT_TTL = 120.0   # ignore stale session state


def running_processes() -> set[str]:
    names: set[str] = set()
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            names.add(comm.read_text().strip().lower())
        except OSError:
            continue
    return names


class VetoEngine:
    """Aggregates process-list, logind-inhibitor and session-agent vetoes.
    check() returns a human-readable reason, or None when idle may proceed."""

    def __init__(self, idle_cfg, logind_manager=None):
        self.cfg = idle_cfg
        self.logind = logind_manager
        self._agent_state: dict = {}
        self._agent_stamp = 0.0

    def update_agent_state(self, report: dict) -> None:
        self._agent_state.update(report)
        self._agent_stamp = time.monotonic()

    @property
    def agent_state(self) -> dict:
        if time.monotonic() - self._agent_stamp > AGENT_REPORT_TTL:
            return {}
        return self._agent_state

    async def check(self) -> str | None:
        state = self.agent_state
        if self.cfg.veto_fullscreen and state.get("fullscreen"):
            return f"fullscreen app: {state['fullscreen']}"
        if self.cfg.veto_mpris != "off" and state.get("mpris_playing"):
            # "foreground_only": only veto when a fullscreen player is present
            # (e.g. fullscreen video) — background music won't hold the TV on
            if self.cfg.veto_mpris == "foreground_only" and not state.get("fullscreen"):
                pass
            else:
                return "media playing (MPRIS)"
        reason = self._check_process_list()
        if reason:
            return reason
        return await self._check_logind_inhibitors()

    def _check_process_list(self) -> str | None:
        entries = [e for e in self.cfg.process_list
                   if "running" in e.get("flags", ["running"])]
        if not entries:
            return None
        procs = running_processes()
        for entry in entries:
            pattern = entry.get("match", "").lower()
            if not pattern:
                continue
            for name in procs:
                if fnmatch.fnmatch(name, pattern):
                    return f"process running: {name}"
        return None

    async def _check_logind_inhibitors(self) -> str | None:
        if self.logind is None:
            return None
        try:
            inhibitors = await self.logind.call_list_inhibitors()
        except Exception as e:
            log.debug("ListInhibitors failed: %s", e)
            return None
        for what, who, why, mode, _uid, _pid in inhibitors:
            if "idle" in what and mode == "block":
                return f"idle inhibitor: {who} ({why})"
        return None
