#!/usr/bin/env python3
"""Infer REAPER's effective spectral analysis-frame length from live outputs.

This deliberately avoids changing libreapeaks.  For each fixed media source it
runs many independent peakcachegenrs/division configurations, always in fresh
REAPER processes.  The observed fine -'s' counts are then intersected against
the already-recovered spectral scheduler.  If the scheduler model is correct,
all divisions constrain the source to a small (ideally single-frame) set of
possible 22.05 kHz analysis lengths.

This distinguishes two questions that whole-file diffs conflate:
  1. how many analysis samples REAPER's source/resampler path exposes at EOF;
  2. how the spectral scheduler turns those samples into records.
"""

from __future__ import annotations

from pathlib import Path
import json
import math
import os
import struct
import subprocess
import sys
import time

from fresh_process import run_one_source


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/reaper-spectral-analysis-length")
REAPER = Path(sys.argv[2] if len(sys.argv) > 2 else os.environ["REAPER_BIN"])
MEDIA = ROOT / "media"
RESULTS = ROOT / "results"
PROBE = (Path.cwd() / "tools/reaper_oracle/build_probe.lua").resolve()
DISPLAY = ":94"
TOKEN_SPECTRAL = -115
ANALYSIS_RATE = 22_050.0

MEDIA.mkdir(parents=True, exist_ok=True)
RESULTS.mkdir(parents=True, exist_ok=True)


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", float(x)))[0]


def write_f32_wave(path: Path, sr: int, channels: int, frames: int, seed: int) -> None:
    payload = bytearray()
    state = seed & 0xFFFFFFFF
    tones = (97.0, 997.0, 2231.0, 6000.0, 0.31 * sr, 0.43 * sr)
    for frame in range(frames):
        for channel in range(channels):
            state = (1664525 * state + 1013904223 + channel) & 0xFFFFFFFF
            hz = min(tones[channel % len(tones)], 0.49 * sr)
            x = (0.25 + 0.09 * (channel % 5)) * math.sin(
                2.0 * math.pi * hz * frame / sr + 0.173 * channel
            )
            x += ((((state >> 8) & 0xFFFFFF) - 0x800000) / float(0x800000)) * 1.0e-4
            if frame in (0, 255, 256, 257, frames - 2, frames - 1):
                x += 0.81 if (frame + channel) % 2 == 0 else -0.81
            payload += struct.pack("<f", f32(x))

    block_align = channels * 4
    fmt = struct.pack("<HHIIHH", 3, channels, sr, sr * block_align, block_align, 32)
    riff_size = 4 + 8 + len(fmt) + 8 + len(payload)
    path.write_bytes(
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(payload))
        + payload
    )


def first_spectral_header(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:4] not in (b"RPKN", b"RPKL"):
        raise ValueError(f"unexpected magic {data[:4]!r}")
    layer_count = data[5]
    headers = [struct.unpack_from("<iI", data, 18 + 8 * i) for i in range(layer_count)]
    positive = [division for division, _ in headers if division > 0]
    if not positive:
        raise ValueError("no positive waveform division")
    for division, count in headers:
        if division == TOKEN_SPECTRAL:
            return positive[0], count
    raise ValueError("no spectral layer")


def scheduler_count(analysis_frames: int, source_rate: int, division: int) -> int:
    if analysis_frames <= 0 or source_rate <= 0 or division <= 0:
        return 0
    hop = division * ANALYSIS_RATE / source_rate
    rounded = math.floor(hop + 0.5)
    phase = (rounded - 1024) * 0.5 if rounded <= 1023 else 0.0
    count = 0
    for _ in range(analysis_frames):
        phase += 1.0
        while phase >= hop:
            count += 1
            phase -= hop
    return count


