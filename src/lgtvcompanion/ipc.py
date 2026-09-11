"""NDJSON IPC protocol shared by daemon, CLI, agent and external scripts.

One JSON object per line over a unix stream socket. Frame types:

  request   {"id": 7, "cmd": "backlight", "args": [80], "devices": ["tv1"]}
  response  {"id": 7, "ok": true, "results": {"tv1": {...}}}
  subscribe {"subscribe": true}            -> subsequent events are delivered
  event     {"event": "SYSTEM_SUSPEND"}
  report    {"report": {"mpris_playing": true, "fullscreen": "mpv"}}   (agent)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

DEFAULT_SOCKET = "/run/lgtv-companion/ipc.sock"

EVENTS = (
    "SYSTEM_DISPLAYS_OFF",
    "SYSTEM_DISPLAYS_ON",
    "SYSTEM_USER_BUSY",
    "SYSTEM_USER_IDLE",
    "SYSTEM_REBOOT",
    "SYSTEM_SHUTDOWN",
    "SYSTEM_RESUME",
    "SYSTEM_SUSPEND",
)

MAX_LINE = 1 << 20


def encode(frame: dict) -> bytes:
    return (json.dumps(frame, separators=(",", ":")) + "\n").encode()


async def read_frame(reader: asyncio.StreamReader) -> dict | None:
    try:
        line = await reader.readline()
    except (ConnectionError, asyncio.IncompleteReadError):
        return None
    if not line:
        return None
    if len(line) > MAX_LINE:
        raise ValueError("IPC frame too large")
    return json.loads(line)


class IpcClient:
    """Small helper for CLI/agent: request/response plus event subscription."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET):
        self.socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ids = 0

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(self.socket_path)

    async def request(self, cmd: str, args: list[Any] | None = None,
                      devices: list[str] | None = None, *, timeout: float = 60.0) -> dict:
        assert self._reader is not None and self._writer is not None
        self._ids += 1
        frame = {"id": self._ids, "cmd": cmd, "args": args or [], "devices": devices or []}
        self._writer.write(encode(frame))
        await self._writer.drain()
        while True:
            resp = await asyncio.wait_for(read_frame(self._reader), timeout=timeout)
            if resp is None:
                raise ConnectionError("daemon closed the IPC connection")
            if resp.get("id") == self._ids:
                return resp
            # events interleaved with the response stream are fine to skip here

    async def subscribe(self):
        """Yield event names forever."""
        assert self._reader is not None and self._writer is not None
        self._writer.write(encode({"subscribe": True}))
        await self._writer.drain()
        while True:
            frame = await read_frame(self._reader)
            if frame is None:
                return
            if "event" in frame:
                yield frame["event"]

    async def send_report(self, report: dict) -> None:
        assert self._writer is not None
        self._writer.write(encode({"report": report}))
        await self._writer.drain()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
