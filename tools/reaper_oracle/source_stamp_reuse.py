#!/usr/bin/env python3
"""Prove that libreapeaks source stamps are accepted by pinned REAPER.

The probe distinguishes two questions:

1. Does libreapeaks derive the same 32-bit source mtime/size header fields that
   REAPER writes for the same media file?
2. When a libreapeaks-generated cache with those fields is placed at REAPER's
   canonical path, does PCM_Source_BuildPeaks(src, 0) return zero (reuse) rather
   than requesting a rebuild?

It also checks that clearly mismatched mtime/size values request a rebuild, and
that changing source PCM bytes while preserving both metadata fields is still
accepted. The latter establishes that source-content hashing is not part of the
observed update check for this pinned oracle; cache validity/preferences remain
separate rebuild conditions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import wave

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fresh_process import run_one_source  # noqa: E402

SAMPLE_RATE = 48_000
CHANNELS = 1
FRAMES = SAMPLE_RATE * 2
PEAK_RATE = 300
SHOWPEAKS = 64
PEAKCACHEGENMODE = 3
FIXED_MTIME = 1_700_000_000


def make_media(wav_path: Path, raw_path: Path) -> None:
    samples = []
    for frame in range(FRAMES):
        sample = round(24_000 * math.sin(2 * math.pi * 997 * frame / SAMPLE_RATE))
        if frame == 0:
            sample = -32768
        elif frame == 1:
            sample = 32767
        samples.append(sample)
    raw = struct.pack("<" + "h" * len(samples), *samples)
    raw_path.write_bytes(raw)
    with wave.open(str(wav_path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(raw)
    fixed_ns = FIXED_MTIME * 1_000_000_000
    os.utime(wav_path, ns=(fixed_ns, fixed_ns))


def parse_header(data: bytes) -> dict[str, object]:
    if len(data) < 18:
        raise RuntimeError("truncated .reapeaks header")
    count = data[5]
    if len(data) < 18 + count * 8:
        raise RuntimeError("truncated .reapeaks layer table")
    return {
        "magic": data[:4].decode("ascii", "replace"),
        "channels": data[4],
        "layer_count": count,
        "sample_rate": struct.unpack_from("<I", data, 6)[0],
        "source_mtime_low32": struct.unpack_from("<I", data, 10)[0],
        "source_size_low32": struct.unpack_from("<I", data, 14)[0],
        "layers": [
            list(struct.unpack_from("<iI", data, 18 + 8 * index))
            for index in range(count)
        ],
    }


def write_config(path: Path) -> None:
    path.write_text(
        "[REAPER]\n"
        f"peakcachegenmode={PEAKCACHEGENMODE}\n"
        f"peakcachegenrs={PEAK_RATE}\n"
        f"showpeaks={SHOWPEAKS}\n",
        encoding="utf-8",
    )


def status_value(status: str, prefix: str) -> str:
    return next(
        (line[len(prefix) :] for line in status.splitlines() if line.startswith(prefix)),
        "",
    )


def run_reuse_probe(
    *,
    reaper: Path,
    source: Path,
    peak_path: Path,
    label: str,
    display: str,
    results: Path,
) -> dict[str, object]:
    cfg_dir = Path(tempfile.mkdtemp(prefix=f"reapeaks-reuse-{label}-"))
    config = cfg_dir / "reaper.ini"
    write_config(config)
    status_path = cfg_dir / "result.txt"
    log_path = results / f"{label}.reaper.log"
    before = peak_path.read_bytes()
    before_stat = peak_path.stat()
    env = os.environ.copy()
    env.update(
        DISPLAY=display,
        REAPEAKS_MEDIA=str(source.resolve()),
        REAPEAKS_RESULT=str(status_path),
    )
    with log_path.open("wb") as log:
        completed = subprocess.run(
            [
                str(reaper),
                "-newinst",
                "-cfgfile",
                str(config),
                "-new",
                "-nosplash",
                str(HERE / "check_peak_reuse.lua"),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=120,
        )
    status = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
    if completed.returncode != 0 or "BEGIN=" not in status:
        raise RuntimeError(
            f"{label}: REAPER probe failed rc={completed.returncode}: {status!r}; log={log_path}"
        )
    reported_write = status_value(status, "PEAK_WRITE=")
    if reported_write and Path(reported_write).resolve() != peak_path.resolve():
        raise RuntimeError(
            f"{label}: canonical write path moved: {reported_write!r} != {str(peak_path)!r}"
        )
    begin = int(status_value(status, "BEGIN="))
    after_exists = peak_path.exists()
    after = peak_path.read_bytes() if after_exists else b""
    after_stat = peak_path.stat() if after_exists else None
    return {
        "label": label,
        "begin": begin,
        "reuse": begin == 0,
        "status": status.strip(),
        "peak_unchanged": after_exists and after == before,
        "peak_mtime_unchanged": bool(
            after_stat is not None and after_stat.st_mtime_ns == before_stat.st_mtime_ns
        ),
        "peak_sha256_before": hashlib.sha256(before).hexdigest(),
        "peak_sha256_after": hashlib.sha256(after).hexdigest() if after_exists else None,
    }


def patch_u32(data: bytes, offset: int, value: int) -> bytes:
    out = bytearray(data)
    struct.pack_into("<I", out, offset, value & 0xFFFF_FFFF)
    return bytes(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("reaper", type=Path)
    parser.add_argument("--display", default=":97")
    args = parser.parse_args()

    root = args.root.resolve()
    results = root / "results"
    media_dir = root / "media"
    results.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "source-stamp.wav"
    raw = media_dir / "source-stamp.pcm16le"
    make_media(source, raw)

    oracle = run_one_source(
        reaper=args.reaper.resolve(),
        source=source,
        probe=HERE / "build_one.lua",
        results_dir=results,
        label="oracle",
        display=args.display,
        peakcachegenrs=PEAK_RATE,
        showpeaks=SHOWPEAKS,
        peakcachegenmode=PEAKCACHEGENMODE,
        expected_source_type="WAVE",
    )
    peak_path = oracle.peak_path.resolve()
    oracle_bytes = peak_path.read_bytes()
    oracle_header = parse_header(oracle_bytes)
    if any(int(layer[0]) <= 0 for layer in oracle_header["layers"]):
        raise RuntimeError(
            "source-stamp reuse oracle requires a waveform-only showpeaks configuration"
        )

    source_stat = source.stat()
    expected_stamp = (
        int(source_stat.st_mtime) & 0xFFFF_FFFF,
        source_stat.st_size & 0xFFFF_FFFF,
    )
    oracle_stamp = (
        int(oracle_header["source_mtime_low32"]),
        int(oracle_header["source_size_low32"]),
    )
    if oracle_stamp != expected_stamp:
        raise RuntimeError(
            f"REAPER header stamp differs from stat(): oracle={oracle_stamp}, stat={expected_stamp}"
        )

    subprocess.run(
        [
            "cargo",
            "run",
            "--quiet",
            "--example",
            "source_stamp_fixture",
            "--",
            str(source),
            str(raw),
            str(peak_path),
        ],
        cwd=HERE.parents[1],
        check=True,
        timeout=180,
    )
    libreapeaks_bytes = peak_path.read_bytes()
    (results / "libreapeaks.reapeaks").write_bytes(libreapeaks_bytes)
    libreapeaks_header = parse_header(libreapeaks_bytes)
    libreapeaks_stamp = (
        int(libreapeaks_header["source_mtime_low32"]),
        int(libreapeaks_header["source_size_low32"]),
    )
    if libreapeaks_stamp != oracle_stamp:
        raise RuntimeError(
            f"libreapeaks stamp differs from REAPER: lib={libreapeaks_stamp}, oracle={oracle_stamp}"
        )

    cases: list[dict[str, object]] = []

    # The central interoperability gate: REAPER must accept the cache generated
    # by libreapeaks without starting a peak rebuild.
    peak_path.write_bytes(libreapeaks_bytes)
    case = run_reuse_probe(
        reaper=args.reaper,
        source=source,
        peak_path=peak_path,
        label="matching-lib-cache",
        display=args.display,
        results=results,
    )
    if not case["reuse"] or not case["peak_unchanged"]:
        raise RuntimeError(f"REAPER did not reuse the matching libreapeaks cache: {case}")
    cases.append(case)

    # A clear mtime mismatch must request a rebuild. 120 seconds is deliberately
    # outside the documented few-second and one-hour/DST tolerances.
    mismatched_mtime = (oracle_stamp[0] + 120) & 0xFFFF_FFFF
    peak_path.write_bytes(patch_u32(libreapeaks_bytes, 10, mismatched_mtime))
    case = run_reuse_probe(
        reaper=args.reaper,
        source=source,
        peak_path=peak_path,
        label="mtime-mismatch",
        display=args.display,
        results=results,
    )
    if case["reuse"]:
        raise RuntimeError(f"REAPER accepted a 120-second mtime mismatch: {case}")
    cases.append(case)

    peak_path.write_bytes(
        patch_u32(libreapeaks_bytes, 14, (oracle_stamp[1] + 1) & 0xFFFF_FFFF)
    )
    case = run_reuse_probe(
        reaper=args.reaper,
        source=source,
        peak_path=peak_path,
        label="size-mismatch",
        display=args.display,
        results=results,
    )
    if case["reuse"]:
        raise RuntimeError(f"REAPER accepted a source-size mismatch: {case}")
    cases.append(case)

    # Change actual PCM bytes while preserving both update-detection fields. If
    # this is still reused, the pinned REAPER source-update check is metadata
    # based rather than content-hash based.
    peak_path.write_bytes(libreapeaks_bytes)
    source_bytes = bytearray(source.read_bytes())
    if len(source_bytes) <= 46:
        raise RuntimeError("unexpectedly short WAV fixture")
    source_bytes[44] ^= 0x01
    source.write_bytes(source_bytes)
    fixed_ns = FIXED_MTIME * 1_000_000_000
    os.utime(source, ns=(fixed_ns, fixed_ns))
    changed_stat = source.stat()
    changed_stamp = (
        int(changed_stat.st_mtime) & 0xFFFF_FFFF,
        changed_stat.st_size & 0xFFFF_FFFF,
    )
    if changed_stamp != oracle_stamp:
        raise RuntimeError(
            f"content-only mutation did not preserve source stamp: {changed_stamp} != {oracle_stamp}"
        )
    case = run_reuse_probe(
        reaper=args.reaper,
        source=source,
        peak_path=peak_path,
        label="content-changed-same-stamp",
        display=args.display,
        results=results,
    )
    if not case["reuse"]:
        raise RuntimeError(
            "REAPER detected content changed despite identical source mtime/size; "
            f"update assumptions need revision: {case}"
        )
    cases.append(case)

    report = {
        "reaper": str(args.reaper.resolve()),
        "source": str(source),
        "oracle_peak": str(peak_path),
        "oracle_stamp": {
            "mtime_low32": oracle_stamp[0],
            "size_low32": oracle_stamp[1],
        },
        "libreapeaks_stamp": {
            "mtime_low32": libreapeaks_stamp[0],
            "size_low32": libreapeaks_stamp[1],
        },
        "oracle_sha256": hashlib.sha256(oracle_bytes).hexdigest(),
        "libreapeaks_sha256": hashlib.sha256(libreapeaks_bytes).hexdigest(),
        "libreapeaks_byte_identical_to_oracle": libreapeaks_bytes == oracle_bytes,
        "cases": cases,
    }
    report_path = results / "source-stamp-reuse.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
