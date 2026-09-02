#!/usr/bin/env python3
from pathlib import Path
import json
import math
import struct
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/spectrogram-f32/media")
root.mkdir(parents=True, exist_ok=True)

cases = [
    dict(name="silence_48k", sr=48_000, pps=300, frames=48_017, ch=1, kind="silence"),
    dict(name="exactbin_48k", sr=48_000, pps=300, frames=48_067, ch=1, kind="tone", hz=6000.0, amp=0.5),
    dict(name="offbin_48k", sr=48_000, pps=300, frames=48_079, ch=1, kind="tone", hz=997.0, amp=0.73),
    dict(name="over1_48k", sr=48_000, pps=300, frames=48_083, ch=1, kind="tone", hz=6000.0, amp=2.0),
    dict(name="tiny_48k", sr=48_000, pps=300, frames=48_089, ch=1, kind="tone", hz=6000.0, amp=1.0e-7),
    dict(name="stereo_44100", sr=44_100, pps=300, frames=88_237, ch=2, kind="lanes"),
    dict(name="threech_96k", sr=96_000, pps=500, frames=96_113, ch=3, kind="lanes"),
    dict(name="lowrate_22051", sr=22_051, pps=300, frames=44_139, ch=2, kind="lanes"),
    dict(name="branch_76800", sr=76_800, pps=300, frames=76_817, ch=1, kind="tone", hz=997.0, amp=0.43),
    dict(name="sixch_48k", sr=48_000, pps=300, frames=72_019, ch=6, kind="lanes"),
    dict(name="dc_over1", sr=48_000, pps=300, frames=48_101, ch=2, kind="dc", value=1.75),
    dict(name="impulse_96k", sr=96_000, pps=375, frames=96_127, ch=4, kind="impulse"),
]

(root / "cases.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")


def f32(value):
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def write_float_wave(path, sr, channels, payload):
    block_align = channels * 4
    byte_rate = sr * block_align
    fmt = struct.pack("<HHIIHH", 3, channels, sr, byte_rate, block_align, 32)
    riff_size = 4 + (8 + len(fmt)) + (8 + len(payload))
    wave = bytearray()
    wave += b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
    wave += b"fmt " + struct.pack("<I", len(fmt)) + fmt
    wave += b"data" + struct.pack("<I", len(payload)) + payload
    path.write_bytes(wave)


for case_index, case in enumerate(cases):
    sr = int(case["sr"])
    frames = int(case["frames"])
    channels = int(case["ch"])
    payload = bytearray()
    rough_fine = max(1, sr // max(1, int(case["pps"])))
    impulse_points = {
        0,
        1,
        2,
        63,
        64,
        127,
        128,
        255,
        256,
        257,
        max(0, rough_fine - 1),
        rough_fine,
        rough_fine + 1,
        max(0, frames - 3),
        max(0, frames - 2),
        max(0, frames - 1),
    }
    seed = (0x2468ACE1 ^ (case_index * 0x9E3779B9)) & 0xFFFFFFFF
    for frame in range(frames):
        for channel in range(channels):
            kind = case["kind"]
            if kind == "silence":
                value = 0.0
            elif kind == "dc":
                value = float(case["value"]) if channel % 2 == 0 else -float(case["value"])
            elif kind == "tone":
                phase = 2.0 * math.pi * float(case["hz"]) * frame / sr + channel * 0.173
                value = float(case["amp"]) * math.sin(phase)
            elif kind == "lanes":
                frequencies = (17.0, 97.0, 997.0, 3000.0, 6000.0, 11000.0)
                hz = min(frequencies[channel % len(frequencies)], 0.49 * sr)
                amp = 0.23 + 0.41 * (channel % 4)
                seed = (1664525 * seed + 1013904223 + channel) & 0xFFFFFFFF
                dither = ((((seed >> 8) & 0xFFFF) - 32768) / 32768.0) * 1.0e-4
                value = amp * math.sin(2.0 * math.pi * hz * frame / sr + channel * 0.271) + dither
            elif kind == "impulse":
                hit = frame in impulse_points or frame % 4093 == (channel * 257) % 4093
                value = (1.375 if (frame + channel) % 2 == 0 else -1.625) if hit else 0.0
            else:
                raise AssertionError(kind)
            payload += struct.pack("<f", f32(value))
    (root / f"{case['name']}.f32le").write_bytes(payload)
    write_float_wave(root / f"{case['name']}.wav", sr, channels, payload)

print(f"F32_SPECTROGRAM_CASES={len(cases)}")
