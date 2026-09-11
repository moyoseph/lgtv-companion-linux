"""Wait for the network to come back after resume.

On resume the daemon races NetworkManager: WoL/API calls fail with
"Network is unreachable" until the link is restored (the proven failure mode
of the legacy setup). A UDP connect() to the TV fails synchronously with
ENETUNREACH while there is no route, which makes it a cheap readiness probe
that needs no privileges and sends no packets.
"""

from __future__ import annotations

import asyncio
import logging
import socket

log = logging.getLogger(__name__)


def _has_route(host: str) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
        return True
    except OSError:
        return False


async def wait_for_network(host: str, *, timeout: float = 30.0,
                           interval: float = 1.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if _has_route(host):
            return True
        if loop.time() >= deadline:
            log.warning("network to %s not ready after %.0fs — proceeding anyway",
                        host, timeout)
            return False
        await asyncio.sleep(interval)
