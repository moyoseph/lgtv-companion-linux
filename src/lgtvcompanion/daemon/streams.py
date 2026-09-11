"""Remote-stream policy: what happens to the TVs when a stream starts/ends.

Detection arrives from the session agent (Sunshine log) or the explicit
-streaming_connect / -streaming_disconnect verbs. On connect the local panel
is redundant — blank or power it off; on disconnect restore per config.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from ..config import RemoteStreamConfig

log = logging.getLogger(__name__)


class StreamController:
    def __init__(self, cfg: RemoteStreamConfig, sessions: Callable[[], Sequence],
                 broadcast: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.sessions = sessions
        self.streaming = False
        self._touched: set[str] = set()   # devices we changed, for "restore"

    async def on_connect(self, source: str = "agent") -> dict:
        if self.streaming:
            return {"streaming": "already-connected"}
        self.streaming = True
        self._touched.clear()
        if not self.cfg.enabled:
            log.info("stream connected (%s) — remote_stream disabled, no action",
                     source)
            return {"streaming": "connected (no action: disabled)"}
        results = {}
        for s in self.sessions():
            try:
                if self.cfg.on_connect == "off":
                    result = await s.power_off()
                    changed = result in ("off", "dry-run")
                else:
                    result = await s.blank()
                    changed = result in ("blanked", "dry-run")
                if changed:
                    self._touched.add(s.cfg.id)
                results[s.cfg.id] = result
                log.info("%s: stream connect -> %s (%s)",
                         s.cfg.id, self.cfg.on_connect, result)
            except Exception as e:
                results[s.cfg.id] = f"error: {e}"
                log.error("%s: stream-connect action failed: %s", s.cfg.id, e)
        return results

    async def on_disconnect(self, source: str = "agent") -> dict:
        if not self.streaming:
            return {"streaming": "not-connected"}
        self.streaming = False
        if not self.cfg.enabled or self.cfg.on_disconnect == "keep_off":
            return {"streaming": "disconnected (keep off)"}
        results = {}
        for s in self.sessions():
            if self.cfg.on_disconnect == "restore" and s.cfg.id not in self._touched:
                results[s.cfg.id] = "untouched"
                continue
            try:
                results[s.cfg.id] = await s.power_on()
                log.info("%s: stream disconnect -> on (%s)",
                         s.cfg.id, results[s.cfg.id])
            except Exception as e:
                results[s.cfg.id] = f"error: {e}"
                log.error("%s: stream-disconnect action failed: %s", s.cfg.id, e)
        self._touched.clear()
        return results
