"""lgtvc-mqtt: MQTT / Home Assistant bridge entry point."""

from __future__ import annotations

import asyncio
import logging
import sys

from .. import config as config_mod
from .. import ipc
from .bridge import Bridge

log = logging.getLogger("lgtvc-mqtt")

RECONNECT_DELAY = 10.0


async def _run(cfg: config_mod.MqttConfig, socket_path: str) -> None:
    bridge = Bridge(cfg, socket_path)
    while True:
        try:
            await bridge.run()
        except Exception as e:
            log.warning("bridge disconnected (%s); retrying in %.0fs",
                        e, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)


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
    if not cfg.global_.mqtt.enabled:
        sys.exit("mqtt.enabled is false in the config — nothing to do")

    socket_path = ipc.default_socket()
    if len(sys.argv) > 2 and sys.argv[1] == "--socket":
        socket_path = sys.argv[2]
    try:
        asyncio.run(_run(cfg.global_.mqtt, socket_path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