probes = [
    dict(name="48k_before", sr=48_000, ch=1, frames=48_131),
    dict(name="48k_after", sr=48_000, ch=1, frames=48_132),
    dict(name="76800_before", sr=76_800, ch=4, frames=50_549),
    dict(name="76800_edge", sr=76_800, ch=4, frames=50_550),
    dict(name="76800_after", sr=76_800, ch=4, frames=50_551),
    dict(name="96k_control", sr=96_000, ch=2, frames=96_073),
    dict(name="192k_before", sr=192_000, ch=2, frames=192_131),
    dict(name="192k_edge", sr=192_000, ch=2, frames=192_132),
    dict(name="192k_after", sr=192_000, ch=2, frames=192_134),
    dict(name="22051_before", sr=22_051, ch=2, frames=5_001),
    dict(name="22051_edge", sr=22_051, ch=2, frames=5_002),
    dict(name="22051_after", sr=22_051, ch=2, frames=5_003),
]

# Broadly separated values make the count constraints as independent as
# possible. REAPER may clamp very fine divisions; we use the division read back
# from the generated file rather than assuming floor(sr/pps).
pps_values = [100, 137, 150, 199, 257, 299, 300, 375, 499, 500, 733, 997, 1000, 1499, 1999, 2999, 4999]

for index, probe in enumerate(probes):
    path = MEDIA / f"{probe['name']}.wav"
    write_f32_wave(path, probe["sr"], probe["ch"], probe["frames"], 0xC0010000 ^ (index << 12) ^ probe["frames"])

xvfb_log = (RESULTS / "xvfb.log").open("wb")
xvfb = subprocess.Popen(
    ["Xvfb", DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
    stdout=xvfb_log,
    stderr=subprocess.STDOUT,
)

report = {
    "oracle": "REAPER 7.79 x86_64 Linux",
    "policy": "one source/configuration = one fresh REAPER process",
    "pps_values": pps_values,
    "probes": [],
}
try:
    time.sleep(0.4)
    for probe in probes:
        observations = []
        for pps in pps_values:
            result = run_one_source(
                reaper=REAPER,
                source=MEDIA / f"{probe['name']}.wav",
                probe=PROBE,
                results_dir=RESULTS,
                label=f"{probe['name']}-pps{pps}",
                display=DISPLAY,
                peakcachegenrs=pps,
            )
            division, count = first_spectral_header(result.copied_peak)
            observations.append(
                dict(pps=pps, division=division, observed_count=count, sha256=result.sha256)
            )

        ideal = probe["frames"] * ANALYSIS_RATE / probe["sr"]
        lo = max(0, math.floor(ideal) - 32)
        hi = math.ceil(ideal) + 32
        candidates = []
        for analysis_frames in range(lo, hi + 1):
            if all(
                scheduler_count(analysis_frames, probe["sr"], row["division"])
                == row["observed_count"]
                for row in observations
            ):
                candidates.append(analysis_frames)

        entry = dict(
            **probe,
            ideal_analysis_frames=ideal,
            candidate_analysis_frames=candidates,
            observations=observations,
        )
        report["probes"].append(entry)
        print(
            f"ANALYSIS_LENGTH probe={probe['name']} source_frames={probe['frames']} "
            f"ideal={ideal:.9f} candidates={candidates}",
            flush=True,
        )
        if not candidates:
            print(
                f"SCHEDULER_MODEL_NOT_SUFFICIENT probe={probe['name']}",
                flush=True,
            )
finally:
    xvfb.terminate()
    try:
        xvfb.wait(timeout=3)
    except subprocess.TimeoutExpired:
        xvfb.kill()
    xvfb_log.close()

(RESULTS / "spectral-analysis-length.json").write_text(
    json.dumps(report, indent=2) + "\n", encoding="utf-8"
)
empty = [p["name"] for p in report["probes"] if not p["candidate_analysis_frames"]]
print(
    f"SPECTRAL_ANALYSIS_LENGTH_DONE probes={len(probes)} "
    f"empty_candidate_sets={len(empty)} empty={empty}",
    flush=True,
)
