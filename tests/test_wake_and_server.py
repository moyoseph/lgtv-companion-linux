"""WakeOnInput debounce/gating and the remaining IpcServer branches
(unknown-event broadcast, agent report frames, malformed frames,
subscriber cleanup on stop)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from lgtvcompanion import ipc
from lgtvcompanion.daemon.inputdev import EV_KEY, KEY_PRESS
from lgtvcompanion.daemon.server import IpcServer
from lgtvcompanion.daemon.wake import WakeOnInput

from .harness import short_sock

# -- WakeOnInput --------------------------------------------------------------


async def test_wake_fires_when_tv_unreachable():
    woke = asyncio.Event()

    async def unreachable():
        return False

    async def wake():
        woke.set()

    w = WakeOnInput(is_tv_reachable=unreachable, wake=wake, cooldown_s=0.0)
    w.notify_input("kbd")
    await asyncio.wait_for(woke.wait(), timeout=1)


async def test_no_wake_when_tv_reachable():
    woke: list = []

    async def reachable():
        return True

    async def wake():
        woke.append(1)

    w = WakeOnInput(is_tv_reachable=reachable, wake=wake, cooldown_s=0.0)
    w.notify_input()
    await asyncio.sleep(0.05)
    assert woke == []


async def test_cooldown_suppresses_second_press():
    checks: list = []

    async def unreachable():
        checks.append(1)
        return False

    async def wake():
        pass

    w = WakeOnInput(is_tv_reachable=unreachable, wake=wake, cooldown_s=100.0)
    # Prime the last-attempt clock so the first press is allowed regardless of
    # the machine's uptime (monotonic() can be < cooldown on a fresh CI runner).
    w._last_attempt = time.monotonic() - 200.0
    w.notify_input()
    w.notify_input()   # within cooldown → dropped
    await asyncio.sleep(0.05)
    assert len(checks) == 1


def test_on_event_only_reacts_to_key_press():
    seen: list = []
    w = WakeOnInput(is_tv_reachable=None, wake=None, cooldown_s=0.0)
    w.notify_input = lambda src="agent": seen.append(src)   # type: ignore[method-assign]
    w._on_event("/dev/input/event0", 0, 30, 1)              # not EV_KEY
    w._on_event("/dev/input/event0", EV_KEY, 30, 0)         # key release
    assert seen == []
    w._on_event("/dev/input/event0", EV_KEY, 30, KEY_PRESS)
    assert seen == ["/dev/input/event0"]


# -- IpcServer ----------------------------------------------------------------


async def _dispatcher(cmd, args, devices):
    return {}


async def test_broadcast_unknown_event_is_ignored():
    server = IpcServer(short_sock(), _dispatcher)
    await server.start()
    try:
        server.broadcast("NOT_A_REAL_EVENT")   # logged + skipped, no raise
    finally:
        await server.stop()


async def test_report_frame_invokes_on_report():
    got: list = []
    server = IpcServer(short_sock(), _dispatcher, on_report=lambda r: got.append(r))
    await server.start()
    client = ipc.IpcClient(server.socket_path)
    await client.connect()
    await client.send_report({"mpris_playing": True})
    await asyncio.sleep(0.05)
    assert got == [{"mpris_playing": True}]
    await client.close()
    await server.stop()


async def test_malformed_frame_gets_error_response():
    server = IpcServer(short_sock(), _dispatcher)
    await server.start()
    reader, writer = await asyncio.open_unix_connection(server.socket_path)
    writer.write(b"this is not json\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=1)
    resp = json.loads(line)
    assert resp["ok"] is False
    writer.close()
    await writer.wait_closed()
    await server.stop()


# -- ipc.py edge branches -------------------------------------------------------

async def test_read_frame_connection_error_returns_none():
    from lgtvcompanion import ipc

    class BadReader:
        async def readline(self):
            raise ConnectionResetError("boom")

    assert await ipc.read_frame(BadReader()) is None


async def test_read_frame_rejects_oversized_line():
    from lgtvcompanion import ipc
    reader = asyncio.StreamReader(limit=ipc.MAX_LINE * 2)
    reader.feed_data(b"x" * (ipc.MAX_LINE + 5) + b"\n")
    reader.feed_eof()
    with pytest.raises(ValueError, match="too large"):
        await ipc.read_frame(reader)


async def test_ipc_client_request_raises_when_server_closes():
    from lgtvcompanion import ipc

    async def handler(reader, writer):
        writer.close()                          # drop immediately, no response

    server = await asyncio.start_unix_server(handler, path=short_sock())
    try:
        client = ipc.IpcClient(server.sockets[0].getsockname())
        await client.connect()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(client.request("status"), timeout=2)
        await client.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_ipc_subscribe_stops_when_server_closes():
    from lgtvcompanion import ipc

    async def handler(reader, writer):
        await reader.readline()                 # consume the subscribe frame
        writer.close()

    server = await asyncio.start_unix_server(handler, path=short_sock())
    try:
        client = ipc.IpcClient(server.sockets[0].getsockname())
        await client.connect()
        agen = client.subscribe()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(agen.__anext__(), timeout=2)
        await client.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_ipc_close_swallows_wait_closed_error():
    from lgtvcompanion import ipc
    server = await asyncio.start_unix_server(lambda r, w: None, path=short_sock())
    try:
        client = ipc.IpcClient(server.sockets[0].getsockname())
        await client.connect()

        async def boom():
            raise RuntimeError("wait_closed failed")

        client._writer.wait_closed = boom
        await client.close()                    # must not raise (114-115)
    finally:
        server.close()
        await server.wait_closed()


# -- WakeOnInput wake() exception -----------------------------------------------

async def test_wake_swallows_callback_exception():
    async def unreachable():
        return False

    async def boom():
        raise RuntimeError("wake failed")

    fired = asyncio.Event()

    async def unreachable_then_flag():
        fired.set()
        return False

    w = WakeOnInput(is_tv_reachable=unreachable_then_flag, wake=boom, cooldown_s=0.0)
    w.notify_input("kbd")
    await asyncio.wait_for(fired.wait(), timeout=2)
    await asyncio.sleep(0.02)                   # let _maybe_wake hit the except (63-64)
    assert not w._busy                          # finally reset it
