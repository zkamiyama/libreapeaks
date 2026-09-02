#!/usr/bin/env python3
"""Fail unless pure EOF extensions read exactly like the plain REAPER cache."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REQUIRED_CASES = (
    "control",
    "trailing-zero-1",
    "trailing-ff-1",
    "trailing-zero-16",
    "trailing-rpkx-empty",
    "trailing-rpkx-timeline",
    "trailing-rpkx-timeline-repeat",
    "trailing-deterministic-4k",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    rows = {row["name"]: row for row in report["cases"]}
    failures: list[str] = []
    for name in REQUIRED_CASES:
        row = rows.get(name)
        if row is None:
            failures.append(f"{name}: missing")
            continue
        if row.get("begin") != 0:
            failures.append(f"{name}: BEGIN={row.get('begin')}")
        if not row.get("read_ok"):
            failures.append(f"{name}: read failed")
        if not row.get("read_signature_equals_control"):
            failures.append(f"{name}: GetPeaks differs from control")
        if not row.get("cache_unchanged"):
            failures.append(f"{name}: REAPER modified cache bytes")

    if failures:
        raise SystemExit("RPKX read gate failed:\n  " + "\n  ".join(failures))
    print("RPKX EOF-append read gate: all required cases are byte-preserved and GetPeaks-identical to control")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
