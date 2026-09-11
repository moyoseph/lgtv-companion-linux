"""Output formatting: -output default|friendly|key <key>."""

from __future__ import annotations

import json
from typing import Any


def format_result(result: Any, mode: str = "default", key: str | None = None) -> str:
    if mode == "friendly":
        return json.dumps(result, indent=4, ensure_ascii=False)
    if mode == "key" and key is not None:
        found = _find_key(result, key)
        if found is None:
            return ""
        if isinstance(found, (dict, list)):
            return json.dumps(found, separators=(",", ":"), ensure_ascii=False)
        return str(found)
    return json.dumps(result, separators=(",", ":"), ensure_ascii=False)


def _find_key(obj: Any, key: str) -> Any:
    """Depth-first search for the first occurrence of `key`."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None
