"""Qt tray tests under an offscreen QApplication (the `qapp` fixture). Skips
cleanly where PySide6 is absent. Covers the menu builder + command helpers in
app.py and the settings dialog's collect/save/apply logic in settings.py."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from lgtvcompanion import config as config_mod  # noqa: E402
from lgtvcompanion.config import DeviceConfig  # noqa: E402
from lgtvcompanion.tray import app as tray_app  # noqa: E402


# -- pure command helpers -------------------------------------------------------


def test_invoke_returns_results_on_ok():
    def ipc(cmd, devices=None):
        return {"ok": True, "results": {"tv1": "ok"}}
    assert tray_app._invoke(ipc, "poweron", None) == {"tv1": "ok"}


def test_invoke_raises_on_daemon_error():
    def ipc(cmd, devices=None):
        return {"ok": False, "error": "boom"}
    with pytest.raises(RuntimeError, match="boom"):
        tray_app._invoke(ipc, "poweron", None)


def test_run_action_routes_success_and_error():
    oks: list = []
    errs: list = []
    tray_app._run_action(
        "poweron", ["tv1"],
        lambda cmd, devices=None: {"ok": True, "results": {"r": 1}},
        lambda cmd, res: oks.append((cmd, res)),
        lambda cmd, exc: errs.append((cmd, exc)))
    assert oks == [("poweron", {"r": 1})] and errs == []

    def raising(cmd, devices=None):
        raise ConnectionError("no daemon")

    tray_app._run_action("poweroff", None, raising,
                         lambda c, r: oks.append(c),
                         lambda c, e: errs.append((c, str(e))))
    assert errs[-1] == ("poweroff", "no daemon")


def test_load_devices_handles_missing_and_bad_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "find_config", lambda: None)
    assert tray_app.load_devices() == []
    bad = tmp_path / "config.json"
    bad.write_text("{ not json")
    monkeypatch.setattr(config_mod, "find_config", lambda: bad)
    assert tray_app.load_devices() == []


# -- build_menu (real QMenu, offscreen) -----------------------------------------


def _labels(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def test_build_menu_single_device_is_flat(qapp):
    devices = [DeviceConfig(id="tv1", host="h")]
    menu = tray_app.build_menu(devices, lambda *a: None, lambda: None, lambda: None)
    labels = _labels(menu)
    assert "Power on" in labels and "Power off" in labels
    assert "Settings…" in labels and "Quit" in labels
    # no submenus for a single device
    assert all(a.menu() is None for a in menu.actions())


def test_build_menu_multi_device_has_submenus(qapp):
    devices = [DeviceConfig(id="tv1", host="h", name="Living Room"),
               DeviceConfig(id="tv2", host="h2")]
    menu = tray_app.build_menu(devices, lambda *a: None, lambda: None, lambda: None)
    submenus = {a.text(): a.menu() for a in menu.actions() if a.menu() is not None}
    assert "Living Room" in submenus and "tv2" in submenus and "All TVs" in submenus
    # each submenu carries the four device actions
    assert _labels(submenus["tv2"]) == ["Power on", "Power off", "Blank screen", "Unblank"]


def test_menu_action_triggers_activate(qapp):
    fired: list = []
    devices = [DeviceConfig(id="tv1", host="h")]
    menu = tray_app.build_menu(
        devices, lambda cmd, devs: fired.append((cmd, devs)),
        lambda: None, lambda: None)
    power_on = next(a for a in menu.actions() if a.text() == "Power on")
    power_on.trigger()
    assert fired == [("poweron", None)]


def test_menu_settings_and_quit_trigger_callbacks(qapp):
    opened: list = []
    quit_called: list = []
    menu = tray_app.build_menu(
        [DeviceConfig(id="tv1", host="h")], lambda *a: None,
        lambda: opened.append(1), lambda: quit_called.append(1))
    next(a for a in menu.actions() if a.text() == "Settings…").trigger()
    next(a for a in menu.actions() if a.text() == "Quit").trigger()
    assert opened == [1] and quit_called == [1]


# -- settings dialog ------------------------------------------------------------


class FakeMsgBox:
    """Stand-in for QMessageBox so no modal dialog is shown."""

    class StandardButton:
        Yes = "YES"

    def __init__(self):
        self.calls: list = []
        self.question_result = "YES"

    def warning(self, *a):
        self.calls.append(("warning", a))

    def critical(self, *a):
        self.calls.append(("critical", a))

    def information(self, *a):
        self.calls.append(("information", a))

    def question(self, *a):
        self.calls.append(("question", a))
        return self.question_result


def _dialog(monkeypatch, tmp_path, cfg=None):
    if cfg is None:
        cfg = config_mod.Config(devices=[DeviceConfig(
            id="tv1", host="192.0.2.10", name="LG")])
    path = tmp_path / "config.json"
    config_mod.save(cfg, path)
    monkeypatch.setattr(config_mod, "find_config", lambda: path)
    from lgtvcompanion.tray.settings import build_dialog
    calls: list = []
    dlg = build_dialog(lambda *a: calls.append(a) or {"ok": True, "results": {}})
    dlg._ipc_calls = calls
    dlg._msgbox = FakeMsgBox()
    return dlg, path


def test_settings_collect_parses_widgets(qapp, monkeypatch, tmp_path):
    dlg, _ = _dialog(monkeypatch, tmp_path)
    dlg.idle_minutes.setValue(7)
    dlg.stream_processes.setText("parsec*, moonlight*")
    dev = dlg.device_widgets[0]
    dev[3].setText("aa:bb:cc:dd:ee:ff, 11:22:33:44:55:66")   # mac field
    dev[5].setValue(0)                                        # hdmi -> None sentinel
    dlg.steam_controller_wake.setChecked(False)
    dlg._collect()
    g = dlg.cfg.global_
    assert g.idle.minutes == 7
    assert g.remote_stream.processes == ["parsec*", "moonlight*"]
    assert g.steam_controller.wake is False
    assert dlg.cfg.devices[0].mac == ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"]
    assert dlg.cfg.devices[0].source_hdmi_input is None


def test_settings_save_writes_and_applies(qapp, monkeypatch, tmp_path):
    dlg, path = _dialog(monkeypatch, tmp_path)
    saved: list = []
    monkeypatch.setattr(config_mod, "save",
                        lambda cfg, p: saved.append(p))
    dlg._save()
    assert saved == [path]                       # config written to the loaded path
    assert dlg._ipc_calls and dlg._ipc_calls[0][0] == "reload"
    # ok result with restart_needed falsy -> "Settings applied" info
    assert any(c[0] == "information" for c in dlg._msgbox.calls)


def test_settings_save_reports_validation_errors(qapp, monkeypatch, tmp_path):
    dlg, _ = _dialog(monkeypatch, tmp_path)
    dlg.device_widgets[0][2].setText("")         # blank host -> invalid
    saved: list = []
    monkeypatch.setattr(config_mod, "save", lambda cfg, p: saved.append(p))
    dlg._save()
    assert saved == []                           # never saved
    assert any(c[0] == "warning" for c in dlg._msgbox.calls)


def test_settings_save_handles_oserror(qapp, monkeypatch, tmp_path):
    dlg, _ = _dialog(monkeypatch, tmp_path)

    def boom(cfg, p):
        raise OSError("permission denied")

    monkeypatch.setattr(config_mod, "save", boom)
    dlg._save()
    assert any(c[0] == "critical" for c in dlg._msgbox.calls)


def test_settings_apply_offers_restart(qapp, monkeypatch, tmp_path):
    import lgtvcompanion.tray.settings as settings_mod
    from lgtvcompanion.tray.settings import build_dialog
    popen: list = []
    monkeypatch.setattr(settings_mod.subprocess, "Popen",
                        lambda cmd: popen.append(cmd))
    d = build_dialog(lambda *a: {"ok": True, "results": {"restart_needed": True}})
    d._msgbox = FakeMsgBox()                     # question() returns Yes
    d._apply()
    assert any(c[0] == "question" for c in d._msgbox.calls)
    assert popen and popen[0][:2] == ["pkexec", "systemctl"]


def test_settings_apply_daemon_unreachable(qapp, monkeypatch, tmp_path):
    from lgtvcompanion.tray.settings import build_dialog

    def unreachable(*a):
        raise ConnectionError("no daemon")

    d = build_dialog(unreachable)
    d._msgbox = FakeMsgBox()
    d._apply()
    assert any(c[0] == "information" for c in d._msgbox.calls)
