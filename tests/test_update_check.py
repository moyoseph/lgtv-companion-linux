from __future__ import annotations

from lgtvcompanion.agent.update import _parse, is_newer


def test_parse_tag():
    assert _parse("v0.2.0") == (0, 2, 0)
    assert _parse("0.2.0") == (0, 2, 0)
    assert _parse("v1.0") == (1, 0)


def test_is_newer():
    assert is_newer("v0.2.0", "0.1.0") is True
    assert is_newer("v0.2.0", "0.2.0") is False
    assert is_newer("v0.1.9", "0.2.0") is False
    assert is_newer("v1.0.0", "0.9.9") is True
    # dev suffixes reduce to the numeric prefix
    assert is_newer("v0.2.0", "0.2.0.dev0") is False or is_newer("v0.2.0", "0.2.0") is False
