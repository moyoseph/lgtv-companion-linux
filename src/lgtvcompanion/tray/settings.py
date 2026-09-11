"""Settings dialog for the tray app (PySide6).

Edits the config file directly (via config.py load/validate/save) and asks the
daemon to hot-reload over IPC; if a change needs a restart (device add/remove,
host/ssl change) the dialog offers to run it via pkexec. Kept in its own module
so app.py stays importable without Qt.
"""

from __future__ import annotations

import subprocess

from .. import config as config_mod


def build_dialog(ipc_request):
    """Return a QDialog subclass instance, or raise ImportError without Qt.

    ipc_request(cmd, args, devices) -> dict is injected so the dialog can send
    `reload` without importing the tray's socket helper.
    """
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QSpinBox,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )

    class SettingsDialog(QDialog):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("LGTV Companion — Settings")
            self.resize(460, 480)
            self.cfg_path = config_mod.find_config()
            self.cfg = (config_mod.load(self.cfg_path) if self.cfg_path
                        else config_mod.Config())

            tabs = QTabWidget()
            tabs.addTab(self._global_tab(QWidget, QFormLayout, QCheckBox,
                                         QSpinBox, QComboBox), "General")
            tabs.addTab(self._devices_tab(QWidget, QVBoxLayout, QFormLayout,
                                          QLineEdit, QCheckBox, QSpinBox,
                                          QComboBox, QLabel), "Devices")
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Save
                | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(self._save)
            buttons.rejected.connect(self.reject)

            root = QVBoxLayout(self)
            root.addWidget(tabs)
            root.addWidget(buttons)
            self._msgbox = QMessageBox

        def _global_tab(self, QWidget, QFormLayout, QCheckBox, QSpinBox, QComboBox):
            g = self.cfg.global_
            w = QWidget()
            form = QFormLayout(w)
            self.idle_enabled = QCheckBox()
            self.idle_enabled.setChecked(g.idle.enabled)
            form.addRow("Blank screen when idle", self.idle_enabled)
            self.idle_minutes = QSpinBox()
            self.idle_minutes.setRange(1, 240)
            self.idle_minutes.setValue(g.idle.minutes)
            form.addRow("Idle timeout (minutes)", self.idle_minutes)
            self.mute_speakers = QCheckBox()
            self.mute_speakers.setChecked(g.idle.mute_speakers)
            form.addRow("Mute speakers when blanked", self.mute_speakers)
            self.wake_on_input = QCheckBox()
            self.wake_on_input.setChecked(g.wake_on_input.enabled)
            form.addRow("Wake TV on keyboard/controller", self.wake_on_input)
            self.power_on_boot = QCheckBox()
            self.power_on_boot.setChecked(g.power_on_at_boot)
            form.addRow("Power on TV at boot", self.power_on_boot)
            self.remote_stream = QCheckBox()
            self.remote_stream.setChecked(g.remote_stream.enabled)
            form.addRow("React to remote streaming", self.remote_stream)
            self.on_connect = QComboBox()
            self.on_connect.addItems(["blank", "off"])
            self.on_connect.setCurrentText(g.remote_stream.on_connect)
            form.addRow("  On stream start", self.on_connect)
            self.on_disconnect = QComboBox()
            self.on_disconnect.addItems(["on", "keep_off", "restore"])
            self.on_disconnect.setCurrentText(g.remote_stream.on_disconnect)
            form.addRow("  On stream end", self.on_disconnect)
            self.topology = QCheckBox()
            self.topology.setChecked(g.topology.enabled)
            form.addRow("Display-topology mode", self.topology)
            return w

        def _devices_tab(self, QWidget, QVBoxLayout, QFormLayout, QLineEdit,
                         QCheckBox, QSpinBox, QComboBox, QLabel):
            w = QWidget()
            box = QVBoxLayout(w)
            self.device_widgets = []
            if not self.cfg.devices:
                box.addWidget(QLabel("No devices. Use: lgtvc setup pair --host <ip>"))
            for dev in self.cfg.devices:
                form = QFormLayout()
                name = QLineEdit(dev.name)
                host = QLineEdit(dev.host)
                mac = QLineEdit(", ".join(dev.mac))
                ssl = QCheckBox()
                ssl.setChecked(dev.ssl)
                hdmi = QSpinBox()
                hdmi.setRange(0, 4)
                hdmi.setValue(dev.source_hdmi_input or 0)
                guard = QCheckBox()
                guard.setChecked(dev.check_hdmi_input_when_powering_off)
                wol = QComboBox()
                wol.addItems(["subnet", "broadcast", "directed", "auto"])
                wol.setCurrentText(dev.wol_method)
                form.addRow(QLabel(f"<b>{dev.id}</b>"))
                form.addRow("Name", name)
                form.addRow("Host/IP", host)
                form.addRow("MAC(s)", mac)
                form.addRow("Use TLS (wss)", ssl)
                form.addRow("PC's HDMI input (0=none)", hdmi)
                form.addRow("Only power off on PC input", guard)
                form.addRow("Wake-on-LAN method", wol)
                box.addLayout(form)
                self.device_widgets.append(
                    (dev, name, host, mac, ssl, hdmi, guard, wol))
            box.addStretch(1)
            return w

        def _collect(self):
            g = self.cfg.global_
            g.idle.enabled = self.idle_enabled.isChecked()
            g.idle.minutes = self.idle_minutes.value()
            g.idle.mute_speakers = self.mute_speakers.isChecked()
            g.wake_on_input.enabled = self.wake_on_input.isChecked()
            g.power_on_at_boot = self.power_on_boot.isChecked()
            g.remote_stream.enabled = self.remote_stream.isChecked()
            g.remote_stream.on_connect = self.on_connect.currentText()
            g.remote_stream.on_disconnect = self.on_disconnect.currentText()
            g.topology.enabled = self.topology.isChecked()
            for dev, name, host, mac, ssl, hdmi, guard, wol in self.device_widgets:
                dev.name = name.text().strip()
                dev.host = host.text().strip()
                dev.mac = [m.strip() for m in mac.text().split(",") if m.strip()]
                dev.ssl = ssl.isChecked()
                dev.source_hdmi_input = hdmi.value() or None
                dev.check_hdmi_input_when_powering_off = guard.isChecked()
                dev.wol_method = wol.currentText()

        def _save(self):
            self._collect()
            problems = config_mod.validate(self.cfg)
            if problems:
                self._msgbox.warning(self, "Invalid settings", "\n".join(problems))
                return
            target = self.cfg_path or config_mod.user_config_path()
            try:
                config_mod.save(self.cfg, target)
            except OSError as e:
                self._msgbox.critical(self, "Could not save",
                                      f"{target}: {e}\n\nSystem config may need "
                                      "root — try: sudo lgtvc setup edit-config")
                return
            self._apply()
            self.accept()

        def _apply(self):
            try:
                resp = ipc_request("reload", [], [])
            except Exception as e:
                self._msgbox.information(self, "Saved",
                                        f"Saved. Daemon not reachable ({e}); "
                                        "changes apply on next restart.")
                return
            if resp.get("ok") and resp.get("results", {}).get("restart_needed"):
                if self._msgbox.question(
                        self, "Restart needed",
                        "Some changes need a daemon restart. Restart now?") \
                        == self._msgbox.StandardButton.Yes:
                    subprocess.Popen(
                        ["pkexec", "systemctl", "restart", "lgtvc-daemon"])
            else:
                self._msgbox.information(self, "Saved", "Settings applied.")

    return SettingsDialog()
