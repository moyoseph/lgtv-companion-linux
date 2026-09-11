"""Unix-socket IPC server: command dispatch + SYSTEM_* event fanout."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path

from .. import ipc

log = logging.getLogger(__name__)

Dispatcher = Callable[[str, list, list[str]], Awaitable[dict]]


class IpcServer:
    def __init__(self, socket_path: str, dispatcher: Dispatcher):
        self.socket_path = socket_path
        self.dispatcher = dispatcher
        self._server: asyncio.Server | None = None
        self._subscribers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and stat.S_ISSOCK(path.stat().st_mode):
            path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self.socket_path)
        # TV remote control, not privilege escalation: local users may talk to
        # the daemon. Peer identity is logged per connection.
        os.chmod(self.socket_path, 0o666)
        log.info("IPC listening on %s", self.socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for w in list(self._subscribers):
            w.close()

    def broadcast(self, event: str) -> None:
        if event not in ipc.EVENTS:
            log.warning("unknown event %r not broadcast", event)
            return
        dead = []
        for w in self._subscribers:
            try:
                w.write(ipc.encode({"event": event}))
            except (ConnectionError, RuntimeError):
                dead.append(w)
        for w in dead:
            self._subscribers.discard(w)
        log.debug("event %s -> %d subscriber(s)", event, len(self._subscribers))

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        peer = _peer_creds(writer)
        log.debug("IPC client connected (%s)", peer)
        try:
            while True:
                try:
                    frame = await ipc.read_frame(reader)
                except ValueError as e:
                    writer.write(ipc.encode({"ok": False, "error": str(e)}))
                    break
                if frame is None:
                    break
                if frame.get("subscribe"):
                    self._subscribers.add(writer)
                    writer.write(ipc.encode({"subscribed": True}))
                    await writer.drain()
                    continue
                if "report" in frame:
                    # session-agent state reports; consumed by the idle engine (v0.2)
                    log.debug("agent report: %s", frame["report"])
                    continue
                if "cmd" in frame:
                    resp = await self._dispatch(frame)
                    writer.write(ipc.encode(resp))
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._subscribers.discard(writer)
            writer.close()

    async def _dispatch(self, frame: dict) -> dict:
        fid = frame.get("id")
        try:
            result = await self.dispatcher(
                str(frame["cmd"]), list(frame.get("args", [])),
                [str(d) for d in frame.get("devices", [])])
            return {"id": fid, "ok": True, "results": result}
        except Exception as e:
            log.exception("IPC command %s failed", frame.get("cmd"))
            return {"id": fid, "ok": False, "error": str(e)}


def _peer_creds(writer: asyncio.StreamWriter) -> str:
    try:
        import socket as socket_mod
        sock = writer.get_extra_info("socket")
        creds = sock.getsockopt(socket_mod.SOL_SOCKET, socket_mod.SO_PEERCRED, 12)
        import struct
        pid, uid, gid = struct.unpack("3i", creds)
        return f"pid={pid} uid={uid} gid={gid}"
    except (OSError, AttributeError):
        return "unknown peer"
