"""Version-check notification (honours global.update_check).

Runs in the agent (session-side, so it can raise a desktop notification): once
a day it asks the GitHub releases API for the latest tag and, if it's newer
than the installed package, posts an org.freedesktop.Notifications toast. Purely
informational — never downloads or installs anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from importlib import metadata

from dbus_fast import Message
from dbus_fast.aio import MessageBus

log = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/moyoseph/lgtv-companion-linux/releases/latest"
CHECK_INTERVAL = 24 * 60 * 60
NOTIFICATIONS = "org.freedesktop.Notifications"


def _installed_version() -> str:
    try:
        return metadata.version("lgtvcompanion")
    except metadata.PackageNotFoundError:
        return "0"


def _parse(tag: str) -> tuple[int, ...]:
    """Loose semver tuple from a tag like 'v0.2.0' → (0, 2, 0)."""
    nums = []
    for part in tag.lstrip("vV").split("."):
        digits = "".join(c for c in part if c.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums)


def is_newer(latest_tag: str, installed: str) -> bool:
    return _parse(latest_tag) > _parse(installed)


def _fetch_latest_tag() -> str | None:
    try:
        req = urllib.request.Request(RELEASES_URL, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (https literal)
            return json.load(resp).get("tag_name")
    except Exception as e:
        log.debug("update check failed: %s", e)
        return None


class UpdateChecker:
    def __init__(self, bus: MessageBus | None):
        self.bus = bus
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="update-check")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            tag = await loop.run_in_executor(None, _fetch_latest_tag)
            installed = _installed_version()
            if tag and is_newer(tag, installed):
                log.info("update available: %s (installed %s)", tag, installed)
                await self._notify(
                    "LGTV Companion update available",
                    f"{tag} is out (you have {installed}). "
                    "Update with pipx/pip or your package manager.")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _notify(self, summary: str, body: str) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.call(Message(
                destination=NOTIFICATIONS, path="/org/freedesktop/Notifications",
                interface=NOTIFICATIONS, member="Notify",
                signature="susssasa{sv}i",
                body=["LGTV Companion", 0, "video-television", summary, body,
                      [], {}, 15000]))
        except Exception as e:
            log.debug("could not post notification: %s", e)
