"""mpris_any_playing against the fake session bus (injected, no monkeypatch)."""

from __future__ import annotations

import asyncio

from dbus_fast import Variant

from lgtvcompanion.agent import mpris as mpris_mod
from lgtvcompanion.agent.mpris import mpris_any_playing

from .fake_bus import FakeBus, Reply, error_reply

VLC = "org.mpris.MediaPlayer2.vlc"
FIREFOX = "org.mpris.MediaPlayer2.firefox.instance123"


def _names(names):
    return Reply([list(names)])


async def test_only_mpris_names_probed():
    bus = FakeBus({
        "ListNames": lambda msg: _names(
            [":1.5", "org.freedesktop.DBus", VLC, "org.kde.KWin", FIREFOX]),
        "Get": lambda msg: Reply([Variant("s", "Paused")]),
    })
    assert await asyncio.wait_for(mpris_any_playing(bus), 2.0) is False
    gets = [m.destination for m in bus.calls if m.member == "Get"]
    assert gets == [VLC, FIREFOX]            # only MPRIS names queried


async def test_playing_variant_unwrapped():
    bus = FakeBus({
        "ListNames": lambda msg: _names([VLC]),
        "Get": lambda msg: Reply([Variant("s", "Playing")]),
    })
    assert await asyncio.wait_for(mpris_any_playing(bus), 2.0) is True


async def test_all_paused_is_false():
    bus = FakeBus({
        "ListNames": lambda msg: _names([VLC, FIREFOX]),
        "Get": lambda msg: Reply([Variant("s", "Paused")]),
    })
    assert await asyncio.wait_for(mpris_any_playing(bus), 2.0) is False


async def test_vanished_player_skipped():
    def get(msg):
        if msg.destination == VLC:
            raise RuntimeError("name vanished mid-probe")
        return Reply([Variant("s", "Playing")])

    bus = FakeBus({"ListNames": lambda msg: _names([VLC, FIREFOX]), "Get": get})
    assert await asyncio.wait_for(mpris_any_playing(bus), 2.0) is True


async def test_listnames_error_is_false():
    bus = FakeBus({"ListNames": lambda msg: error_reply("bus is unhappy")})
    assert await asyncio.wait_for(mpris_any_playing(bus), 2.0) is False
    assert [m.member for m in bus.calls] == ["ListNames"]   # no Get attempted


async def test_cached_bus_used_when_none_passed(monkeypatch):
    bus = FakeBus({"ListNames": lambda msg: _names([])})
    monkeypatch.setattr(mpris_mod, "_bus", bus)             # connected=True
    assert await asyncio.wait_for(mpris_any_playing(), 2.0) is False
    assert [m.member for m in bus.calls] == ["ListNames"]   # cached bus was used
