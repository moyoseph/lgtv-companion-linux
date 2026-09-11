"""Button names for the pointer-input socket (vendored from upstream)."""

from __future__ import annotations

import json
from importlib import resources

with resources.files("lgtvcompanion.data").joinpath("lg_api_buttons.json").open() as f:
    BUTTONS: tuple[str, ...] = tuple(dict.fromkeys(json.load(f)["Buttons"]))


def is_valid_button(name: str) -> bool:
    return name.upper() in BUTTONS


def canonical_button(name: str) -> str:
    upper = name.upper()
    if upper in BUTTONS:
        return upper
    raise ValueError(f"unknown button {name!r} (see -help for the list)")
