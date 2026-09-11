from __future__ import annotations

from lgtvcompanion.agent.fullscreen import _KWIN_SCRIPT, FullscreenProbe


def test_kwin_script_has_no_unfilled_placeholders():
    for marker in ("@NAME@", "@PATH@", "@IFACE@"):
        assert marker not in _KWIN_SCRIPT
    assert "org.lgtvcompanion.Agent" in _KWIN_SCRIPT
    assert "callDBus" in _KWIN_SCRIPT
    # both KWin 5 and 6 active-window accessors present
    assert "activeWindow" in _KWIN_SCRIPT and "activeClient" in _KWIN_SCRIPT


def test_report_caches_fullscreen_state():
    probe = FullscreenProbe(bus=None)  # bus unused for the callback path
    probe._on_report(True, "steam_app_620")
    assert probe._latest == (True, "steam_app_620")
    assert probe._event.is_set()


def test_report_normalizes_empty_app():
    probe = FullscreenProbe(bus=None)
    probe._on_report(False, "")
    assert probe._latest == (False, "")
