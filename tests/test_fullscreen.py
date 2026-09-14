from __future__ import annotations

import asyncio

from lgtvcompanion.agent.fullscreen import (
    _KWIN_SCRIPT,
    AGENT_BUS_NAME,
    AGENT_OBJ_PATH,
    FullscreenProbe,
)

from .fake_bus import FakeBus, Reply, error_reply


def test_kwin_script_has_no_unfilled_placeholders():
    for marker in ("@NAME@", "@PATH@", "@IFACE@"):
        assert marker not in _KWIN_SCRIPT
    assert "org.lgtvcompanion.Agent" in _KWIN_SCRIPT
    assert "callDBus" in _KWIN_SCRIPT
    # both KWin 5 and 6 active-window accessors present
    assert "activeWindow" in _KWIN_SCRIPT and "activeClient" in _KWIN_SCRIPT


def test_report_caches_fullscreen_state():
    probe = FullscreenProbe(bus=None)  # bus unused for the callback path
    probe._on_report(True, "steam_app_620")
    assert probe._latest == (True, "steam_app_620")
    assert probe._event.is_set()


def test_report_normalizes_empty_app():
    probe = FullscreenProbe(bus=None)
    probe._on_report(False, "")
    assert probe._latest == (False, "")


# -- probe() against a fake KWin ----------------------------------------------

def _kwin_bus(fs: bool, app: str, script_id: int = 7) -> FakeBus:
    """FakeBus acting like KWin scripting: `run` re-enters the exported
    _Reporter the way KWin's script calls back over the bus."""
    bus = FakeBus()

    def run(msg):
        bus.exported[AGENT_OBJ_PATH].Report(fs, app)
        return Reply()

    bus.handlers.update({
        "loadScript": lambda msg: Reply([script_id]),
        "run": run,
        "unloadScript": lambda msg: Reply(),
    })
    return bus


async def test_probe_returns_fullscreen_app_class():
    bus = _kwin_bus(True, "vlc")
    probe = FullscreenProbe(bus)
    assert await asyncio.wait_for(probe.probe(), 2.0) == "vlc"
    assert bus.names == [AGENT_BUS_NAME]
    assert [m.member for m in bus.calls] == ["loadScript", "run", "unloadScript"]


async def test_probe_not_fullscreen_is_none():
    bus = _kwin_bus(False, "vlc")
    assert await asyncio.wait_for(FullscreenProbe(bus).probe(), 2.0) is None


async def test_probe_empty_class_falls_back_to_fullscreen():
    bus = _kwin_bus(True, "")
    assert await asyncio.wait_for(FullscreenProbe(bus).probe(), 2.0) == "fullscreen"


async def test_probe_load_error_returns_none_without_unload():
    bus = FakeBus({"loadScript": lambda msg: error_reply("kwin says no")})
    assert await asyncio.wait_for(FullscreenProbe(bus).probe(), 2.0) is None
    assert [m.member for m in bus.calls] == ["loadScript"]   # no unloadScript


async def test_probe_timeout_still_unloads_script():
    # loadScript/run succeed but the script never calls Report back
    bus = FakeBus({"loadScript": lambda msg: Reply([3])})
    probe = FullscreenProbe(bus)
    assert await asyncio.wait_for(probe.probe(timeout=0.05), 2.0) is None
    assert "unloadScript" in [m.member for m in bus.calls]   # finally path ran


async def test_second_probe_exports_only_once():
    bus = _kwin_bus(True, "vlc")
    probe = FullscreenProbe(bus)
    assert await asyncio.wait_for(probe.probe(), 2.0) == "vlc"
    assert await asyncio.wait_for(probe.probe(), 2.0) == "vlc"
    assert bus.names == [AGENT_BUS_NAME]                 # request_name once
    assert list(bus.exported) == [AGENT_OBJ_PATH]        # one exported object


async def test_run_script_falls_back_to_kwin6_interface():
    bus = FakeBus()
    seen: list[str] = []

    def run(msg):
        seen.append(msg.interface)
        if msg.interface == "org.kde.kwin.Script":       # KWin 5 shape rejected
            return error_reply("unknown interface")
        bus.exported[AGENT_OBJ_PATH].Report(True, "game")
        return Reply()

    bus.handlers.update({
        "loadScript": lambda msg: Reply([3]),
        "run": run,
    })
    assert await asyncio.wait_for(FullscreenProbe(bus).probe(), 2.0) == "game"
    assert seen == ["org.kde.kwin.Script", "org.kde.kwin.Scripting"]
    run_paths = {m.path for m in bus.calls if m.member == "run"}
    assert run_paths == {"/Scripting/Script3"}


async def test_available_variants():
    yes = FakeBus({"NameHasOwner": lambda msg: Reply([True])})
    assert await asyncio.wait_for(FullscreenProbe(yes).available(), 2.0) is True

    no = FakeBus({"NameHasOwner": lambda msg: Reply([False])})
    assert await asyncio.wait_for(FullscreenProbe(no).available(), 2.0) is False

    empty = FakeBus({"NameHasOwner": lambda msg: Reply([])})
    assert await asyncio.wait_for(FullscreenProbe(empty).available(), 2.0) is False

    def boom(msg):
        raise RuntimeError("bus gone")

    err = FakeBus({"NameHasOwner": boom})
    assert await asyncio.wait_for(FullscreenProbe(err).available(), 2.0) is False


async def test_probe_swallows_unload_script_error():
    # the finally-path _unload_script must swallow a failing unloadScript (152-153)
    bus = FakeBus()

    def run(msg):
        bus.exported[AGENT_OBJ_PATH].Report(True, "vlc")
        return Reply()

    def unload_boom(msg):
        raise RuntimeError("kwin gone")

    bus.handlers.update({
        "loadScript": lambda msg: Reply([3]),
        "run": run,
        "unloadScript": unload_boom,
    })
    probe = FullscreenProbe(bus)
    result = await probe.probe(timeout=1.0)
    assert result == "vlc"                      # returned despite the unload failure
