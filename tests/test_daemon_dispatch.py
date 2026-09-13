"""Daemon.dispatch / _meta / reload / on_boot / _power_all — driven through
FakeSessions and real DeviceSessions against the FakeTv."""

from __future__ import annotations

import asyncio

import pytest

from lgtvcompanion import config as config_mod
from lgtvcompanion.config import DeviceConfig

from .harness import FakeSession, make_daemon, make_session


def _remote_stream_cfg():
    cfg = config_mod.Config(devices=[DeviceConfig(id="tv1", host="127.0.0.1")])
    cfg.global_.remote_stream.enabled = True
    cfg.global_.remote_stream.on_connect = "blank"
    cfg.global_.remote_stream.on_disconnect = "restore"
    return cfg


# -- dispatch -------------------------------------------------------------------


async def test_dispatch_unknown_command_raises(tmp_path):
    d = make_daemon(tmp_path)
    with pytest.raises(ValueError, match="unknown command"):
        await d.dispatch("frobnicate", [], [])


async def test_dispatch_status_and_reload_verbs(tmp_path):
    d = make_daemon(tmp_path)
    d.sessions = [FakeSession("tv1")]
    st = await d.dispatch("status", [], [])
    assert st["devices"]["tv1"]["power_state"] == "Unknown"
    # no config_path -> reload reports the problem instead of raising
    assert (await d.dispatch("reload", [], []))["reloaded"] is False


async def test_dispatch_regular_command_via_fake_tv(tv, tmp_path):
    d = make_daemon(tmp_path)
    d.sessions = [make_session(tv, tmp_path)]
    result = await d.dispatch("volume", [25], [])
    assert result["tv1"]["volume"] == 25
    await d.sessions[0].disconnect()


async def test_dispatch_per_device_error_is_reported_not_raised(tv, tmp_path):
    tv.error_uris["audio/setVolume"] = {"errorCode": "500"}
    d = make_daemon(tmp_path)
    d.sessions = [make_session(tv, tmp_path)]
    result = await d.dispatch("volume", [25], [])
    assert "error" in result["tv1"]
    await d.sessions[0].disconnect()


async def test_dispatch_selector_matches_and_rejects(tmp_path):
    d = make_daemon(tmp_path)
    d.sessions = [FakeSession("tv1"), FakeSession("tv2")]
    result = await d.dispatch("autodisable", [], ["TV2"])   # case-insensitive
    assert result == {"tv2": "auto-disabled"}
    assert d.sessions[1].auto_enabled is False
    with pytest.raises(ValueError, match="unknown device"):
        await d.dispatch("poweron", [], ["bogus"])


# -- _meta actions ----------------------------------------------------------------


async def test_meta_auto_enable_disable_roundtrip(tmp_path):
    d = make_daemon(tmp_path)
    d.sessions = [FakeSession("tv1")]
    assert await d.dispatch("autodisable", [], []) == {"tv1": "auto-disabled"}
    assert d.sessions[0].auto_enabled is False
    assert await d.dispatch("autoenable", [], []) == {"tv1": "auto-enabled"}
    assert d.sessions[0].auto_enabled is True


async def test_meta_force_idle_without_engine_blanks_directly(tmp_path):
    d = make_daemon(tmp_path)
    d.sessions = [FakeSession("tv1")]
    assert await d.dispatch("idle", [], []) == {"tv1": "blanked"}
    assert await d.dispatch("unidle", [], []) == {"tv1": "on"}
    assert d.sessions[0].calls == ["blank", "unblank"]


async def test_meta_force_idle_with_engine(tmp_path):
    cfg = config_mod.Config(devices=[DeviceConfig(id="tv1", host="127.0.0.1")])
    cfg.global_.idle.enabled = True
    d = make_daemon(tmp_path, cfg=cfg)
    assert d.idle_engine is not None
    d.sessions = [FakeSession("tv1")]
    try:
        assert await d.dispatch("idle", [], []) == {"idle": "forced"}
        assert d.sessions[0].calls == ["blank"]
        assert await d.dispatch("unidle", [], []) == {"idle": "released"}
        assert d.sessions[0].calls == ["blank", "unblank"]
    finally:
        d.idle_engine.stop()


