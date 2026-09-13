"""Tests for tray/app.py:ipc_request — the synchronous IPC helper (importable
without PySide6; Qt lives inside main()). Exercised against a real AF_UNIX
server running in a background thread."""

from __future__ import annotations

import json
import socket
import threading

import pytest

from lgtvcompanion.tray.app import ipc_request

from .harness import short_sock


def _listener(path: str) -> socket.socket:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    srv.settimeout(5.0)
    return srv


def test_ipc_request_roundtrip():
    path = short_sock()
    srv = _listener(path)
    received: list[bytes] = []

    def serve() -> None:
        conn, _ = srv.accept()
        with conn:
            conn.settimeout(5.0)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            received.append(buf)
            conn.sendall(b'{"id":1,"ok":true,"results":{}}\n')

    t = threading.Thread(target=serve)
    t.start()
    try:
        resp = ipc_request("poweron", devices=["tv1"], socket_path=path, timeout=5.0)
    finally:
        t.join(timeout=5.0)
        srv.close()
    assert not t.is_alive()
    assert resp == {"id": 1, "ok": True, "results": {}}
    # exact wire frame, byte for byte
    assert received[0] == b'{"id":1,"cmd":"poweron","args":[],"devices":["tv1"]}\n'
    assert json.loads(received[0]) == {
        "id": 1, "cmd": "poweron", "args": [], "devices": ["tv1"]}


def test_ipc_request_connection_closed_without_reply():
    path = short_sock()
    srv = _listener(path)

    def serve() -> None:
        conn, _ = srv.accept()
        with conn:
            conn.settimeout(5.0)
            conn.recv(65536)  # swallow the request, then hang up with no newline

    t = threading.Thread(target=serve)
    t.start()
    try:
        with pytest.raises(ConnectionError, match="daemon closed the connection"):
            ipc_request("poweron", socket_path=path, timeout=5.0)
    finally:
        t.join(timeout=5.0)
        srv.close()
    assert not t.is_alive()
