#!/usr/bin/env python3
from pathlib import Path
import json
import math
import struct
import sys
import wave

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/spectrogram-stress/media")
root.mkdir(parents=True, exist_ok=True)
cases = []


def add(name, sr, pps, frames, ch, kind, **extra):
    case = dict(name=name, sr=int(sr), pps=int(pps), frames=int(frames), ch=int(ch), kind=kind)
    case.update(extra)
    cases.append(case)


# fine=floor(sr/pps) crosses the recovered 256-sample placement branch here.
for sr in (76_799, 76_800, 76_801):
    add(f"rate_threshold_{sr}", sr, 300, sr + 17, 1, "tone", hz=997.0, amp=0.43)
    add(f"rate_threshold_{sr}_3ch", sr, 300, sr * 2 + 113, 3, "lanes")

# Preference-rate matrix: same source, radically different fine divisions.
for sr in (32_000, 44_100, 48_000, 96_000, 192_000):
    for pps in (100, 150, 300, 500, 1_000):
        ch = 1 + ((sr // 1_000 + pps) % 4)
        add(f"prefs_{sr}_{pps}", sr, pps, sr + 131, ch, "lanes")

# More direct 255/256/257-ish fine-division probes.
for sr, pps in (
    (44_100, 171), (44_100, 172), (44_100, 173),
    (48_000, 187), (48_000, 188), (48_000, 189),
    (96_000, 374), (96_000, 375), (96_000, 376),
):
    add(f"fine256_{sr}_{pps}", sr, pps, sr + 73, 2, "tone", hz=997.0, amp=0.47)

# Source-rate sweep from telephony-ish rates through 192 kHz.
for index, sr in enumerate((8_000, 11_025, 16_000, 22_050, 22_051, 24_000, 32_000, 44_100, 48_000, 88_200, 96_000, 176_400, 192_000)):
    add(f"rate_sweep_{sr}", sr, 300, sr + 37 + index, 3, "chirp", f0=7.0, f1=max(20.0, sr * 0.49), amp=0.61)

# Numeric/signal extremes.
add("ext_silence", 48_000, 300, 48_017, 1, "silence")
add("ext_dc_pos", 48_000, 300, 48_019, 1, "dc", value=32767)
add("ext_dc_neg", 48_000, 300, 48_023, 1, "dc", value=-32768)
add("ext_nyquist", 48_000, 300, 48_029, 1, "alt")
add("ext_lsb_alt", 48_000, 300, 48_031, 1, "lsb_alt")
add("ext_square", 48_000, 300, 48_037, 1, "square", period=17, amp=0.93)
add("ext_step", 48_000, 300, 48_041, 1, "step", amp=0.91)
add("ext_ramp", 48_000, 300, 48_043, 1, "ramp", period=257, amp=0.97)
add("ext_chirp", 48_000, 300, 48_047, 1, "chirp", f0=1.0, f1=23_999.0, amp=0.72)
add("ext_noise_full", 48_000, 300, 48_053, 1, "noise", atten=1)
add("ext_noise_quiet", 48_000, 300, 48_059, 1, "noise", atten=257)
add("ext_impulse_train", 48_000, 300, 96_061, 2, "impulse")
for bin_index in (1, 2, 63, 127):
    add(f"ext_exact_bin{bin_index}", 48_000, 300, 48_067 + bin_index, 1, "exact_bin", bin=bin_index, amp=0.77)
add("ext_offbin_17hz", 48_000, 300, 48_079, 1, "tone", hz=17.0, amp=0.83)
for name, amp in (("1lsb", 1.0 / 32768.0), ("2lsb", 2.0 / 32768.0), ("tiny", 0.001), ("near_full", 32767.0 / 32768.0)):
    add(f"ext_amp_{name}", 48_000, 300, 48_083 + len(cases) % 17, 1, "tone", hz=6000.0, amp=amp)

# Non-power-of-two and wider channel layouts.
for ch in range(1, 9):
    add(f"channels_{ch}", 96_000, 375, 30_011 + ch * 17, ch, "lanes")
add("channels_sparse_8", 48_000, 300, 72_013, 8, "sparse", active=6)

# Scheduler edges for the 96 kHz / 300 path.
for frames in (319, 320, 321, 5_119, 5_120, 5_121, 91_519, 91_520, 91_521, 288_319, 288_320, 288_321):
    add(f"boundary96_{frames}", 96_000, 300, frames, 1, "tone", hz=997.0, amp=0.43)

# Many coarse frames plus wide interleave/state-bleed pressure.
add("long_48k_8ch", 48_000, 300, 480_037, 8, "lanes")
add("long_96k_7ch", 96_000, 300, 672_061, 7, "lanes")
add("long_192k_5ch", 192_000, 300, 806_417, 5, "lanes")

# Reproducible pseudo-random matrix.
rates = (8_000, 11_025, 16_000, 22_050, 22_051, 24_000, 32_000, 44_100, 48_000, 76_799, 76_800, 76_801, 88_200, 96_000, 176_400, 192_000)
preferences = (100, 149, 150, 171, 172, 173, 187, 188, 200, 299, 300, 301, 374, 375, 376, 499, 500, 501, 1_000)
kinds = ("lanes", "noise", "chirp", "impulse", "ramp", "square")
state = 0xA5C31E27
for index in range(24):
    state = (1664525 * state + 1013904223) & 0xffffffff
    sr = rates[state % len(rates)]
    state = ((state << 9) | (state >> 23)) & 0xffffffff
    pps = preferences[state % len(preferences)]
    ch = 1 + ((state >> 5) % 8)
    frames = 257 + ((state >> 8) % max(513, int(sr * 1.6)))
    kind = kinds[(state >> 17) % len(kinds)]
    extra = {}
    if kind == "noise":
        extra["atten"] = 1 + state % 31
    elif kind == "chirp":
        extra.update(f0=3.0, f1=max(8.0, sr * 0.47), amp=0.53)
    elif kind == "ramp":
        extra.update(period=251 + state % 29, amp=0.79)
    elif kind == "square":
        extra.update(period=7 + state % 23, amp=0.81)
    add(f"random_{index:02d}_{sr}_{pps}_{ch}", sr, pps, frames, ch, kind, **extra)

assert len(cases) >= 100, len(cases)
(root / "cases.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")


def q16(value):
    return max(-32768, min(32767, int(round(value))))


for case_index, case in enumerate(cases):
    sr, frames, ch = case["sr"], case["frames"], case["ch"]
    seed = (0x13579BDF ^ (case_index * 0x9E3779B9)) & 0xffffffff
    payload = bytearray()
    rough_fine = max(1, sr // max(1, case["pps"]))
    impulse_points = {0, 1, 2, 63, 64, 127, 128, 255, 256, 257, max(0, rough_fine - 1), rough_fine, rough_fine + 1, max(0, frames - 3), max(0, frames - 2), max(0, frames - 1)}
    duration = max(frames / sr, 1.0 / sr)
    for frame in range(frames):
        values = []
        for channel in range(ch):
            kind = case["kind"]
            if kind == "silence":
                value = 0
            elif kind == "dc":
                value = case["value"]
            elif kind == "alt":
                value = 32767 if (frame + channel) % 2 == 0 else -32768
            elif kind == "lsb_alt":
                value = 1 if (frame + channel) % 2 == 0 else -1
            elif kind == "tone":
                phase = 2.0 * math.pi * case["hz"] * frame / sr + channel * 0.173
                value = q16(case["amp"] * 32767.0 * math.sin(phase))
            elif kind == "exact_bin":
                hz = case["bin"] * sr / 256.0
                phase = 2.0 * math.pi * hz * frame / sr + channel * 0.131
                value = q16(case["amp"] * 32767.0 * math.sin(phase))
            elif kind == "chirp":
                t = frame / sr
                slope = (case["f1"] - case["f0"]) / duration
                phase = 2.0 * math.pi * (case["f0"] * t + 0.5 * slope * t * t) + channel * 0.097
                value = q16(case["amp"] * 32767.0 * math.sin(phase))
            elif kind == "square":
                period = max(2, int(case["period"]))
                high = ((frame + channel * 3) % period) < period // 2
                value = q16(case["amp"] * 32767.0 * (1.0 if high else -1.0))
            elif kind == "step":
                sign = -1.0 if frame < frames // 2 else 1.0
                value = q16(case["amp"] * 32767.0 * (-sign if channel % 2 else sign))
            elif kind == "ramp":
                period = max(2, int(case["period"]))
                phase = ((frame + channel * 19) % period) / (period - 1)
                value = q16(case["amp"] * 32767.0 * (2.0 * phase - 1.0))
            elif kind == "impulse":
                hit = frame in impulse_points or frame % 4093 == (channel * 257) % 4093
                value = (32767 if (frame + channel) % 2 == 0 else -32768) if hit else 0
            elif kind == "noise":
                seed = (1664525 * seed + 1013904223 + channel) & 0xffffffff
                signed = ((seed >> 16) & 0xffff) - 32768
                value = int(signed // max(1, int(case.get("atten", 1))))
            elif kind == "lanes":
                seed = (1664525 * seed + 1013904223 + channel) & 0xffffffff
                frequencies = (17.0, 97.0, 997.0, 3000.0, 6000.0, 11000.0, 0.21 * sr, 0.43 * sr)
                hz = min(frequencies[channel % len(frequencies)], 0.49 * sr)
                amp = 0.18 + 0.055 * (channel % 6)
                tone = amp * 32767.0 * math.sin(2.0 * math.pi * hz * frame / sr + channel * 0.271)
                dither = (((seed >> 16) & 0xffff) - 32768) * 0.027
                value = q16(tone + dither)
            elif kind == "sparse":
                value = q16(0.71 * 32767.0 * math.sin(2.0 * math.pi * 997.0 * frame / sr)) if channel == case["active"] else 0
            else:
                raise AssertionError(kind)
            values.append(value)
        payload += struct.pack("<" + "h" * ch, *values)
    (root / f"{case['name']}.s16le").write_bytes(payload)
    with wave.open(str(root / f"{case['name']}.wav"), "wb") as out:
        out.setnchannels(ch)
        out.setsampwidth(2)
        out.setframerate(sr)
        out.writeframes(payload)

print(f"SPECTROGRAM_STRESS_CASES={len(cases)}")
