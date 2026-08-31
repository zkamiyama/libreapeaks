#!/usr/bin/env python3
"""Persist REAPER's canonical peak-cache paths for external generators.

The resulting JSON contains path policy only. This command does not decode
media and does not ask REAPER to generate a ``.reapeaks`` file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
if str(EXAMPLES) not in sys.path:
    sys.path.insert(0, str(EXAMPLES))

from reaper_config import (  # noqa: E402
    ReaperConfigError,
    build_reaper_cache_map,
    load_reaper_cache_map,
    write_reaper_cache_map,
)


def collect_media(values: list[Path], *, recursive: bool) -> list[Path]:
    files: list[Path] = []
    for raw in values:
        path = raw.expanduser().resolve(strict=False)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.iterdir()
            files.extend(candidate for candidate in iterator if candidate.is_file())
        else:
            raise ReaperConfigError(f"media path not found: {path}")
    unique: list[Path] = []
    seen: set[str] = set()
    for path in files:
        key = str(path.resolve(strict=False))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    if not unique:
        raise ReaperConfigError("no media files were selected")
    return unique


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", nargs="+", type=Path)
    parser.add_argument("--reaper-executable", required=True, type=Path)
    parser.add_argument("--reaper-ini", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument(
        "--merge",
        action="store_true",
        help="preserve entries already present in --output",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        media = collect_media(args.media, recursive=args.recursive)
        payload = build_reaper_cache_map(
            media,
            reaper_executable=args.reaper_executable,
            reaper_ini=args.reaper_ini,
            timeout=args.timeout,
        )
        if args.merge and args.output.is_file():
            existing = load_reaper_cache_map(args.output)
            entries = payload["entries"]
            assert isinstance(entries, dict)
            for key, paths in existing.items():
                entries.setdefault(key, paths.to_json())
        output = write_reaper_cache_map(args.output, payload)
    except ReaperConfigError as exc:
        print(f"make_cache_map: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "entries": len(payload["entries"]),
                "generated_peak_files": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
