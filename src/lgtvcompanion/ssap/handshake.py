"""SSAP pairing: registration payload and client-key persistence.

webOS firmware 43.00.92+ (webOS 2025/2026) blacklisted the long-published
"LG Remote App" signed manifest (appId com.lge.test) — pairing with it now
fails "403 blacklisted certificate detected", and even where it pairs the
elevated permissions (e.g. the pointer-input/button socket) are withheld
(401). See upstream issue #351.

The fix (matching upstream LGTVC v5.5.0+ and every maintained webOS client
lib): send a **signature-free, generic** manifest — no `signed`/`signatures`
blocks — with all requested permissions in the outer `permissions` list. The
signed block existed only so LG-certified apps could skip the on-screen
prompt via signature verification; without it the user simply approves the
outer permissions at the PROMPT. This generic manifest is accepted on every
webOS version (old and new) and grants the full permission set.
"""

from __future__ import annotations

from pathlib import Path

# Generic, signature-free manifest (upstream "V3"). Full outer-permission set,
# granted via the on-screen PROMPT. Works on webOS 2025/2026 and earlier.
MANIFEST = {
    "manifestVersion": 1,
    "permissions": [
        "LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE",
        "TEST_OPEN", "TEST_PROTECTED", "CONTROL_AUDIO", "CONTROL_DISPLAY",
        "CONTROL_INPUT_JOYSTICK", "CONTROL_INPUT_MEDIA_RECORDING",
        "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_TV", "CONTROL_POWER",
        "CONTROL_TV_SCREEN", "READ_APP_STATUS", "READ_CURRENT_CHANNEL",
        "READ_INPUT_DEVICE_LIST", "READ_NETWORK_STATE", "READ_RUNNING_APPS",
        "READ_TV_CHANNEL_LIST", "WRITE_NOTIFICATION_TOAST", "READ_POWER_STATE",
        "READ_COUNTRY_INFO", "READ_SETTINGS", "CONTROL_INPUT_TEXT",
        "CONTROL_MOUSE_AND_KEYBOARD", "WRITE_SETTINGS", "WRITE_NOTIFICATION_ALERT",
        "READ_INSTALLED_APPS", "READ_UPDATE_INFO",
    ],
}


def register_payload(client_key: str | None) -> dict:
    return {
        "type": "register",
        "id": "register_0",
        "payload": {
            "client-key": client_key,
            "forcePairing": False,
            "manifest": MANIFEST,
            "pairingType": "PROMPT",
        },
    }


class KeyStore:
    """One plain-text key file per device, format-compatible with the legacy
    /etc/lgtvcontrol/client.key (bare key string + newline)."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path(self, device_id: str) -> Path:
        return self.directory / f"{device_id}.key"

    def load(self, device_id: str) -> str | None:
        try:
            return self.path(device_id).read_text().strip() or None
        except OSError:
            return None

    def save(self, device_id: str, key: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        p = self.path(device_id)
        p.write_text(key + "\n")
        p.chmod(0o600)
