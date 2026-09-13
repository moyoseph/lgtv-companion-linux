from __future__ import annotations

import pytest

from lgtvcompanion import config as config_mod

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
    import asyncio
    async def run():
        d._on_report({"locked": True})
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert d.sessions[0].calls == ["blank"]


def test_config_validates_lock_enums(tmp_path):
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="h")])
    cfg.global_.on_lock = "explode"
    path = tmp_path / "c.json"
    config_mod.save(cfg, path)
    with pytest.raises(ValueError, match="on_lock"):
        config_mod.load(path)
