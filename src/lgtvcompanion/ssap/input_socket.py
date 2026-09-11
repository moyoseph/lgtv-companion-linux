"""Virtual remote buttons via the pointer-input socket.

getPointerInputSocket returns a one-shot websocket URI; buttons are sent over
it as plain-text frames. Deprecated by LG on recent firmware but still widely
functional.
"""

from __future__ import annotations

import asyncio
import logging
import ssl

import websockets

from .client import SsapClient

log = logging.getLogger(__name__)

URI_POINTER_SOCKET = "com.webos.service.networkinput/getPointerInputSocket"


async def send_buttons(client: SsapClient, buttons: list[str],
                       *, delay: float = 0.15) -> None:
    payload = await client.request(URI_POINTER_SOCKET)
    socket_path = payload.get("socketPath")
    if not socket_path:
        raise ConnectionError(f"{client.host}: no pointer socket ({payload})")
    ctx = None
    if socket_path.startswith("wss:"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect(socket_path, ssl=ctx, open_timeout=5) as ws:
        for name in buttons:
            await ws.send(f"type:button\nname:{name.upper()}\n\n")
            if delay and len(buttons) > 1:
                await asyncio.sleep(delay)
