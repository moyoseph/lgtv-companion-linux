"""SSAP pairing: registration payload and client-key persistence."""

from __future__ import annotations

from pathlib import Path

# LG-issued signed manifest (appId com.lge.test). The signature must be sent
# verbatim or the TV rejects the pairing prompt for unknown clients.
MANIFEST = {
    "appVersion": "1.1",
    "manifestVersion": 1,
    "permissions": [
        "LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE",
        "TEST_OPEN", "TEST_PROTECTED", "CONTROL_AUDIO", "CONTROL_DISPLAY",
        "CONTROL_INPUT_JOYSTICK", "CONTROL_INPUT_MEDIA_RECORDING",
        "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_TV", "CONTROL_POWER",
        "CONTROL_TV_SCREEN", "READ_APP_STATUS", "READ_CURRENT_CHANNEL",
        "READ_INPUT_DEVICE_LIST", "READ_NETWORK_STATE", "READ_RUNNING_APPS",
        "READ_TV_CHANNEL_LIST", "WRITE_NOTIFICATION_TOAST", "READ_POWER_STATE",
        "READ_COUNTRY_INFO", "CONTROL_INPUT_TEXT", "CONTROL_MOUSE_AND_KEYBOARD",
        "READ_INSTALLED_APPS", "READ_SETTINGS", "READ_STORAGE_DEVICE_LIST",
    ],
    "signatures": [{"signature": (
        "eyJhbGdvcml0aG0iOiJSU0EtU0hBMjU2Iiwia2V5SWQiOiJ0ZXN0LXNpZ25pbm"
        "ctY2VydCIsInNpZ25hdHVyZVZlcnNpb24iOjF9.hrVRgjCwXVvE2OOSpDZ58hR"
        "+59aFNwYDyjQgKk3auukd7pcegmE2CzPCa0bJ0ZsRAcKkCTJrWo5iDzNhMBWRy"
        "aMOv5zWSrthlf7G128qvIlpMT0YNY+n/FaOHE73uLrS/g7swl3/qH/BGFG2Hu4"
        "RlL48eb3lLKqTt2xKHdCs6Cd4RMfJPYnzgvI4BNrFUKsjkcu+WD4OO2A27Pq1n"
        "50cMchmcaXadJhGrOqH5YmHdOCj5NSHzJYrsW0HPlpuAx/ECMeIZYDh6RMqaFM"
        "2DXzdKX9NmmyqzJ3o/0lkk/N97gfVRLW5hA29yeAwaCViZNCP8iC9aO0q9fQoj"
        "oa7NQnAtw=="
    ), "signatureVersion": 1}],
    "signed": {
        "appId": "com.lge.test",
        "created": "20140509",
        "localizedAppNames": {
            "": "LG Remote App",
            "ko-KR": "리모컨 앱",
            "zxx-XX": "ЛГ Rэмotэ AПП",
        },
        "localizedVendorNames": {"": "LG Electronics"},
        "permissions": [
            "TEST_SECURE", "CONTROL_INPUT_TEXT", "CONTROL_MOUSE_AND_KEYBOARD",
            "READ_INSTALLED_APPS", "READ_LGE_SDX", "READ_NOTIFICATIONS",
            "SEARCH", "WRITE_SETTINGS", "WRITE_NOTIFICATION_ALERT",
            "CONTROL_POWER", "READ_CURRENT_CHANNEL", "READ_RUNNING_APPS",
            "READ_UPDATE_INFO", "UPDATE_FROM_REMOTE_APP",
            "READ_LGE_TV_INPUT_EVENTS", "READ_TV_CURRENT_TIME",
        ],
        "serial": "2f930e2d2cfe083771f68e4fe7bb07",
        "vendorId": "com.lge",
    },
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
