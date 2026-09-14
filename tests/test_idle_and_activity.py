from __future__ import annotations

import asyncio
from types import SimpleNamespace


from lgtvcompanion.activity import (
    EV_ABS,
    EV_KEY,
    EV_REL,
    ActivityFilter,
    parse_ignored_keys,
)
from lgtvcompanion.daemon.idle import IdleEngine
from lgtvcompanion.daemon.vetoes import VetoEngine


def idle_cfg(**kw):
    base = dict(enabled=True, minutes=10, mute_speakers=False,
                veto_fullscreen=True, veto_mpris="any",
                ignored_keys=[], process_list=[])
    base.update(kw)
    return SimpleNamespace(**base)


# --- ActivityFilter ---------------------------------------------------------

def test_key_press_is_activity():
    f = ActivityFilter()
    assert f.is_activity("kbd", EV_KEY, 30, 1) is True
    assert f.is_activity("kbd", EV_KEY, 30, 0) is False  # release


def test_ignored_keys_do_not_count():
    f = ActivityFilter(parse_ignored_keys(["KEY_VOLUMEUP", 114]))
    assert f.is_activity("kbd", EV_KEY, 115, 1) is False
    assert f.is_activity("kbd", EV_KEY, 114, 1) is False
    assert f.is_activity("kbd", EV_KEY, 30, 1) is True


def test_mouse_debounce_needs_three_samples():
    f = ActivityFilter()
    assert f.is_activity("mouse", EV_REL, 0, 5) is False
    assert f.is_activity("mouse", EV_REL, 0, 5) is False
    assert f.is_activity("mouse", EV_REL, 0, 5) is True


def test_abs_deadband_filters_stick_jitter():
    f = ActivityFilter()
    f.is_activity("pad", EV_ABS, 0, 1000)          # baseline (ABS_X, a stick)
    assert f.is_activity("pad", EV_ABS, 0, 1500) is False   # jitter
    assert f.is_activity("pad", EV_ABS, 0, 9000) is True    # real motion


def test_dpad_hat_counts_as_activity():
    # D-pad/hat axes are discrete (-1/0/1); the stick deadband would drop them.
    f = ActivityFilter()
    assert f.is_activity("pad", EV_ABS, 0x10, -1) is True   # ABS_HAT0X pressed
    assert f.is_activity("pad", EV_ABS, 0x11, 1) is True    # ABS_HAT0Y pressed
    assert f.is_activity("pad", EV_ABS, 0x10, 0) is False   # released/centered


def test_trigger_pull_counts_as_activity():
    # Triggers rest at 0 with a 0..255 range — always below the stick deadband.
    f = ActivityFilter()
    assert f.is_activity("pad", EV_ABS, 0x05, 255) is True  # ABS_RZ full pull
    assert f.is_activity("pad", EV_ABS, 0x02, 200) is True  # ABS_Z pull
    assert f.is_activity("pad", EV_ABS, 0x05, 10) is False  # rest/noise


# --- VetoEngine -------------------------------------------------------------

async def test_veto_mpris_and_fullscreen():
    v = VetoEngine(idle_cfg())
    assert await v.check() is None
    v.update_agent_state({"mpris_playing": True})
    assert "MPRIS" in await v.check()
    v.update_agent_state({"mpris_playing": False, "fullscreen": "mpv"})
    assert "fullscreen" in await v.check()


async def test_veto_mpris_off_setting():
    v = VetoEngine(idle_cfg(veto_mpris="off", veto_fullscreen=False))
    v.update_agent_state({"mpris_playing": True, "fullscreen": "mpv"})
    assert await v.check() is None


async def test_veto_process_list(monkeypatch):
    v = VetoEngine(idle_cfg(process_list=[{"match": "steam*", "flags": ["running"]}]))
    monkeypatch.setattr("lgtvcompanion.daemon.vetoes.running_processes",
                        lambda: {"bash", "steamwebhelper"})
    assert "steamwebhelper" in await v.check()
    monkeypatch.setattr("lgtvcompanion.daemon.vetoes.running_processes",
                        lambda: {"bash"})
    assert await v.check() is None


async def test_veto_logind_inhibitor():
    class FakeManager:
        async def call_list_inhibitors(self):
            return [("idle", "firefox", "video playing", "block", 1000, 4242)]

    v = VetoEngine(idle_cfg(), logind_manager=None)
    v.logind = FakeManager()
    assert "firefox" in await v.check()


# --- IdleEngine -------------------------------------------------------------

async def test_idle_engine_blank_and_wake():
    events = []

    async def on_idle():
        events.append("idle")

    async def on_busy():
        events.append("busy")

    v = VetoEngine(idle_cfg())
    engine = IdleEngine(minutes=1, vetoes=v, on_idle=on_idle, on_busy=on_busy)
    engine.timeout_s = 0.2  # shrink for the test
    engine.start()
    await asyncio.sleep(0.5)
    assert engine.is_idle and events == ["idle"]
    engine.notify_activity()
    await asyncio.sleep(0.1)
    assert not engine.is_idle and events == ["idle", "busy"]
    engine.stop()


async def test_idle_engine_respects_veto():
    events = []

    async def on_idle():
        events.append("idle")

    async def on_busy():
        events.append("busy")

    v = VetoEngine(idle_cfg())
    v.update_agent_state({"mpris_playing": True})
    engine = IdleEngine(minutes=1, vetoes=v, on_idle=on_idle, on_busy=on_busy)
    engine.timeout_s = 0.1
    engine.start()
    await asyncio.sleep(0.4)
    assert not engine.is_idle and events == []
    engine.stop()


async def test_forced_idle_ignores_activity():
    events = []

    async def on_idle():
        events.append("idle")

    async def on_busy():
        events.append("busy")

    engine = IdleEngine(minutes=1, vetoes=VetoEngine(idle_cfg()),
                        on_idle=on_idle, on_busy=on_busy)
    await engine.force_idle()
    assert engine.is_idle
    engine.notify_activity()          # must NOT unblank while forced
    await asyncio.sleep(0.05)
    assert engine.is_idle
    await engine.force_unidle()
    assert not engine.is_idle and events == ["idle", "busy"]
