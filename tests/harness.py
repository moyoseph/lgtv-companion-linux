"""Shared test helpers.

Promoted from per-file duplicates: FakeSession + make_daemon (daemon tests),
short_sock (AF_UNIX-safe socket paths), make_session (DeviceSession wired to
the FakeTv fixture).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from lgtvcompanion import config as config_mod
from lgtvcompanion.config import DeviceConfig
from lgtvcompanion.daemon.devices import DeviceSession
from lgtvcompanion.daemon.main import Daemon
from lgtvcompanion.ssap.handshake import KeyStore

from .fake_tv import VALID_KEY


def short_sock() -> str:
    # AF_UNIX paths are capped (~104 bytes on macOS); pytest tmp_path is too deep
    return str(Path(tempfile.mkdtemp(prefix="lgtvc")) / "ipc.sock")


class FakeSession:
    """Daemon-facing DeviceSession stub: records calls, returns the strings the
    daemon expects; knobs for the probed power state, busy, and per-method
    failure."""

    def __init__(self, id_: str, key: str | None = None, *,
                 host: str = "127.0.0.1", probe_state: str = "Active",
                 fail: tuple[str, ...] = ()):
        self.cfg = SimpleNamespace(id=id_, name=id_, unique_display_key=key,
                                   host=host, source_hdmi_input=4)
        self.auto_enabled = True
        self.busy = False
        self.power_state = "Unknown"
        self.client = SimpleNamespace(connected=False)
        self.probe_state = probe_state
        self.calls: list = []
        self._fail = set(fail)

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name in self._fail:
            raise RuntimeError(f"{name} failed")

    async def blank(self):
        self._record("blank")
        return "blanked"

    async def unblank(self):
        self._record("unblank")
        return "on"

    async def power_off(self):
        self._record("off")
        return "off"

    async def power_on(self):
        self._record("on")
        return "Active"

    async def probe_power_state(self, *, timeout: float = 3.0) -> str:
        self._record("probe")
        return self.probe_state

    async def execute(self, cmd, args):
        self.calls.append((cmd.name, list(args)))
        return {"returnValue": True}

    async def disconnect(self):
        self.calls.append("disconnect")


def make_daemon(tmp_path, cfg=None, config_path=None, **global_kw) -> Daemon:
    """Build a Daemon with global-config overrides applied BEFORE construction,
    so the __init__ optional-subsystem branches actually run."""
    if cfg is None:
        cfg = config_mod.Config(
            devices=[DeviceConfig(id="tv1", host="127.0.0.1")])
    for k, v in global_kw.items():
        setattr(cfg.global_, k, v)
    return Daemon(cfg, KeyStore(tmp_path / "keys"), str(tmp_path / "s.sock"),
                  config_path=config_path, state_dir=tmp_path)


def make_session(tv, tmp_path, **overrides) -> DeviceSession:
    """A real DeviceSession whose clients are re-pointed at the FakeTv port."""
    store = KeyStore(tmp_path / "keys")
    store.save("tv1", VALID_KEY)
    cfg = DeviceConfig(
        id="tv1", host="127.0.0.1", ssl=False, source_hdmi_input=4,
        persistent_connection="keep_open", retry_attempts=2,
        backoff_base=0.05, backoff_max=0.1, timeout=3.0,
        **overrides)
    session = DeviceSession(cfg, store)
    session.client.port = tv.port
    orig = session._new_client

    def patched(**kw):
        c = orig(**kw)
        c.port = tv.port
        return c

    session._new_client = patched
    return session
