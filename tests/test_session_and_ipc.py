from __future__ import annotations

import asyncio

from lgtvcompanion import ipc
from lgtvcompanion.daemon.server import IpcServer
from lgtvcompanion.ssap.commands import COMMANDS

from .harness import make_session, short_sock


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
