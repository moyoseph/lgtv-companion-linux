"""Asyncio SSAP websocket client for LG webOS TVs."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from .handshake import register_payload

log = logging.getLogger(__name__)

PORT_PLAIN = 3000
PORT_SSL = 3001
DEFAULT_TIMEOUT = 10.0
PAIRING_TIMEOUT = 60.0

# webOS returns -102 ("must be screen off" family) when the screen is already
# in the requested state; callers treat it as success.
BENIGN_ERROR_CODES = {"-102", -102}


class SsapError(Exception):
    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload or {}


class PairingRejected(SsapError):
    pass


class KeyRejected(SsapError):
    """Stored client key was not accepted; re-pairing is required."""


def _ssl_context() -> ssl.SSLContext:
    # TVs present self-signed certs; upstream and the legacy setup both skip
    # verification, and the LAN threat model matches.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class SsapClient:
    """One websocket session to one TV.

    connect() performs the transport connect + SSAP registration. request()
    correlates responses by message id. A single background reader task routes
    incoming frames to pending futures and subscription callbacks.
    """

    def __init__(
        self,
        host: str,
        *,
        use_ssl: bool = True,
        client_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        on_new_key: Callable[[str], None] | None = None,
        on_pairing_prompt: Callable[[], None] | None = None,
    ):
        self.host = host
        self.use_ssl = use_ssl
        self.client_key = client_key
        self.timeout = timeout
        self.on_new_key = on_new_key
        self.on_pairing_prompt = on_pairing_prompt
        self._ws: websockets.ClientConnection | None = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._subscriptions: dict[str, Callable[[dict], Awaitable[None] | None]] = {}
        self._ids = itertools.count(1)

    @property
    def url(self) -> str:
        if self.use_ssl:
            return f"wss://{self.host}:{PORT_SSL}/"
        return f"ws://{self.host}:{PORT_PLAIN}/"

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self, *, pairing: bool = False) -> None:
        ssl_ctx = _ssl_context() if self.use_ssl else None
        self._ws = await asyncio.wait_for(
            websockets.connect(
                self.url, ssl=ssl_ctx, open_timeout=self.timeout, close_timeout=2
            ),
            timeout=self.timeout + 2,
        )
        try:
            await self._register(pairing=pairing)
        except BaseException:
            await self.close()
            raise
        self._reader = asyncio.create_task(self._read_loop(), name=f"ssap-read-{self.host}")

    async def _register(self, *, pairing: bool) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(register_payload(self.client_key)))
        deadline = PAIRING_TIMEOUT if pairing else self.timeout
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=deadline)
            msg = json.loads(raw)
            mtype = msg.get("type")
            payload = msg.get("payload", {})
            if mtype == "response" and payload.get("pairingType") == "PROMPT":
                if not pairing and self.client_key:
                    # TV is asking for a prompt although we sent a key: the
                    # key is no longer valid.
                    raise KeyRejected("stored client key rejected by TV", payload)
                deadline = PAIRING_TIMEOUT
                if self.on_pairing_prompt:
                    self.on_pairing_prompt()
                continue
            if mtype == "registered":
                key = payload.get("client-key")
                if key and key != self.client_key:
                    self.client_key = key
                    if self.on_new_key:
                        self.on_new_key(key)
                return
            if mtype == "error":
                text = str(msg.get("error", msg))
                if "rejected" in text.lower() or "denied" in text.lower():
                    raise PairingRejected(text, payload)
                raise SsapError(f"registration failed: {text}", payload)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.warning("%s: non-JSON frame ignored", self.host)
                    continue
                mid = str(msg.get("id"))
                fut = self._pending.get(mid)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                    continue
                sub = self._subscriptions.get(mid)
                if sub is not None:
                    res = sub(msg)
                    if asyncio.iscoroutine(res):
                        await res
        except websockets.ConnectionClosed as e:
            log.debug("%s: connection closed (%s)", self.host, e)
        except Exception:
            log.exception("%s: reader failed", self.host)
        finally:
            err = ConnectionError(f"{self.host}: connection lost")
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()
            self._ws = None

    async def request(
        self,
        uri: str,
        payload: dict | None = None,
        *,
        timeout: float | None = None,
        benign_errors: bool = True,
    ) -> dict:
        """Send ssap:// request, return the response payload.

        Raises SsapError on TV-reported errors (except the benign -102 family),
        ConnectionError if the socket drops.
        """
        if self._ws is None:
            raise ConnectionError(f"{self.host}: not connected")
        mid = str(next(self._ids))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        msg: dict[str, Any] = {"id": mid, "type": "request", "uri": f"ssap://{uri}"}
        if payload is not None:
            msg["payload"] = payload
        try:
            await self._ws.send(json.dumps(msg))
            resp = await asyncio.wait_for(fut, timeout=timeout or self.timeout)
        finally:
            self._pending.pop(mid, None)
        rpayload = resp.get("payload", {})
        if resp.get("type") == "error":
            if benign_errors and rpayload.get("errorCode") in BENIGN_ERROR_CODES:
                return rpayload
            raise SsapError(f"{uri}: {resp.get('error', rpayload)}", rpayload)
        return rpayload

    async def subscribe(
        self, uri: str, callback: Callable[[dict], Awaitable[None] | None],
        payload: dict | None = None,
    ) -> str:
        if self._ws is None:
            raise ConnectionError(f"{self.host}: not connected")
        mid = f"sub-{next(self._ids)}"
        self._subscriptions[mid] = callback
        msg: dict[str, Any] = {"id": mid, "type": "subscribe", "uri": f"ssap://{uri}"}
        if payload is not None:
            msg["payload"] = payload
        await self._ws.send(json.dumps(msg))
        return mid

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
