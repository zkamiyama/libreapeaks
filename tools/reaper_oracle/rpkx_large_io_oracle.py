#!/usr/bin/env python3
"""Measure pinned REAPER behavior with very large packed RPKX v1 EOF containers.

The payload is created as a sparse zero-filled extent so the test can exercise
large logical cache sizes without spending CI time writing hundreds of MiB. If
REAPER performs read()/pread64() across the payload, strace still observes the
logical bytes returned. mmap length is recorded separately because mapping a
large file is not evidence that the mapped pages were touched.

This is an observational performance oracle plus a compatibility gate. Timing
and syscall byte counts are published but deliberately not threshold-gated on
the first version of the experiment.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import time

HERE = Path(__file__).resolve().parent
RPKX_HEADER_SIZE = 32
RPKX_DIRECTORY_ENTRY_SIZE = 48
RPKX_PAYLOAD_OFFSET = RPKX_HEADER_SIZE + RPKX_DIRECTORY_ENTRY_SIZE
NAMESPACE = bytes.fromhex("107a928e49024c62a827a02983ee1101")
REQUESTED = 16
LINE_RE = re.compile(r"^PEAK rate=(\d+) extra=(\d+) ret=(\d+) values=(.*)$")
RETURN_RE = re.compile(r"=\s*(-?\d+)\s*$")
MMAP_LEN_RE = re.compile(r"mmap\([^,]+,\s*(\d+),")


def status_value(status: str, prefix: str) -> str:
    return next(
        (line[len(prefix) :] for line in status.splitlines() if line.startswith(prefix)),
        "",
    )


def signature(status: str) -> list[str]:
    normalized: list[str] = []
    for line in status.splitlines():
        match = LINE_RE.match(line)
        if match is None:
            continue
        rate, extra, retval_text, raw_values = match.groups()
        retval = int(retval_text)
        returned = retval & 0xFFFFF
        extra_available = bool(retval & 0x1000000)
        values = raw_values.split(",") if raw_values else []
        blocks = 3 if int(extra) != 0 and extra_available else 2
        kept: list[str] = []
        for block in range(blocks):
            start = block * REQUESTED
            kept.extend(values[start : start + returned])
        normalized.append(
            f"PEAK rate={rate} extra={extra} returned={returned} "
            f"output_mode={(retval & 0xF00000) >> 20} "
            f"extra_available={int(extra_available)} values={','.join(kept)}"
        )
    return normalized


def write_config(path: Path) -> None:
    path.write_text(
        "[REAPER]\n"
        "peakcachegenmode=3\n"
        "peakcachegenrs=300\n"
        "showpeaks=64\n",
        encoding="utf-8",
    )


def write_case(peak_path: Path, baseline: bytes, payload_len: int | None) -> None:
    with peak_path.open("wb") as handle:
        handle.write(baseline)
        if payload_len is None:
            return

        source_mtime, source_size = struct.unpack_from("<II", baseline, 10)
        container_len = RPKX_PAYLOAD_OFFSET + payload_len
        header = (
            b"RPKX"
            + struct.pack("<HHIIQII", 1, RPKX_HEADER_SIZE, 0, 1, container_len, source_mtime, source_size)
        )
        assert len(header) == RPKX_HEADER_SIZE
        directory = (
            NAMESPACE
            + b"LOAD"
            + struct.pack("<IIIQQ", 1, 0, 0, RPKX_PAYLOAD_OFFSET, payload_len)
        )
        assert len(directory) == RPKX_DIRECTORY_ENTRY_SIZE
        handle.write(header)
        handle.write(directory)
        handle.truncate(len(baseline) + container_len)


def parse_trace(path: Path) -> dict[str, int]:
    read_bytes = 0
    pread_bytes = 0
    peak_open_count = 0
    peak_mmap_max = 0
    if not path.exists():
        return {
            "read_bytes": 0,
            "pread_bytes": 0,
            "open_count": 0,
            "mmap_max_len": 0,
        }

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ".reapeaks>" not in line:
            continue
        if "openat(" in line:
            peak_open_count += 1
        if "read(" in line and "pread64(" not in line:
            match = RETURN_RE.search(line)
            if match and int(match.group(1)) > 0:
                read_bytes += int(match.group(1))
        if "pread64(" in line:
            match = RETURN_RE.search(line)
            if match and int(match.group(1)) > 0:
                pread_bytes += int(match.group(1))
        if "mmap(" in line:
            match = MMAP_LEN_RE.search(line)
            if match:
                peak_mmap_max = max(peak_mmap_max, int(match.group(1)))
    return {
        "read_bytes": read_bytes,
        "pread_bytes": pread_bytes,
        "open_count": peak_open_count,
        "mmap_max_len": peak_mmap_max,
    }


def run_case(
    *,
    reaper: Path,
    source: Path,
    peak_path: Path,
    baseline: bytes,
    name: str,
    payload_len: int | None,
    display: str,
    results: Path,
    timeout: int,
) -> dict[str, object]:
    write_case(peak_path, baseline, payload_len)
    before = peak_path.stat()

    case_dir = results / "large-rpkx" / name
    case_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = Path(tempfile.mkdtemp(prefix=f"reapeaks-large-rpkx-{name}-"))
    config = cfg_dir / "reaper.ini"
    status_path = cfg_dir / "result.txt"
    trace_path = case_dir / "strace.txt"
    log_path = case_dir / "reaper.log"
    write_config(config)

    env = os.environ.copy()
    env.update(
        DISPLAY=display,
        REAPEAKS_MEDIA=str(source.resolve()),
        REAPEAKS_RESULT=str(status_path),
    )
    command = [
        "strace",
        "-f",
        "-yy",
        "-qq",
        "-e",
        "trace=openat,read,pread64,lseek,mmap,munmap,close",
        "-o",
        str(trace_path),
        str(reaper),
        "-newinst",
        "-cfgfile",
        str(config),
        "-new",
        "-nosplash",
        str(HERE / "cache_read_probe.lua"),
    ]

    started = time.monotonic()
    timed_out = False
    returncode: int | None = None
    try:
        with log_path.open("wb") as log:
            completed = subprocess.run(
                command,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
    elapsed = time.monotonic() - started

    status = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
    (case_dir / "status.txt").write_text(status, encoding="utf-8")
    after = peak_path.stat() if peak_path.exists() else None
    begin_text = status_value(status, "BEGIN=")
    begin = int(begin_text) if begin_text else None
    traced = parse_trace(trace_path)

    return {
        "name": name,
        "payload_bytes": payload_len,
        "cache_bytes": before.st_size,
        "begin": begin,
        "read_ok": "READ_OK=1" in status,
        "signature": signature(status),
        "returncode": returncode,
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
        "cache_size_unchanged": after is not None and after.st_size == before.st_size,
        "cache_mtime_unchanged": after is not None and after.st_mtime_ns == before.st_mtime_ns,
        "reapeaks_read_bytes": traced["read_bytes"],
        "reapeaks_pread_bytes": traced["pread_bytes"],
        "reapeaks_open_count": traced["open_count"],
        "reapeaks_mmap_max_len": traced["mmap_max_len"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("reaper", type=Path)
    parser.add_argument("--display", default=":96")
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    root = args.root.resolve()
    results = root / "results"
    extension_report = json.loads(
        (results / "cache-extension-oracle.json").read_text(encoding="utf-8")
    )
    source = Path(extension_report["source"])
    peak_path = Path(extension_report["peak_path"])
    baseline = (results / "baseline.reapeaks").read_bytes()

    cases: list[tuple[str, int | None]] = [
        ("plain", None),
        ("rpkx-0mib", 0),
        ("rpkx-1mib", 1 * 1024 * 1024),
        ("rpkx-16mib", 16 * 1024 * 1024),
        ("rpkx-128mib", 128 * 1024 * 1024),
        ("rpkx-512mib", 512 * 1024 * 1024),
    ]

    rows: list[dict[str, object]] = []
    for index, (name, payload_len) in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {name}", flush=True)
        rows.append(
            run_case(
                reaper=args.reaper.resolve(),
                source=source,
                peak_path=peak_path,
                baseline=baseline,
                name=name,
                payload_len=payload_len,
                display=args.display,
                results=results,
                timeout=args.timeout,
            )
        )

    control = rows[0]
    control_signature = control["signature"]
    failures: list[str] = []
    for row in rows:
        same = bool(row["read_ok"] and row["signature"] == control_signature)
        row["read_signature_equals_control"] = same
        if row["begin"] != 0:
            failures.append(f"{row['name']}: BEGIN={row['begin']}")
        if not row["read_ok"]:
            failures.append(f"{row['name']}: read failed")
        if not same:
            failures.append(f"{row['name']}: GetPeaks differs from plain control")
        if not row["cache_size_unchanged"] or not row["cache_mtime_unchanged"]:
            failures.append(f"{row['name']}: REAPER modified cache")

    output = {
        "scope": "REAPER 7.79 Linux x86_64 / Ubuntu 24.04 / Xvfb / sparse zero RPKX payload",
        "layout": "RPKX v1 packed directory-first: 32-byte header + 48-byte directory entry + payload",
        "baseline_bytes": len(baseline),
        "cases": rows,
    }
    out_path = results / "rpkx-large-io-oracle.json"
    out_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")

    print("# large RPKX I/O oracle")
    for row in rows:
        print(
            f"{row['name']}: cache={row['cache_bytes']} elapsed={row['elapsed_seconds']:.3f}s "
            f"read={row['reapeaks_read_bytes']} pread={row['reapeaks_pread_bytes']} "
            f"mmap_max={row['reapeaks_mmap_max_len']} same={row['read_signature_equals_control']}"
        )
    if failures:
        raise SystemExit("large RPKX compatibility gate failed:\n  " + "\n  ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
