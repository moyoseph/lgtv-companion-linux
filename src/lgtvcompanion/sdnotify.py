"""Minimal systemd NOTIFY_SOCKET writer (Type=notify without a dependency)."""

from __future__ import annotations

import os
import socket


def notify(state: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.send(state.encode())
    except OSError:
        pass


def ready() -> None:
    notify("READY=1")


def stopping() -> None:
    notify("STOPPING=1")


def status(text: str) -> None:
    notify(f"STATUS={text}")
