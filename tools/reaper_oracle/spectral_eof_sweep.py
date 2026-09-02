#!/usr/bin/env python3
"""Observe REAPER 7.79 spectral EOF behavior without fitting an implementation.

The output is intentionally evidence, not a guessed compatibility rule.  Each
point is generated twice in independent REAPER processes and must be identical.
The report records the first -'s' layer count and the final records so scheduler
count and EOF payload behavior can be studied separately.
"""

from pathlib import Path
import json
import math
import os
import struct
import subprocess
import sys
import time

from fresh_process import run_one_source


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/reaper-spectral-eof")
REAPER = Path(sys.argv[2] if len(sys.argv) > 2 else os.environ["REAPER_BIN"])
MEDIA = ROOT / "media"
RESULTS = ROOT / "results"
PROBE = (Path.cwd() / "tools/reaper_oracle/build_probe.lua").resolve()
DISPLAY = ":98"
TOKEN_SPECTRAL = -115
ANALYSIS_RATE = 22_050

MEDIA.mkdir(parents=True, exist_ok=True)
RESULTS.mkdir(parents=True, exist_ok=True)


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", float(x)))[0]


def write_f32_wave(path: Path, sr: int, channels: int, frames: int, seed: int) -> None:
    payload = bytearray()
    state = seed & 0xFFFFFFFF
    freqs = (97.0, 997.0, 2231.0, 6000.0, 0.37 * sr, 0.43 * sr)
    for frame in range(frames):
        for channel in range(channels):
            state = (1664525 * state + 1013904223 + channel) & 0xFFFFFFFF
            hz = min(freqs[channel % len(freqs)], sr * 0.49)
            tone = (0.31 + 0.11 * channel) * math.sin(
                2.0 * math.pi * hz * frame / sr + 0.137 * channel
            )
            noise = ((((state >> 8) & 0xFFFFFF) - 0x800000) / float(0x800000)) * 2.0e-4
            value = tone + noise
            if frame in (0, 1, 255, 256, 257, frames - 3, frames - 2, frames - 1):
                value += 0.73 if (frame + channel) % 2 == 0 else -0.73
            payload += struct.pack("<f", f32(value))

    block_align = channels * 4
    byte_rate = sr * block_align
    fmt = struct.pack("<HHIIHH", 3, channels, sr, byte_rate, block_align, 32)
    riff_size = 4 + (8 + len(fmt)) + (8 + len(payload))
    data = bytearray()
    data += b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
    data += b"fmt " + struct.pack("<I", len(fmt)) + fmt
    data += b"data" + struct.pack("<I", len(payload)) + payload
    path.write_bytes(data)


def parse_first_spectral(path: Path) -> dict:
    data = path.read_bytes()
    if data[:4] not in (b"RPKN", b"RPKL"):
        raise ValueError(f"unexpected magic {data[:4]!r}")
    channels = data[4]
    layer_count = data[5]
    headers = [struct.unpack_from("<iI", data, 18 + 8 * i) for i in range(layer_count)]
    offset = 18 + 8 * layer_count
    for layer_index, (division, count) in enumerate(headers):
        size = count * channels * 4
        if division == TOKEN_SPECTRAL:
            payload = data[offset : offset + size]
            if len(payload) != size:
                raise ValueError("truncated spectral payload")
            logical = []
            start = max(0, count - 4)
            for time_index in range(start, count):
                lanes = []
                for channel in range(channels):
                    code = struct.unpack_from(
                        "<I", payload, (time_index * channels + channel) * 4
                    )[0]
                    lanes.append(
                        dict(
                            code=code,
                            frequency_hz=code & 0x7FFF,
                            density=(code >> 15) & 0x3FFF,
                        )
                    )
                logical.append(dict(index=time_index, channels=lanes))
            return dict(
                magic=data[:4].decode("ascii"),
                channels=channels,
                layer_index=layer_index,
                count=count,
                last_records=logical,
            )
        offset += size
    raise ValueError("no spectral layer")


