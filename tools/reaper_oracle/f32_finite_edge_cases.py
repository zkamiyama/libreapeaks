#!/usr/bin/env python3
from pathlib import Path
import json
import struct
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/f32-finite-edge/media")
root.mkdir(parents=True, exist_ok=True)
cases = []


def add(name, bits, frames=48_017, ch=1, sr=48_000, pps=300, pattern="constant"):
    cases.append(
        dict(
            name=name,
            sr=sr,
            pps=pps,
            frames=frames,
            ch=ch,
            pattern=pattern,
            bits=[int(x) for x in bits],
        )
    )


# IEEE-f32 finite classification endpoints and sign variants.
add("finite_pos_zero", [0x00000000])
add("finite_neg_zero", [0x80000000])
add("finite_min_subnormal", [0x00000001, 0x80000001], ch=2, pattern="lanes")
add("finite_max_subnormal", [0x007FFFFF, 0x807FFFFF], ch=2, pattern="lanes")
add("finite_min_normal", [0x00800000, 0x80800000], ch=2, pattern="lanes")
add("finite_max_value", [0x7F7FFFFF, 0xFF7FFFFF], ch=2, pattern="lanes")

# Exact waveform decision boundaries around unity and saturation.
add("finite_unity_neighbors", [0x3F7FFFFF, 0x3F800000, 0x3F800001], ch=3, pattern="lanes")
add("finite_neg_unity_neighbors", [0xBF7FFFFF, 0xBF800000, 0xBF800001], ch=3, pattern="lanes")
add("finite_256_neighbors", [0x437FFFFF, 0x43800000, 0x43800001], ch=3, pattern="lanes")
add("finite_neg_256_neighbors", [0xC37FFFFF, 0xC3800000, 0xC3800001], ch=3, pattern="lanes")

# Exact representable .5 ties recovered by the exhaustive RPKL oracle.
add("finite_first_half_tie", [0x38800000, 0xB8800000], ch=2, pattern="lanes")
add("finite_observed_half_tie", [0x3F23AC00, 0xBF23AC00], ch=2, pattern="lanes")

# Powers/exponents spanning the normal finite range, with both signs.
exponent_bits = []
for exponent in (1, 2, 8, 16, 32, 64, 96, 120, 126, 127, 128, 160, 192, 224, 253, 254):
    word = exponent << 23
    exponent_bits.extend((word, word | 0x80000000))
add("finite_exponent_ladder", exponent_bits, frames=96_019, ch=4, pattern="cycle")

# Deterministic raw-bit sequence. Mask out exponent=255 so every word is finite.
state = 0xC001D00D
random_bits = []
for _ in range(4096):
    state = (1664525 * state + 1013904223) & 0xFFFFFFFF
    word = state
    if (word & 0x7F800000) == 0x7F800000:
        word ^= 0x00800000
    random_bits.append(word)
add("finite_raw_bit_sequence", random_bits, frames=131_071, ch=2, pattern="cycle")

# Sweep all 8,192 positive/negative exact-half-tie classes in the linear RPKL
# region. The tie lower codes are 1 mod 3 through the first large block; build
# them directly from exact f32 bit patterns by scanning the finite linear range.
half_ties = []
for bits in range(0x00000001, 0x3F800001):
    # Avoid converting the entire range: exact RPKL half ties are representable
    # multiples where f32 * 24576 is n+0.5. They occur on a sparse power-of-two
    # lattice; sample by reconstructing from odd half-units instead.
    break
for lower_code in range(24_576):
    target = (lower_code + 0.5) / 24_576.0
    packed = struct.pack("<f", target)
    value = struct.unpack("<f", packed)[0]
    if value * 24_576.0 == lower_code + 0.5:
        word = struct.unpack("<I", packed)[0]
        half_ties.extend((word, word | 0x80000000))
assert len(half_ties) == 16_384, len(half_ties)
add("finite_all_linear_half_ties", half_ties, frames=262_144, ch=2, pattern="cycle")

(root / "cases.json").write_text(json.dumps(cases, indent=2) + "\n", encoding="utf-8")


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


for case in cases:
    frames = int(case["frames"])
    channels = int(case["ch"])
    words = case["bits"]
    pattern = case["pattern"]
    payload = bytearray()
    for frame in range(frames):
        for channel in range(channels):
            if pattern == "constant":
                word = words[0]
            elif pattern == "lanes":
                word = words[channel % len(words)]
            elif pattern == "cycle":
                word = words[(frame * channels + channel) % len(words)]
            else:
                raise AssertionError(pattern)
            assert (word & 0x7F800000) != 0x7F800000, hex(word)
            payload += struct.pack("<I", word)
    (root / f"{case['name']}.f32le").write_bytes(payload)
    write_float_wave(root / f"{case['name']}.wav", int(case["sr"]), channels, payload)

print(f"F32_FINITE_EDGE_CASES={len(cases)} half_tie_words={len(half_ties)}")