async def test_meta_streaming_hooks(tmp_path):
    d = make_daemon(tmp_path, cfg=_remote_stream_cfg())
    d.sessions = [FakeSession("tv1")]
    await d.dispatch("streaming_connect", [], [])
    assert d.streams.streaming is True
    assert d.sessions[0].calls == ["blank"]
    await d.dispatch("streaming_disconnect", [], [])
    assert d.streams.streaming is False
    assert d.sessions[0].calls == ["blank", "on"]


async def test_meta_clearlog_and_unknown_action(tmp_path):
    d = make_daemon(tmp_path)
    result = await d.dispatch("clearlog", [], [])
    assert "journald" in result["log"]
    with pytest.raises(ValueError, match="unhandled meta action"):
        await d._meta("bogus", [])


# -- reload error paths -------------------------------------------------------------


async def test_reload_with_garbage_config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json")
    d = make_daemon(tmp_path, config_path=path)
    result = d.reload()
    assert result["reloaded"] is False and result["error"]


async def test_reload_disables_idle_engine(tmp_path):
    # on-disk config has idle off; the running daemon has it on
    path = tmp_path / "config.json"
    disk = config_mod.Config(devices=[DeviceConfig(id="tv1", host="127.0.0.1")])
    config_mod.save(disk, path)
    cfg = config_mod.Config(devices=[DeviceConfig(id="tv1", host="127.0.0.1")])
    cfg.global_.idle.enabled = True
    d = make_daemon(tmp_path, cfg=cfg, config_path=path)
    assert d.idle_engine is not None
    assert d.reload()["reloaded"] is True
    assert d.idle_engine is None


# -- on_boot / _power_all / on_resume ------------------------------------------------


async def test_on_boot_powers_on(tmp_path):
    d = make_daemon(tmp_path)
    d.sessions = [FakeSession("tv1")]
    await d.on_boot()
    assert d.sessions[0].calls == ["on"]


async def test_on_boot_disabled(tmp_path):
    d = make_daemon(tmp_path, power_on_at_boot=False)
    d.sessions = [FakeSession("tv1")]
    await d.on_boot()
    assert d.sessions[0].calls == []


async def test_on_boot_restores_topology_instead(tmp_path):
    cfg = config_mod.Config(devices=[DeviceConfig(id="tv1", host="127.0.0.1")])
    cfg.global_.topology.enabled = True
    cfg.global_.topology.keep_on_boot = True
    d = make_daemon(tmp_path, cfg=cfg)
    d.sessions = [FakeSession("tv1", key="GSM-1")]
    d._save_topology({"GSM-1"})
    await d.on_boot()
    assert d.sessions[0].calls == ["on"]      # restored, not plain power-on


async def test_power_all_collects_failures(tmp_path, caplog):
    import logging
    d = make_daemon(tmp_path)
    d.sessions = [FakeSession("tv1"), FakeSession("tv2", fail=("off",))]
    with caplog.at_level(logging.ERROR, logger="lgtvc-daemon"):
        await d._power_all("off")
    assert d.sessions[0].calls == ["off"]
    assert d.sessions[1].calls == ["off"]     # attempted despite failure
    assert "power off failed" in caplog.text


async def test_power_all_no_managed_sessions(tmp_path):
    d = make_daemon(tmp_path)
    d.sessions = [FakeSession("tv1")]
    d.sessions[0].auto_enabled = False
    await d._power_all("on")                   # early return, nothing to do
    assert d.sessions[0].calls == []


async def test_on_resume_waits_then_powers_on(tmp_path):
    d = make_daemon(tmp_path)
    d.sessions = [FakeSession("tv1")]          # host 127.0.0.1 -> instant
    await asyncio.wait_for(d.on_resume(), timeout=5)
    assert d.sessions[0].calls == ["on"]


async def test_noop_callback(tmp_path):
    await make_daemon(tmp_path)._noop()
