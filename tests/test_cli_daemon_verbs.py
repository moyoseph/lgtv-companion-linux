"""lgtvc's daemon-facing paths — run_via_daemon / run_daemon_verb / run_events —
against a real IpcServer with a stub dispatcher. Zero mocks."""

from __future__ import annotations

import asyncio

from lgtvcompanion.cli.main import (
    ParseError,
    parse_tokens,
    run_daemon_verb,
    run_events,
    run_via_daemon,
)
from lgtvcompanion.daemon.server import IpcServer

from .harness import short_sock


async def _server(dispatcher):
    server = IpcServer(short_sock(), dispatcher)
    await server.start()
    return server


async def test_run_via_daemon_prints_results(capsys):
    async def dispatcher(cmd, args, devices):
        return {"tv1": {"volume": args[0]}}

    server = await _server(dispatcher)
    try:
        inv = parse_tokens(["-volume", "30"])
        rc = await asyncio.wait_for(
            run_via_daemon(inv, server.socket_path), timeout=5)
    finally:
        await server.stop()
    assert rc == 0
    assert '"volume":30' in capsys.readouterr().out


async def test_run_via_daemon_reports_errors(capsys):
    async def dispatcher(cmd, args, devices):
        raise ValueError("nope")

    server = await _server(dispatcher)
    try:
        rc = await asyncio.wait_for(
            run_via_daemon(parse_tokens(["-poweron"]), server.socket_path),
            timeout=5)
    finally:
        await server.stop()
    assert rc == 1
    assert "nope" in capsys.readouterr().err


async def test_run_daemon_verb_without_daemon(capsys):
    rc = await run_daemon_verb("status", "/nonexistent/ipc.sock")
    assert rc == 1
    assert "daemon not running" in capsys.readouterr().err


async def test_run_daemon_verb_success_and_error(capsys):
    state = {"raise": False}

    async def dispatcher(cmd, args, devices):
        if state["raise"]:
            raise RuntimeError("boom")
        return {"dry_run": False}

    server = await _server(dispatcher)
    try:
        assert await asyncio.wait_for(
            run_daemon_verb("status", server.socket_path), timeout=5) == 0
        assert '"dry_run": false' in capsys.readouterr().out
        state["raise"] = True
        assert await asyncio.wait_for(
            run_daemon_verb("reload", server.socket_path), timeout=5) == 1
        assert "boom" in capsys.readouterr().err
    finally:
        await server.stop()


async def test_run_events_streams_broadcasts(capsys):
    async def dispatcher(cmd, args, devices):
        return {}

    server = await _server(dispatcher)
    task = asyncio.create_task(run_events(server.socket_path))
    try:
        await asyncio.sleep(0.1)                # let the subscription register
        server.broadcast("SYSTEM_SUSPEND")
        for _ in range(200):
            if "SYSTEM_SUSPEND" in capsys.readouterr().out:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("event never printed")
    finally:
        task.cancel()
        try:
            rc = await asyncio.wait_for(task, timeout=2)
            assert rc == 0                      # swallows the cancel, exits 0
        except asyncio.CancelledError:
            pass
        await server.stop()


def test_parse_output_of_shorthand_and_bad_mode():
    inv = parse_tokens(["-of", "-poweron"])
    assert inv.output_mode == "friendly"
    import pytest
    with pytest.raises(ParseError, match="bad mode"):
        parse_tokens(["-output", "bogus", "-poweron"])
