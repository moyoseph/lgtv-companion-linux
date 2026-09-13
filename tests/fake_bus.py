"""A minimal fake dbus-fast MessageBus.

The code under test only ever reads `.message_type` and `.body` off replies,
so replies are plain objects — no real Message construction needed. Handlers
are keyed by the outgoing message's `member`.
"""

from __future__ import annotations

import asyncio

from dbus_fast import MessageType


class Reply:
    def __init__(self, body=None, message_type=MessageType.METHOD_RETURN):
        self.body = body if body is not None else []
        self.message_type = message_type


def error_reply(text: str = "error") -> Reply:
    return Reply(body=[text], message_type=MessageType.ERROR)


class FakeBus:
    """Routes `await bus.call(msg)` through a member-keyed handler dict.

    Handlers are `msg -> Reply` (sync or async). A missing member returns an
    empty METHOD_RETURN. `export` captures the exported object so tests can
    re-enter its methods (e.g. KWin's Report callback).
    """

    def __init__(self, handlers: dict | None = None):
        self.handlers = dict(handlers or {})
        self.calls: list = []        # every Message passed to call()
        self.exported: dict = {}     # path -> object
        self.names: list = []        # names requested via request_name
        self.connected = True

    async def call(self, msg):
        self.calls.append(msg)
        handler = self.handlers.get(msg.member)
        if handler is None:
            return Reply()
        result = handler(msg)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def request_name(self, name):
        self.names.append(name)

    def export(self, path, obj):
        self.exported[path] = obj

    def disconnect(self):
        self.connected = False
