from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest

from lgtvcompanion import config as config_mod
from lgtvcompanion.daemon.vetoes import VetoEngine
from lgtvcompanion.ssap import wol
from lgtvcompanion.ssap.commands import lookup

from .harness import FakeSession, make_daemon


# --- dolbyHdrFilmMaker (#287) ------------------------------------------------

def test_picturemode_has_dolby_hdr_filmmaker():
    pm = lookup("picturemode")
    assert pm.args[0].parse("dolbyHdrFilmMaker") == "dolbyHdrFilmMaker"


# --- WoL explicit targets + interface (cross-subnet, #214/#281) --------------

def test_send_wol_extra_targets_reach_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(2)
    port = listener.getsockname()[1]
    old = wol.WOL_PORT
    wol.WOL_PORT = port
    try:
        # method=directed → only tv_ip; extra_targets adds 127.0.0.1
        wol.send_wol(["aa:bb:cc:dd:ee:ff"], "127.0.0.2", method="directed",
                     extra_targets=["127.0.0.1"])
        data, _ = listener.recvfrom(200)
    finally:
        wol.WOL_PORT = old
        listener.close()
    assert data == wol.magic_packet("aa:bb:cc:dd:ee:ff")


def test_interface_ip_unknown_returns_none():
    assert wol.interface_ip("definitely-not-a-nic-xyz") is None
    assert wol.interface_ip("") is None


# --- veto_mpris foreground_only ----------------------------------------------

def _idle(**kw):
    base = dict(enabled=True, minutes=10, action="blank", mute_speakers=False,
                veto_fullscreen=True, veto_mpris="foreground_only",
                ignored_keys=[], process_list=[])
    base.update(kw)
    return SimpleNamespace(**base)


async def test_veto_mpris_foreground_only():
    v = VetoEngine(_idle(veto_mpris="foreground_only", veto_fullscreen=False))
    v.update_agent_state({"mpris_playing": True, "fullscreen": None})
    assert await v.check() is None                       # bg music → no veto
    v.update_agent_state({"mpris_playing": True, "fullscreen": "mpv"})
    assert "MPRIS" in await v.check()                    # fullscreen video → veto


async def test_veto_mpris_any_still_vetoes_background():
    v = VetoEngine(_idle(veto_mpris="any", veto_fullscreen=False))
    v.update_agent_state({"mpris_playing": True, "fullscreen": None})
    assert "MPRIS" in await v.check()


# --- idle action power_off + topology keep_on_boot (daemon) -------------------

_daemon = make_daemon


async def test_idle_action_power_off(tmp_path):
    d = _daemon(tmp_path)
    d.cfg.global_.idle.action = "power_off"
    d.sessions = [FakeSession("tv1")]
    await d._idle_blank()
    await d._idle_unblank()
    assert d.sessions[0].calls == ["off", "on"]


async def test_idle_action_blank(tmp_path):
    d = _daemon(tmp_path)
    d.cfg.global_.idle.action = "blank"
    d.sessions = [FakeSession("tv1")]
    await d._idle_blank()
    await d._idle_unblank()
    assert d.sessions[0].calls == ["blank", "unblank"]


async def test_topology_keep_on_boot_persist_and_restore(tmp_path):
    d = _daemon(tmp_path)
    d.cfg.global_.topology.enabled = True
    d.cfg.global_.topology.keep_on_boot = True
    d.sessions = [FakeSession("tv1", key="GSM-1"), FakeSession("tv2", key="GSM-2")]
    # a topology change persists the present set
    await d._on_topology_change({"GSM-1"})
    assert json.loads(d._topology_state_path().read_text()) == ["GSM-1"]
    assert d.sessions[0].calls[-1] == "on"      # tv1 present
    assert d.sessions[1].calls[-1] == "off"     # tv2 absent
    # a fresh daemon restores that set at boot
    d2 = _daemon(tmp_path)
    d2.cfg.global_.topology.enabled = True
    d2.cfg.global_.topology.keep_on_boot = True
    d2.sessions = [FakeSession("tv1", key="GSM-1"), FakeSession("tv2", key="GSM-2")]
    await d2._restore_topology()
    assert d2.sessions[0].calls == ["on"]
    assert d2.sessions[1].calls == ["off"]


def test_config_validates_new_enums(tmp_path):
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(id="tv1", host="h")])
    cfg.global_.idle.action = "explode"
    p = tmp_path / "c.json"
    config_mod.save(cfg, p)
    with pytest.raises(ValueError, match="idle.action"):
        config_mod.load(p)


def test_config_roundtrips_new_fields(tmp_path):
    cfg = config_mod.Config(devices=[config_mod.DeviceConfig(
        id="tv1", host="h", interface="eth0", wol_targets=["10.0.1.255"])])
    cfg.global_.daemon_power_events = True
    cfg.global_.idle.action = "power_off"
    p = tmp_path / "c.json"
    config_mod.save(cfg, p)
    loaded = config_mod.load(p)
    assert loaded.global_.daemon_power_events is True
    assert loaded.global_.idle.action == "power_off"
    assert loaded.devices[0].interface == "eth0"
    assert loaded.devices[0].wol_targets == ["10.0.1.255"]
