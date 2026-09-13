#!/usr/bin/env python3
"""Emit shields.io endpoint JSON for the tests + coverage badges.

Usage: gen_badges.py <coverage.json> <num_tests> <output_dir>

Writes <output_dir>/coverage.json and <output_dir>/tests.json in the
shields.io "endpoint" schema. Published to the `badges` branch by CI and
read by the README badges.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def coverage_color(pct: int) -> str:
    if pct >= 90:
        return "brightgreen"
    if pct >= 80:
        return "green"
    if pct >= 70:
        return "yellowgreen"
    if pct >= 60:
        return "yellow"
    if pct >= 50:
        return "orange"
    return "red"


def main() -> None:
    cov_path, num_tests, out_dir = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)

    pct = round(json.loads(Path(cov_path).read_text())["totals"]["percent_covered"])

    badges = {
        "coverage.json": {
            "schemaVersion": 1,
            "label": "coverage",
            "message": f"{pct}%",
            "color": coverage_color(pct),
        },
        "tests.json": {
            "schemaVersion": 1,
            "label": "tests",
            "message": f"{num_tests} passed",
            "color": "brightgreen",
        },
    }
    for name, data in badges.items():
        (out_dir / name).write_text(json.dumps(data))
        print(name, data)


if __name__ == "__main__":
    main()
