"""Wake-on-LAN: magic packets over broadcast / subnet-directed / unicast."""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import sys

log = logging.getLogger(__name__)

WOL_PORT = 9

_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B


def magic_packet(mac: str) -> bytes:
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(mac_bytes) != 6:
        raise ValueError(f"bad MAC address: {mac!r}")
    return b"\xff" * 6 + mac_bytes * 16


def subnet_broadcast(tv_ip: str) -> str:
    """Directed broadcast address of the interface that routes to tv_ip.

    Linux-only (SIOCGIF* ioctls); falls back to the global broadcast address
    elsewhere or when the interface can't be resolved.
    """
    if not sys.platform.startswith("linux"):
        return "255.255.255.255"
    import fcntl  # Linux-only module

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect((tv_ip, WOL_PORT))
            local_ip = probe.getsockname()[0]
        except OSError:
            return "255.255.255.255"

    for _, ifname in socket.if_nameindex():
        packed = struct.pack("16sH14s", ifname.encode()[:16], socket.AF_INET, b"\0" * 14)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                addr_res = fcntl.ioctl(s.fileno(), _SIOCGIFADDR, packed)
                if socket.inet_ntoa(addr_res[20:24]) != local_ip:
                    continue
                mask_res = fcntl.ioctl(s.fileno(), _SIOCGIFNETMASK, packed)
        except OSError:
            continue
        ip_int = struct.unpack("!I", socket.inet_aton(local_ip))[0]
        mask_int = struct.unpack("!I", mask_res[20:24])[0]
        bcast = (ip_int & mask_int) | (~mask_int & 0xFFFFFFFF)
        return socket.inet_ntoa(struct.pack("!I", bcast))

    return "255.255.255.255"


def send_wol(
    macs: list[str],
    tv_ip: str,
    *,
    method: str = "subnet",
    subnet_override: str | None = None,
) -> None:
    """Send one round of magic packets for every MAC.

    method: "broadcast" (255.255.255.255), "subnet" (directed broadcast +
    unicast to the TV IP — the proven combination), "directed" (unicast only),
    "auto" (all of the above).
    """
    targets: list[str] = []
    if method in ("broadcast", "auto"):
        targets.append("255.255.255.255")
    if method in ("subnet", "auto"):
        targets.append(subnet_override or subnet_broadcast(tv_ip))
        targets.append(tv_ip)
    if method in ("directed",):
        targets.append(tv_ip)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for mac in macs:
            packet = magic_packet(mac)
            for target in dict.fromkeys(targets):
                try:
                    s.sendto(packet, (target, WOL_PORT))
                except OSError as e:
                    log.warning("WoL send to %s failed: %s", target, e)


async def wol_burst(
    macs: list[str],
    tv_ip: str,
    *,
    method: str = "subnet",
    subnet_override: str | None = None,
    interval: float = 1.0,
    stop: asyncio.Event | None = None,
    duration: float | None = None,
) -> None:
    """Re-send magic packets every `interval` seconds until `stop` is set or
    `duration` elapses. WiFi TVs waking from standby routinely miss the first
    packets before the radio re-associates; packets are idempotent."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration if duration else None
    while True:
        send_wol(macs, tv_ip, method=method, subnet_override=subnet_override)
        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval)
        if deadline is not None and loop.time() >= deadline:
            return
