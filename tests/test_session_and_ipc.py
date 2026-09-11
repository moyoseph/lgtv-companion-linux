from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from lgtvcompanion import ipc
from lgtvcompanion.config import DeviceConfig
from lgtvcompanion.daemon.devices import DeviceSession
from lgtvcompanion.daemon.server import IpcServer
from lgtvcompanion.ssap.commands import COMMANDS
from lgtvcompanion.ssap.handshake import KeyStore

from .fake_tv import VALID_KEY


def short_sock() -> str:
    # AF_UNIX paths are capped (~104 bytes on macOS); pytest tmp_path is too deep
    return str(Path(tempfile.mkdtemp(prefix="lgtvc")) / "ipc.sock")


def make_session(tv, tmp_path, **overrides) -> DeviceSession:
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

    def patched():
        c = orig()
        c.port = tv.port
        return c

    session._new_client = patched
    return session


async def test_session_executes_request_command(tv, tmp_path):
    s = make_session(tv, tmp_path)
    result = await s.execute(COMMANDS["volume"], [42])
    assert result["volume"] == 42
    await s.disconnect()


async def test_session_luna_command(tv, tmp_path):
    s = make_session(tv, tmp_path)
    result = await s.execute(COMMANDS["backlight"], [70])
    assert result == {"returnValue": True}
    assert tv.luna_calls
    await s.disconnect()


async def test_session_power_off_guard(tv, tmp_path):
    tv.foreground_app = "netflix"
    s = make_session(tv, tmp_path)
    assert await s.power_off() == "refused-wrong-input"
    assert tv.power_state == "Active"


async def test_session_dry_run_sends_nothing(tv, tmp_path):
    s = make_session(tv, tmp_path)
    s.dry_run = True
    assert await s.power_off() == "dry-run"
    assert await s.power_on() == "dry-run"
    assert await s.execute(COMMANDS["volume"], [10]) == "dry-run"
    assert tv.requests == []


async def test_ipc_server_roundtrip_and_events(tmp_path):
    calls = []

    async def dispatcher(cmd, args, devices):
        calls.append((cmd, args, devices))
        return {"tv1": "ok"}

    sock = short_sock()
    server = IpcServer(sock, dispatcher)
    await server.start()

    client = ipc.IpcClient(sock)
    await client.connect()
    resp = await client.request("poweron", [], ["tv1"])
    assert resp["ok"] is True
    assert resp["results"] == {"tv1": "ok"}
    assert calls == [("poweron", [], ["tv1"])]

    # event subscription on a second connection
    sub = ipc.IpcClient(sock)
    await sub.connect()
    events = []

    async def collect():
        async for e in sub.subscribe():
            events.append(e)
            break

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.1)
    server.broadcast("SYSTEM_SUSPEND")
    await asyncio.wait_for(task, timeout=2)
    assert events == ["SYSTEM_SUSPEND"]

    await client.close()
    await sub.close()
    await server.stop()


async def test_ipc_dispatch_error_is_reported(tmp_path):
    async def dispatcher(cmd, args, devices):
        raise ValueError("nope")

    sock = short_sock()
    server = IpcServer(sock, dispatcher)
    await server.start()
    client = ipc.IpcClient(sock)
    await client.connect()
    resp = await client.request("poweron", [], [])
    assert resp["ok"] is False
    assert "nope" in resp["error"]
    await client.close()
    await server.stop()
