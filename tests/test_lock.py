from __future__ import annotations

import pytest

from lgtvcompanion import config as config_mod
from lgtvcompanion.daemon.main import Daemon
from lgtvcompanion.ssap.handshake import KeyStore


class FakeSession:
    def __init__(self, id_):
        self.cfg = type("C", (), {"id": id_, "name": id_})()
        self.auto_enabled = True
        self.calls = []

    async def blank(self):
        self.calls.append("blank")

    async def power_off(self):
        self.calls.append("off")

    async def power_on(self):
        self.calls.append("on")


def _daemon(tmp_path, **global_kw):
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="10.0.0.6")])
    for k, v in global_kw.items():
        setattr(cfg.global_, k, v)
    d = Daemon(cfg, KeyStore(tmp_path / "keys"), str(tmp_path / "ipc.sock"))
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
