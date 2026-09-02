#!/usr/bin/env python3
"""Force PCM_Source_GetPeaks reads through caches accepted by the extension oracle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
PEAK_RATE = 300
SHOWPEAKS = 64
PEAKCACHEGENMODE = 3


def write_config(path: Path) -> None:
    path.write_text(
        "[REAPER]\n"
        f"peakcachegenmode={PEAKCACHEGENMODE}\n"
        f"peakcachegenrs={PEAK_RATE}\n"
        f"showpeaks={SHOWPEAKS}\n",
        encoding="utf-8",
    )


def signature(status: str) -> list[str]:
    return [line for line in status.splitlines() if line.startswith("PEAK ")]


def status_value(status: str, prefix: str) -> str:
    return next(
        (line[len(prefix) :] for line in status.splitlines() if line.startswith(prefix)),
        "",
    )


def run_read(
    *,
    reaper: Path,
    source: Path,
    peak_path: Path,
    input_path: Path,
    case_name: str,
    display: str,
    results: Path,
    timeout: int,
) -> dict[str, object]:
    case_dir = results / "read-cases" / case_name
    case_dir.mkdir(parents=True, exist_ok=True)
    log_path = case_dir / "reaper.log"
    status_copy = case_dir / "status.txt"
    mutated = input_path.read_bytes()
    peak_path.write_bytes(mutated)
    before_stat = peak_path.stat()

    cfg_dir = Path(tempfile.mkdtemp(prefix=f"reapeaks-read-{case_name}-"))
    config = cfg_dir / "reaper.ini"
    write_config(config)
    status_path = cfg_dir / "result.txt"
    env = os.environ.copy()
    env.update(
        DISPLAY=display,
        REAPEAKS_MEDIA=str(source.resolve()),
        REAPEAKS_RESULT=str(status_path),
    )

    timed_out = False
    returncode: int | None = None
    try:
        with log_path.open("wb") as log:
            completed = subprocess.run(
                [
                    str(reaper),
                    "-newinst",
                    "-cfgfile",
                    str(config),
                    "-new",
                    "-nosplash",
                    str(HERE / "cache_read_probe.lua"),
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True

    status = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
    status_copy.write_text(status, encoding="utf-8")
    after_exists = peak_path.exists()
    after = peak_path.read_bytes() if after_exists else b""
    after_stat = peak_path.stat() if after_exists else None
    begin_text = status_value(status, "BEGIN=")
    begin = int(begin_text) if begin_text else None
    read_ok = "READ_OK=1" in status

    return {
        "name": case_name,
        "begin": begin,
        "returncode": returncode,
        "timed_out": timed_out,
        "read_ok": read_ok,
        "signature": signature(status),
        "status": status.strip(),
        "cache_unchanged": after_exists and after == mutated,
        "cache_mtime_unchanged": bool(
            after_stat is not None and after_stat.st_mtime_ns == before_stat.st_mtime_ns
        ),
        "input_sha256": hashlib.sha256(mutated).hexdigest(),
        "output_sha256": hashlib.sha256(after).hexdigest() if after_exists else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("reaper", type=Path)
    parser.add_argument("--display", default=":96")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()

    root = args.root.resolve()
    results = root / "results"
    report = json.loads((results / "cache-extension-oracle.json").read_text(encoding="utf-8"))
    source = Path(report["source"])
    peak_path = Path(report["peak_path"])
    accepted = [
        row for row in report["cases"] if row["classification"] == "accepted-preserved"
    ]
    accepted.sort(key=lambda row: 0 if row["name"] == "control" else 1)

    rows: list[dict[str, object]] = []
    for index, source_row in enumerate(accepted, 1):
        name = str(source_row["name"])
        print(f"[{index:02d}/{len(accepted):02d}] read {name}", flush=True)
        rows.append(
            run_read(
                reaper=args.reaper.resolve(),
                source=source,
                peak_path=peak_path,
                input_path=results / "cases" / name / "input.reapeaks",
                case_name=name,
                display=args.display,
                results=results,
                timeout=args.timeout,
            )
        )

    control = next(row for row in rows if row["name"] == "control")
    control_signature = control["signature"]
    if not control["read_ok"] or control["begin"] != 0:
        raise RuntimeError(f"control read probe failed: {control}")

    summary = {
        "same-as-control": 0,
        "different-read": 0,
        "read-failed": 0,
    }
    for row in rows:
        same = bool(row["read_ok"] and row["signature"] == control_signature)
        row["read_signature_equals_control"] = same
        if not row["read_ok"]:
            summary["read-failed"] += 1
        elif same:
            summary["same-as-control"] += 1
        else:
            summary["different-read"] += 1

    output = {
        "source": str(source),
        "peak_path": str(peak_path),
        "control_signature": control_signature,
        "summary": summary,
        "cases": rows,
    }
    out_path = results / "cache-read-matrix.json"
    out_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")

    print("# read summary")
    for key, value in summary.items():
        print(f"{key}: {value}")
    for row in rows:
        print(
            f"{row['name']}: begin={row['begin']} read_ok={row['read_ok']} "
            f"same={row['read_signature_equals_control']} cache_unchanged={row['cache_unchanged']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
