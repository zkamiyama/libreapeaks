#!/usr/bin/env python3
from pathlib import Path
import json
import math
import struct
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/spectrogram-f32-stress/media")
root.mkdir(parents=True, exist_ok=True)
cases = []


def add(name, sr, pps, frames, ch, kind, **extra):
    case = dict(name=name, sr=int(sr), pps=int(pps), frames=int(frames), ch=int(ch), kind=kind)
    case.update(extra)
    cases.append(case)


for sr in (76_799, 76_800, 76_801):
    add(f"rate_threshold_{sr}", sr, 300, sr + 17, 1, "tone", hz=997.0, amp=0.43)
    add(f"rate_threshold_{sr}_3ch", sr, 300, sr * 2 + 113, 3, "lanes")

for sr in (32_000, 44_100, 48_000, 96_000, 192_000):
    for pps in (100, 150, 300, 500, 1_000):
        ch = 1 + ((sr // 1_000 + pps) % 4)
        add(f"prefs_{sr}_{pps}", sr, pps, sr + 131, ch, "lanes")

for sr, pps in (
    (44_100, 171),
    (44_100, 172),
    (44_100, 173),
    (48_000, 187),
    (48_000, 188),
    (48_000, 189),
    (96_000, 374),
    (96_000, 375),
    (96_000, 376),
):
    add(f"fine256_{sr}_{pps}", sr, pps, sr + 73, 2, "tone", hz=997.0, amp=0.47)

for index, sr in enumerate(
    (8_000, 11_025, 16_000, 22_050, 22_051, 24_000, 32_000, 44_100, 48_000, 88_200, 96_000, 176_400, 192_000)
):
    add(
        f"rate_sweep_{sr}",
        sr,
        300,
        sr + 37 + index,
        3,
        "chirp",
        f0=7.0,
        f1=max(20.0, sr * 0.49),
        amp=0.61,
    )

add("ext_silence", 48_000, 300, 48_017, 1, "silence")
add("ext_dc_pos", 48_000, 300, 48_019, 1, "dc", value=1.0)
add("ext_dc_neg", 48_000, 300, 48_023, 1, "dc", value=-1.0)
add("ext_dc_over1", 48_000, 300, 48_027, 2, "dc_lanes", value=1.75)
add("ext_alt", 48_000, 300, 48_029, 1, "alt", amp=1.0)
add("ext_tiny_alt", 48_000, 300, 48_031, 1, "alt", amp=1.0e-7)
add("ext_square", 48_000, 300, 48_037, 1, "square", period=17, amp=0.93)
add("ext_step", 48_000, 300, 48_041, 1, "step", amp=0.91)
add("ext_ramp", 48_000, 300, 48_043, 1, "ramp", period=257, amp=0.97)
add("ext_chirp", 48_000, 300, 48_047, 1, "chirp", f0=1.0, f1=23_999.0, amp=0.72)
add("ext_noise_full", 48_000, 300, 48_053, 1, "noise", amp=1.2)
add("ext_noise_quiet", 48_000, 300, 48_059, 1, "noise", amp=1.0e-4)
add("ext_impulse_train", 48_000, 300, 96_061, 2, "impulse", amp=1.625)
for bin_index in (1, 2, 63, 127):
    add(
        f"ext_exact_bin{bin_index}",
        48_000,
        300,
        48_067 + bin_index,
        1,
        "exact_bin",
        bin=bin_index,
        amp=0.77,
    )
add("ext_offbin_17hz", 48_000, 300, 48_079, 1, "tone", hz=17.0, amp=0.83)
for name, amp in (
    ("tiny", 1.0e-7),
    ("very_tiny", 1.0e-20),
    ("half", 0.5),
    ("near_full", 0.999_999),
    ("over1", 1.25),
    ("over2", 2.5),
):
    add(f"ext_amp_{name}", 48_000, 300, 48_083 + len(cases) % 17, 1, "tone", hz=6000.0, amp=amp)

for ch in range(1, 9):
    add(f"channels_{ch}", 96_000, 375, 30_011 + ch * 17, ch, "lanes")
add("channels_sparse_8", 48_000, 300, 72_013, 8, "sparse", active=6, amp=1.4)

for frames in (319, 320, 321, 5_119, 5_120, 5_121, 91_519, 91_520, 91_521, 288_319, 288_320, 288_321):
    add(f"boundary96_{frames}", 96_000, 300, frames, 1, "tone", hz=997.0, amp=0.43)

add("long_48k_8ch", 48_000, 300, 480_037, 8, "lanes")
add("long_96k_7ch", 96_000, 300, 672_061, 7, "lanes")
add("long_192k_5ch", 192_000, 300, 806_417, 5, "lanes")

rates = (
    8_000,
    11_025,
    16_000,
    22_050,
    22_051,
    24_000,
    32_000,
    44_100,
    48_000,
    76_799,
    76_800,
    76_801,
    88_200,
    96_000,
    176_400,
    192_000,
)
preferences = (100, 149, 150, 171, 172, 173, 187, 188, 200, 299, 300, 301, 374, 375, 376, 499, 500, 501, 1_000)
kinds = ("lanes", "noise", "chirp", "impulse", "ramp", "square", "tone")
state = 0x6F32A5C3
for index in range(27):
    state = (1664525 * state + 1013904223) & 0xFFFFFFFF
    sr = rates[state % len(rates)]
    state = ((state << 9) | (state >> 23)) & 0xFFFFFFFF
    pps = preferences[state % len(preferences)]
    ch = 1 + ((state >> 5) % 8)
    frames = 257 + ((state >> 8) % max(513, int(sr * 1.6)))
    kind = kinds[(state >> 17) % len(kinds)]
    extra = {}
    if kind == "noise":
        extra["amp"] = 0.02 + (state % 250) / 100.0
    elif kind == "chirp":
        extra.update(f0=3.0, f1=max(8.0, sr * 0.47), amp=0.53 + (state % 5) * 0.31)
    elif kind == "ramp":
        extra.update(period=251 + state % 29, amp=0.79 + (state % 4) * 0.37)
    elif kind == "square":
        extra.update(period=7 + state % 23, amp=0.81 + (state % 3) * 0.55)
    elif kind == "impulse":
        extra["amp"] = 0.7 + (state % 4) * 0.6
    elif kind == "tone":
        extra.update(hz=max(3.0, min(sr * 0.47, 17.0 + state % max(1, sr // 3))), amp=0.2 + (state % 7) * 0.43)
    add(f"random_{index:02d}_{sr}_{pps}_{ch}", sr, pps, frames, ch, kind, **extra)

assert len(cases) == 128, len(cases)
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
    sr, frames, ch = case["sr"], case["frames"], case["ch"]
    seed = (0x13579BDF ^ (case_index * 0x9E3779B9)) & 0xFFFFFFFF
    payload = bytearray()
    rough_fine = max(1, sr // max(1, case["pps"]))
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
    duration = max(frames / sr, 1.0 / sr)
    for frame in range(frames):
        for channel in range(ch):
            kind = case["kind"]
            if kind == "silence":
                value = 0.0
            elif kind == "dc":
                value = case["value"]
            elif kind == "dc_lanes":
                value = case["value"] if channel % 2 == 0 else -case["value"]
            elif kind == "alt":
                value = case["amp"] if (frame + channel) % 2 == 0 else -case["amp"]
            elif kind == "tone":
                phase = 2.0 * math.pi * case["hz"] * frame / sr + channel * 0.173
                value = case["amp"] * math.sin(phase)
            elif kind == "exact_bin":
                hz = case["bin"] * sr / 256.0
                phase = 2.0 * math.pi * hz * frame / sr + channel * 0.131
                value = case["amp"] * math.sin(phase)
            elif kind == "chirp":
                t = frame / sr
                slope = (case["f1"] - case["f0"]) / duration
                phase = 2.0 * math.pi * (case["f0"] * t + 0.5 * slope * t * t) + channel * 0.097
                value = case["amp"] * math.sin(phase)
            elif kind == "square":
                period = max(2, int(case["period"]))
                high = ((frame + channel * 3) % period) < period // 2
                value = case["amp"] * (1.0 if high else -1.0)
            elif kind == "step":
                sign = -1.0 if frame < frames // 2 else 1.0
                value = case["amp"] * (-sign if channel % 2 else sign)
            elif kind == "ramp":
                period = max(2, int(case["period"]))
                phase = ((frame + channel * 19) % period) / (period - 1)
                value = case["amp"] * (2.0 * phase - 1.0)
            elif kind == "impulse":
                hit = frame in impulse_points or frame % 4093 == (channel * 257) % 4093
                value = (case.get("amp", 1.625) if (frame + channel) % 2 == 0 else -case.get("amp", 1.625)) if hit else 0.0
            elif kind == "noise":
                seed = (1664525 * seed + 1013904223 + channel) & 0xFFFFFFFF
                signed = (((seed >> 8) & 0xFFFFFF) - 0x800000) / float(0x800000)
                value = signed * case.get("amp", 1.0)
            elif kind == "lanes":
                seed = (1664525 * seed + 1013904223 + channel) & 0xFFFFFFFF
                frequencies = (17.0, 97.0, 997.0, 3000.0, 6000.0, 11000.0, 0.21 * sr, 0.43 * sr)
                hz = min(frequencies[channel % len(frequencies)], 0.49 * sr)
                amp = 0.18 + 0.33 * (channel % 6)
                tone = amp * math.sin(2.0 * math.pi * hz * frame / sr + channel * 0.271)
                dither = ((((seed >> 16) & 0xFFFF) - 32768) / 32768.0) * 1.0e-4
                value = tone + dither
            elif kind == "sparse":
                value = case["amp"] * math.sin(2.0 * math.pi * 997.0 * frame / sr) if channel == case["active"] else 0.0
            else:
                raise AssertionError(kind)
            payload += struct.pack("<f", f32(value))
    (root / f"{case['name']}.f32le").write_bytes(payload)
    write_float_wave(root / f"{case['name']}.wav", sr, ch, payload)

print(f"F32_SPECTROGRAM_STRESS_CASES={len(cases)}")
