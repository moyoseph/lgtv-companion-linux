"""MPRIS playback detection — the Linux analog of upstream's "video wake
lock": browsers, mpv, VLC, Jellyfin clients and most players expose
org.mpris.MediaPlayer2 with PlaybackStatus on the session bus."""

from __future__ import annotations

import logging

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

log = logging.getLogger(__name__)

_bus: MessageBus | None = None


async def _get_bus() -> MessageBus:
    global _bus
    if _bus is None or not _bus.connected:
        _bus = await MessageBus(bus_type=BusType.SESSION).connect()
    return _bus


async def mpris_any_playing() -> bool:
    bus = await _get_bus()
    reply = await bus.call(Message(
        destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus", member="ListNames"))
    if reply.message_type != MessageType.METHOD_RETURN:
        return False
    players = [n for n in reply.body[0] if n.startswith("org.mpris.MediaPlayer2.")]
    for name in players:
        try:
            status = await bus.call(Message(
                destination=name, path="/org/mpris/MediaPlayer2",
                interface="org.freedesktop.DBus.Properties", member="Get",
                signature="ss", body=["org.mpris.MediaPlayer2.Player",
                                      "PlaybackStatus"]))
            if (status.message_type == MessageType.METHOD_RETURN
                    and status.body[0].value == "Playing"):
                return True
        except Exception as e:
            log.debug("%s: PlaybackStatus failed (%s)", name, e)
    return False
