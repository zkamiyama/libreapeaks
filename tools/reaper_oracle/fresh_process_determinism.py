#!/usr/bin/env python3
"""Prove that REAPER oracle outputs are independent of prior oracle cases.

Every build is a separate REAPER process with a separate reaper.ini.  The Xvfb
server is intentionally shared because it is not part of REAPER's DSP state.
"""

from pathlib import Path
import json
import math
import os
import struct
import subprocess
import sys
import time
import wave

from fresh_process import run_one_source


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/reaper-fresh-process")
REAPER = Path(sys.argv[2] if len(sys.argv) > 2 else os.environ["REAPER_BIN"])
MEDIA = ROOT / "media"
RESULTS = ROOT / "results"
PROBE = (Path.cwd() / "tools/reaper_oracle/build_probe.lua").resolve()
DISPLAY = ":97"
REPEATS = 8

MEDIA.mkdir(parents=True, exist_ok=True)
RESULTS.mkdir(parents=True, exist_ok=True)


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", float(x)))[0]


def write_f32_wave(path: Path, sr: int, channels: int, frames: int, seed: int) -> None:
    payload = bytearray()
    state = seed & 0xFFFFFFFF
    freqs = (17.0, 97.0, 997.0, 3000.0, 6000.0, 11000.0, 0.21 * sr, 0.43 * sr)
    for frame in range(frames):
        for channel in range(channels):
            state = (1664525 * state + 1013904223 + channel) & 0xFFFFFFFF
            hz = min(freqs[channel % len(freqs)], 0.49 * sr)
            amp = 0.21 + 0.27 * (channel % 5)
            tone = amp * math.sin(2.0 * math.pi * hz * frame / sr + channel * 0.271)
            noise = ((((state >> 8) & 0xFFFFFF) - 0x800000) / float(0x800000)) * 1.0e-4
            value = tone + noise
            if frame in (0, 1, 255, 256, 257, frames - 2, frames - 1):
                value += (1.35 if (frame + channel) % 2 == 0 else -1.35)
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


def write_pcm16_wave(path: Path, sr: int, channels: int, frames: int, seed: int) -> None:
    values = []
    state = seed & 0xFFFFFFFF
    for frame in range(frames):
        for channel in range(channels):
            state = (1103515245 * state + 12345 + channel) & 0xFFFFFFFF
            hz = min((113.0, 997.0, 6000.0, 0.31 * sr)[channel % 4], 0.49 * sr)
            x = 0.68 * math.sin(2.0 * math.pi * hz * frame / sr + 0.19 * channel)
            x += ((((state >> 16) & 0xFFFF) - 32768) / 32768.0) * 0.002
            if frame in (0, 159, 160, 255, 256, 257, frames - 1):
                x = 0.999969 if (frame + channel) % 2 == 0 else -1.0
            values.append(max(-32768, min(32767, round(x * 32768.0))))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(sr)
        out.writeframes(struct.pack("<" + "h" * len(values), *values))


cases = [
    dict(name="f32_76800_299_4", kind="f32", sr=76_800, pps=299, ch=4, frames=50_550, seed=0x76800299),
    dict(name="f32_48000_1000_1", kind="f32", sr=48_000, pps=1_000, ch=1, frames=48_131, seed=0x48001000),
    dict(name="pcm16_48000_300_2", kind="pcm16", sr=48_000, pps=300, ch=2, frames=48_137, seed=0x48000300),
    dict(name="pcm16_96000_300_4", kind="pcm16", sr=96_000, pps=300, ch=4, frames=96_073, seed=0x96000300),
]

for case in cases:
    path = MEDIA / f"{case['name']}.wav"
    if case["kind"] == "f32":
        write_f32_wave(path, case["sr"], case["ch"], case["frames"], case["seed"])
    else:
        write_pcm16_wave(path, case["sr"], case["ch"], case["frames"], case["seed"])

xvfb_log = (RESULTS / "xvfb.log").open("wb")
xvfb = subprocess.Popen(
    ["Xvfb", DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
    stdout=xvfb_log,
    stderr=subprocess.STDOUT,
)

def build(case: dict, label: str):
    return run_one_source(
        reaper=REAPER,
        source=MEDIA / f"{case['name']}.wav",
        probe=PROBE,
        results_dir=RESULTS,
        label=label,
        display=DISPLAY,
        peakcachegenrs=case["pps"],
        showpeaks=1345,
        peakcachegenmode=3,
        expected_source_type="WAVE",
    )

try:
    time.sleep(0.4)
    baselines = {}
    report = {"policy": "one source = one fresh REAPER process", "repeats": REPEATS, "cases": {}}

    for case in cases:
        name = case["name"]
        hashes = []
        baseline_bytes = None
        for repeat in range(REPEATS):
            result = build(case, f"repeat-{name}-{repeat:02d}")
            payload = result.copied_peak.read_bytes()
            if baseline_bytes is None:
                baseline_bytes = payload
                baselines[name] = payload
            elif payload != baseline_bytes:
                raise SystemExit(
                    f"NONDETERMINISTIC_REAPER_OUTPUT case={name} repeat={repeat} "
                    f"baseline_sha256={hashes[0]} current_sha256={result.sha256}"
                )
            hashes.append(result.sha256)
        report["cases"][name] = {"sha256": hashes[0], "repeat_hashes": hashes}
        print(f"FRESH_REPEAT_EXACT {name} {REPEATS}/{REPEATS} sha256={hashes[0]}", flush=True)

    orders = [
        [0, 1, 2, 3],
        [3, 2, 1, 0],
        [1, 2, 3, 0],
        [2, 3, 0, 1],
        [2, 0, 3, 1],
        [1, 3, 0, 2],
    ]
    order_report = []
    for order_index, order in enumerate(orders):
        row = []
        for position, case_index in enumerate(order):
            case = cases[case_index]
            name = case["name"]
            result = build(case, f"order-{order_index:02d}-{position:02d}-{name}")
            payload = result.copied_peak.read_bytes()
            if payload != baselines[name]:
                raise SystemExit(
                    f"ORDER_DEPENDENT_REAPER_OUTPUT order={order_index} position={position} "
                    f"case={name} baseline_sha256={report['cases'][name]['sha256']} "
                    f"current_sha256={result.sha256}"
                )
            row.append(dict(case=name, sha256=result.sha256))
        order_report.append(row)
        print(f"FRESH_ORDER_EXACT order={order_index} cases={len(order)}", flush=True)

    report["orders"] = order_report
    (RESULTS / "fresh-process-determinism.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"FRESH_PROCESS_DETERMINISM_EXACT cases={len(cases)} repeats={REPEATS} "
        f"orders={len(orders)} invocations={len(cases) * REPEATS + len(cases) * len(orders)}",
        flush=True,
    )
finally:
    xvfb.terminate()
    try:
        xvfb.wait(timeout=3)
    except subprocess.TimeoutExpired:
        xvfb.kill()
    xvfb_log.close()