def round_half_up_ratio(numerator: int, denominator: int) -> int:
    if numerator <= 0:
        return 0
    return (numerator + denominator // 2) // denominator


def candidate_count(frames: int, sr: int, division: int, center_twice: int) -> int:
    numerator = frames * ANALYSIS_RATE * 2 - center_twice * sr
    denominator = division * ANALYSIS_RATE * 2
    return round_half_up_ratio(numerator, denominator)


scenarios = [
    dict(name="48k_pps1000", sr=48_000, pps=1_000, ch=1, anchor=48_131, radius=10),
    dict(name="76800_pps299", sr=76_800, pps=299, ch=4, anchor=50_550, radius=12),
    dict(name="96k_pps375", sr=96_000, pps=375, ch=2, anchor=96_073, radius=10),
    dict(name="192k_pps1000", sr=192_000, pps=1_000, ch=2, anchor=192_131, radius=10),
    dict(name="22051_pps300", sr=22_051, pps=300, ch=2, anchor=5_000, radius=10),
]

xvfb_log = (RESULTS / "xvfb.log").open("wb")
xvfb = subprocess.Popen(
    ["Xvfb", DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
    stdout=xvfb_log,
    stderr=subprocess.STDOUT,
)

report = {"oracle": "REAPER 7.79 x86_64 Linux", "policy": "fresh process per point", "scenarios": []}
try:
    time.sleep(0.4)
    for scenario_index, scenario in enumerate(scenarios):
        sr = scenario["sr"]
        pps = scenario["pps"]
        ch = scenario["ch"]
        division = max(1, sr // pps)
        points = []
        previous_count = None
        for frames in range(
            scenario["anchor"] - scenario["radius"],
            scenario["anchor"] + scenario["radius"] + 1,
        ):
            source = MEDIA / f"{scenario['name']}_{frames}.wav"
            write_f32_wave(source, sr, ch, frames, 0x5EED0000 ^ frames ^ (scenario_index << 20))
            a = run_one_source(
                reaper=REAPER,
                source=source,
                probe=PROBE,
                results_dir=RESULTS,
                label=f"{scenario['name']}-{frames}-a",
                display=DISPLAY,
                peakcachegenrs=pps,
            )
            b = run_one_source(
                reaper=REAPER,
                source=source,
                probe=PROBE,
                results_dir=RESULTS,
                label=f"{scenario['name']}-{frames}-b",
                display=DISPLAY,
                peakcachegenrs=pps,
            )
            if a.copied_peak.read_bytes() != b.copied_peak.read_bytes():
                raise SystemExit(
                    f"NONDETERMINISTIC_EOF_POINT scenario={scenario['name']} frames={frames} "
                    f"a={a.sha256} b={b.sha256}"
                )
            observed = parse_first_spectral(a.copied_peak)
            row = dict(
                frames=frames,
                division=division,
                observed_count=observed["count"],
                candidate_center_512=candidate_count(frames, sr, division, 1024),
                candidate_center_511_5=candidate_count(frames, sr, division, 1023),
                sha256=a.sha256,
                last_records=observed["last_records"],
            )
            points.append(row)
            if previous_count is None or observed["count"] != previous_count:
                print(
                    f"EOF_TRANSITION scenario={scenario['name']} frames={frames} "
                    f"count={observed['count']} c512={row['candidate_center_512']} "
                    f"c511_5={row['candidate_center_511_5']}",
                    flush=True,
                )
            previous_count = observed["count"]
        report["scenarios"].append(
            dict(
                name=scenario["name"],
                sample_rate=sr,
                peakcachegenrs=pps,
                channels=ch,
                fine_division=division,
                points=points,
            )
        )
        print(f"EOF_SWEEP_DONE scenario={scenario['name']} points={len(points)}", flush=True)
finally:
    xvfb.terminate()
    try:
        xvfb.wait(timeout=3)
    except subprocess.TimeoutExpired:
        xvfb.kill()
    xvfb_log.close()

(RESULTS / "spectral-eof-sweep.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(
    "SPECTRAL_EOF_SWEEP_EXACT "
    f"scenarios={len(scenarios)} points={sum(len(x['points']) for x in report['scenarios'])} "
    "repeats_per_point=2",
    flush=True,
)
