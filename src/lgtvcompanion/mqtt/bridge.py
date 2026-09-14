"""IPC ↔ MQTT bridge: publishes TV state to MQTT (with Home Assistant
discovery) and forwards MQTT commands to the daemon.

Runs as a separate process (`lgtvc-mqtt`) so the aiomqtt dependency stays out
of the core daemon. It's just another IPC client of the daemon (like the tray),
talking to `/run/lgtv-companion/ipc.sock`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..config import MqttConfig
from ..ipc import IpcClient
from . import discovery as disc

log = logging.getLogger(__name__)

POLL_INTERVAL = 10.0
# Lower than aiomqtt's 60s default so that, on a *hard* PC power-off, the broker
# detects the dropped connection and fires the retained "offline" Will within
# ~1.5x this (Home Assistant sees the PC gone in seconds, not a minute).
KEEPALIVE = 30


def state_topics_for(device: dict) -> dict[str, str]:
    """Map a daemon status device entry to its MQTT state-topic leaf values."""
    ps = device.get("power_state", "Unknown")
    power = "ON" if ps in ("Active", "Screen Off") else "OFF"
    screen = "ON" if ps == "Active" else "OFF"
    hdmi = device.get("source_hdmi_input")
    return {
        "power": power,
        "screen": screen,
        "state": ps,
        "input": str(hdmi) if hdmi else "",
    }


class Bridge:
    def __init__(self, cfg: MqttConfig, socket_path: str):
        self.cfg = cfg
        self.socket_path = socket_path
        self._published: dict[str, str] = {}

    async def run(self, stop: asyncio.Event | None = None) -> None:
        import aiomqtt
        prefix = self.cfg.topic_prefix
        avail = disc.bridge_availability_topic(prefix)
        will = aiomqtt.Will(avail, b"offline", qos=1, retain=True)
        async with aiomqtt.Client(
            hostname=self.cfg.host, port=self.cfg.port,
            username=self.cfg.username or None, password=self.cfg.password or None,
            identifier=self.cfg.client_id, will=will, keepalive=KEEPALIVE,
        ) as client:
            log.info("connected to MQTT %s:%s (keepalive %ss)",
                     self.cfg.host, self.cfg.port, KEEPALIVE)
            await client.publish(avail, b"online", qos=1, retain=True)
            await client.subscribe(disc.command_subscription(prefix))
            ipc = IpcClient(self.socket_path)
            await ipc.connect()
            poller = asyncio.create_task(self._poll_loop(client, ipc))
            try:
                await self._announce(client, ipc)
                await self._serve(client, ipc, stop)
            finally:
                poller.cancel()
                await ipc.close()

    async def _serve(self, client, ipc: IpcClient,
                     stop: asyncio.Event | None) -> None:
        """Dispatch inbound MQTT commands until the broker drops or `stop` is
        set. On a graceful stop we retract availability *now* — the retained
        Will only fires on an *ungraceful* drop (minutes later, at the keepalive
        timeout), so a clean shutdown would otherwise leave a stale "online"."""
        consumer = asyncio.create_task(self._consume(client, ipc))
        stopper = asyncio.create_task(stop.wait()) if stop is not None else None
        tasks = [t for t in (consumer, stopper) if t is not None]
        try:
            done, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
        if stopper is not None and stopper in done:
            with contextlib.suppress(Exception):
                await client.publish(
                    disc.bridge_availability_topic(self.cfg.topic_prefix),
                    b"offline", qos=1, retain=True)
        elif consumer in done and consumer.exception() is not None:
            raise consumer.exception()  # broker dropped -> let _run reconnect

    async def _consume(self, client, ipc: IpcClient) -> None:
        async for message in client.messages:
            await self._handle_command(ipc, str(message.topic), message.payload)

    async def _announce(self, client, ipc: IpcClient) -> None:
        """Publish HA discovery + initial availability for each device."""
        if not self.cfg.discovery:
            return
        status = await self._status(ipc)
        for dev_id, dev in status.get("devices", {}).items():
            for topic, payload in disc.discovery_configs(
                    self.cfg.topic_prefix, self.cfg.discovery_prefix,
                    dev_id, dev.get("name", "")):
                await client.publish(topic, disc.discovery_payload_json(payload),
                                     qos=1, retain=True)
            await client.publish(
                disc.availability_topic(self.cfg.topic_prefix, dev_id),
                b"online", qos=1, retain=True)

    async def _poll_loop(self, client, ipc: IpcClient) -> None:
        while True:
            try:
                await self._publish_state(client, await self._status(ipc))
            except Exception as e:
                log.debug("poll failed: %s", e)
            await asyncio.sleep(POLL_INTERVAL)

    async def _status(self, ipc: IpcClient) -> dict:
        resp = await ipc.request("status", [], [])
        return resp.get("results", {}) if resp.get("ok") else {}

    async def _publish_state(self, client, status: dict) -> None:
        prefix = self.cfg.topic_prefix
        for dev_id, dev in status.get("devices", {}).items():
            leaves = state_topics_for(dev)
            leaves["idle"] = "ON" if status.get("idle_active") else "OFF"
            for leaf, value in leaves.items():
                topic = disc.state_topic(prefix, dev_id, leaf)
                if self._published.get(topic) != value:  # only publish changes
                    self._published[topic] = value
                    await client.publish(topic, value.encode(), retain=True)

    async def _handle_command(self, ipc: IpcClient, topic: str, payload) -> None:
        text = payload.decode(errors="replace") if isinstance(payload, bytes) \
            else str(payload)
        parsed = disc.parse_command(self.cfg.topic_prefix, topic, text)
        if parsed is None:
            log.debug("ignoring MQTT topic %s", topic)
            return
        cmd, args, devices = parsed
        log.info("MQTT %s=%s -> %s %s %s", topic, text, cmd, args, devices)
        try:
            await ipc.request(cmd, args, devices)
        except Exception as e:
            log.error("command %s failed: %s", cmd, e)
