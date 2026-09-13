"""lgtvc-tray: system-tray control (optional [tray] extra, PySide6).

Core actions per device (power/screen/HDMI), idle force/release, and a settings
dialog (see settings.py). Talks to the daemon over the IPC socket with
short-lived synchronous requests — no Qt/asyncio bridge needed.
"""

from __future__ import annotations

import json
import socket
import sys

from .. import config as config_mod
from .. import ipc


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


def main() -> None:  # pragma: no cover — pure Qt wiring; ipc_request above is tested
    try:
        from PySide6.QtGui import QAction, QIcon
        from PySide6.QtWidgets import (
            QApplication,
            QMenu,
            QMessageBox,
            QSystemTrayIcon,
        )
    except ImportError:
        sys.exit("PySide6 not installed — pip install 'lgtvcompanion[tray]'")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    cfg_path = config_mod.find_config()
    devices = []
    if cfg_path is not None:
        try:
            devices = config_mod.load(cfg_path).devices
        except (ValueError, OSError):
            pass

    icon = QIcon.fromTheme("video-television", QIcon.fromTheme("display"))
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("LGTV Companion")
    menu = QMenu()

    def run(cmd: str, dev_ids: list[str] | None = None):
        def slot():
            try:
                resp = ipc_request(cmd, devices=dev_ids)
                if not resp.get("ok"):
                    raise RuntimeError(resp.get("error", "unknown error"))
                tray.showMessage("LGTV Companion",
                                 f"-{cmd}: {json.dumps(resp.get('results'))}",
                                 QSystemTrayIcon.MessageIcon.Information, 4000)
            except Exception as e:
                QMessageBox.warning(None, "LGTV Companion", f"-{cmd} failed:\n{e}")
        return slot

    def add_device_actions(target: QMenu, dev_ids: list[str] | None):
        for label, cmd in (("Power on", "poweron"), ("Power off", "poweroff"),
                           ("Blank screen", "screenoff"), ("Unblank", "screenon")):
            action = QAction(label, target)
            action.triggered.connect(run(cmd, dev_ids))
            target.addAction(action)

    if len(devices) > 1:
        for dev in devices:
            sub = menu.addMenu(dev.name or dev.id)
            add_device_actions(sub, [dev.id])
        all_menu = menu.addMenu("All TVs")
        add_device_actions(all_menu, None)
    else:
        add_device_actions(menu, None)

    menu.addSeparator()
    idle_on = QAction("Idle now (blank)", menu)
    idle_on.triggered.connect(run("idle"))
    menu.addAction(idle_on)
    idle_off = QAction("Resume from idle", menu)
    idle_off.triggered.connect(run("unidle"))
    menu.addAction(idle_off)

    menu.addSeparator()
    settings_action = QAction("Settings…", menu)

    def open_settings():
        try:
            from .settings import build_dialog
            dlg = build_dialog(
                lambda cmd, args, devices: ipc_request(cmd, args, devices))
            dlg.exec()
        except Exception as e:
            QMessageBox.warning(None, "LGTV Companion", f"Settings failed:\n{e}")

    settings_action.triggered.connect(open_settings)
    menu.addAction(settings_action)

    menu.addSeparator()
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
