"""lgtvc-tray: system-tray control (optional [tray] extra, PySide6).

Core actions per device (power/screen/HDMI), idle force/release, and a settings
dialog (see settings.py). Talks to the daemon over the IPC socket with
short-lived synchronous requests — no Qt/asyncio bridge needed.

The menu-building and command logic live in module functions (build_menu,
_invoke, _run_action) so they're unit-testable under an offscreen QApplication;
main() is the thin Qt/event-loop shell.
"""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Callable

from .. import config as config_mod
from .. import ipc

_DEVICE_ACTIONS = (
    ("Power on", "poweron"),
    ("Power off", "poweroff"),
    ("Blank screen", "screenoff"),
    ("Unblank", "screenon"),
)


def ipc_request(cmd: str, args: list | None = None,
                devices: list[str] | None = None,
                socket_path: str | None = None, timeout: float = 30.0) -> dict:
    path = socket_path or ipc.default_socket()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(ipc.encode(
            {"id": 1, "cmd": cmd, "args": args or [], "devices": devices or []}))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            buf += chunk
    return json.loads(buf.splitlines()[0])


def _invoke(ipc_call: Callable, cmd: str, dev_ids: list[str] | None) -> dict:
    """Run one daemon command, raising RuntimeError on a daemon-reported error."""
    resp = ipc_call(cmd, devices=dev_ids)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "unknown error"))
    return resp.get("results")


def _run_action(cmd: str, dev_ids: list[str] | None, ipc_call: Callable,
                on_ok: Callable[[str, dict], None],
                on_error: Callable[[str, Exception], None]) -> None:
    """Invoke a command and route the outcome to the success/error callbacks
    (the tray toast / QMessageBox in the live app)."""
    try:
        results = _invoke(ipc_call, cmd, dev_ids)
        on_ok(cmd, results)
    except Exception as e:
        on_error(cmd, e)


def load_devices() -> list:
    cfg_path = config_mod.find_config()
    if cfg_path is None:
        return []
    try:
        return config_mod.load(cfg_path).devices
    except (ValueError, OSError):
        return []


def build_menu(devices: list, activate: Callable[[str, list[str] | None], None],
               open_settings: Callable[[], None], quit_cb: Callable[[], None]):
    """Build the tray context menu. `activate(cmd, dev_ids)` runs a device
    command; per-device submenus appear only with more than one TV."""
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QMenu

    menu = QMenu()

    def add_device_actions(target: QMenu, dev_ids: list[str] | None) -> None:
        for label, cmd in _DEVICE_ACTIONS:
            action = QAction(label, target)
            action.triggered.connect(
                lambda _checked=False, c=cmd, d=dev_ids: activate(c, d))
            target.addAction(action)

    if len(devices) > 1:
        for dev in devices:
            add_device_actions(menu.addMenu(dev.name or dev.id), [dev.id])
        add_device_actions(menu.addMenu("All TVs"), None)
    else:
        add_device_actions(menu, None)

    menu.addSeparator()
    for label, cmd in (("Idle now (blank)", "idle"), ("Resume from idle", "unidle")):
        action = QAction(label, menu)
        action.triggered.connect(lambda _checked=False, c=cmd: activate(c, None))
        menu.addAction(action)

    menu.addSeparator()
    settings_action = QAction("Settings…", menu)
    settings_action.triggered.connect(lambda _checked=False: open_settings())
    menu.addAction(settings_action)
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(lambda _checked=False: quit_cb())
    menu.addAction(quit_action)
    return menu


def main() -> None:  # pragma: no cover — Qt/event-loop shell; logic is tested above
    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon
    except ImportError:
        sys.exit("PySide6 not installed — pip install 'lgtvcompanion[tray]'")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    tray = QSystemTrayIcon(QIcon.fromTheme("video-television", QIcon.fromTheme("display")))
    tray.setToolTip("LGTV Companion")

    def on_ok(cmd: str, results: dict) -> None:
        tray.showMessage("LGTV Companion", f"-{cmd}: {json.dumps(results)}",
                         QSystemTrayIcon.MessageIcon.Information, 4000)

    def on_error(cmd: str, exc: Exception) -> None:
        QMessageBox.warning(None, "LGTV Companion", f"-{cmd} failed:\n{exc}")

    def activate(cmd: str, dev_ids: list[str] | None) -> None:
        _run_action(cmd, dev_ids, ipc_request, on_ok, on_error)

    def open_settings() -> None:
        try:
            from .settings import build_dialog
            build_dialog(lambda cmd, args, devices: ipc_request(cmd, args, devices)).exec()
        except Exception as e:
            QMessageBox.warning(None, "LGTV Companion", f"Settings failed:\n{e}")

    tray.setContextMenu(build_menu(load_devices(), activate, open_settings, app.quit))
    tray.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
