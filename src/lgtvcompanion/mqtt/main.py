"""lgtvc-mqtt: MQTT / Home Assistant bridge entry point."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from .. import config as config_mod
from .. import ipc
from .bridge import Bridge

log = logging.getLogger("lgtvc-mqtt")

RECONNECT_DELAY = 10.0


async def _run(cfg: config_mod.MqttConfig, socket_path: str) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # e.g. Windows/threads
            loop.add_signal_handler(sig, stop.set)
    bridge = Bridge(cfg, socket_path)
    while not stop.is_set():
        try:
            await bridge.run(stop)  # returns cleanly once `stop` is set
        except Exception as e:
            log.warning("bridge disconnected (%s); retrying in %.0fs",
                        e, RECONNECT_DELAY)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_DELAY)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    try:
        import aiomqtt  # noqa: F401
    except ImportError:
        sys.exit("aiomqtt not installed — pip install 'lgtvcompanion[mqtt]'")

    cfg_path = config_mod.find_config()
    if cfg_path is None:
        sys.exit("no config found")
    cfg = config_mod.load(cfg_path)
    g = cfg.global_
    if not g.mqtt.enabled:
        sys.exit("mqtt.enabled is false in the config — nothing to do")
    if g.offline_mode and not config_mod.is_lan_host(g.mqtt.host):
        sys.exit(f"offline_mode is on and mqtt.host {g.mqtt.host!r} isn't a "
                 "private/LAN address — use a LAN broker or turn offline_mode off")

    socket_path = ipc.default_socket()
    if len(sys.argv) > 2 and sys.argv[1] == "--socket":
        socket_path = sys.argv[2]
    try:
        asyncio.run(_run(cfg.global_.mqtt, socket_path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
