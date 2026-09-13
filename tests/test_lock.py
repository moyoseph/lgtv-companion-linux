from __future__ import annotations

import asyncio

import pytest

from lgtvcompanion import config as config_mod
from lgtvcompanion.agent.lock import SCREENSAVER, SCREENSAVER_PATHS, ScreenLockWatcher

from .fake_bus import FakeBus, Reply
from .harness import FakeSession, make_daemon


def _daemon(tmp_path, **global_kw):
    d = make_daemon(tmp_path, **global_kw)
    d.sessions = [FakeSession("tv1")]
    return d


async def test_on_lock_blank(tmp_path):
    d = _daemon(tmp_path, on_lock="blank", on_unlock="on")
    await d._on_lock(True)
    assert d.sessions[0].calls == ["blank"]
    await d._on_lock(False)
    assert d.sessions[0].calls == ["blank", "on"]


async def test_on_lock_off(tmp_path):
    d = _daemon(tmp_path, on_lock="off", on_unlock="on")
    await d._on_lock(True)
    assert d.sessions[0].calls == ["off"]


async def test_on_lock_none_does_nothing(tmp_path):
    d = _daemon(tmp_path, on_lock="none", on_unlock="none")
    await d._on_lock(True)
    await d._on_lock(False)
    assert d.sessions[0].calls == []


def test_lock_report_routes_through_on_report(tmp_path):
    d = _daemon(tmp_path, on_lock="blank")
    async def run():
        d._on_report({"locked": True})
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert d.sessions[0].calls == ["blank"]


# -- agent-side ScreenLockWatcher ---------------------------------------------

def _no_change(locked):
    raise AssertionError("on_change must not fire here")


def test_lock_handler_coerces_to_bool():
    events: list[bool] = []
    w = ScreenLockWatcher(None, events.append)
    w._on_active_changed(1)
    w._on_active_changed(0)
    assert events == [True, False]
    assert all(isinstance(e, bool) for e in events)


class _FakeSignalIface:
    """Captures the handler ScreenLockWatcher registers via on_active_changed
    (dbus-fast's generated signal hook)."""

    def __init__(self):
        self.handler = None

    def on_active_changed(self, handler):
        self.handler = handler


class _FakeProxyObject:
    def __init__(self, iface):
        self._iface = iface

    def get_interface(self, name):
        assert name == SCREENSAVER
        return self._iface


class _FakeProxyBus:
    """introspect/get_proxy_object stand-in — tests/fake_bus.FakeBus only
    routes call(), so start() needs its own minimal fake."""

    def __init__(self, ok_paths):
        self.ok_paths = set(ok_paths)
        self.introspected: list[str] = []
        self.iface = _FakeSignalIface()

    async def introspect(self, name, path):
        self.introspected.append(path)
        if path not in self.ok_paths:
            raise RuntimeError(f"no ScreenSaver object at {path}")
        return {"fake": "introspection"}

    def get_proxy_object(self, name, path, introspection):
        return _FakeProxyObject(self.iface)


async def test_lock_watcher_falls_back_to_short_path():
    events: list[bool] = []
    bus = _FakeProxyBus(ok_paths=["/ScreenSaver"])
    w = ScreenLockWatcher(bus, events.append)
    assert await asyncio.wait_for(w.start(), 2.0) is True
    assert bus.introspected == list(SCREENSAVER_PATHS)   # long path tried first
    assert bus.iface.handler is not None
    bus.iface.handler(True)                              # locker fires the signal
    bus.iface.handler(0)
    assert events == [True, False]


async def test_lock_watcher_start_false_when_all_paths_fail():
    events: list[bool] = []
    bus = _FakeProxyBus(ok_paths=[])
    w = ScreenLockWatcher(bus, events.append)
    assert await asyncio.wait_for(w.start(), 2.0) is False
    assert bus.introspected == list(SCREENSAVER_PATHS)
    assert events == []


async def test_lock_watcher_available_variants():
    yes = FakeBus({"NameHasOwner": lambda msg: Reply([True])})
    assert await asyncio.wait_for(
        ScreenLockWatcher(yes, _no_change).available(), 2.0) is True
    assert yes.calls[0].body == [SCREENSAVER]            # asked about ScreenSaver

    no = FakeBus({"NameHasOwner": lambda msg: Reply([False])})
    assert await asyncio.wait_for(
        ScreenLockWatcher(no, _no_change).available(), 2.0) is False

    def boom(msg):
        raise RuntimeError("bus gone")

    err = FakeBus({"NameHasOwner": boom})
    assert await asyncio.wait_for(
        ScreenLockWatcher(err, _no_change).available(), 2.0) is False


def test_config_validates_lock_enums(tmp_path):
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="h")])
    cfg.global_.on_lock = "explode"
    path = tmp_path / "c.json"
    config_mod.save(cfg, path)
    with pytest.raises(ValueError, match="on_lock"):
        config_mod.load(path)
