#!/usr/bin/env python3
"""Generate the broad spectral stress WAV corpus from its TSV manifest.

The synthesis intentionally mirrors tests/spectral_stress_oracle.rs. Keeping
media generation next to the live REAPER harness makes the oracle reproducible
instead of relying on transient WAV files.
"""

from __future__ import annotations

import argparse
import csv
import struct
from pathlib import Path

U32 = 0xFFFF_FFFF


def u32(x: int) -> int:
    return x & U32


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", float(x)))[0]


def trunc_div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError
    q = abs(a) // abs(b)
    return -q if (a < 0) ^ (b < 0) else q


def clamp16(value: int) -> int:
    return max(-32768, min(32767, value))


def ch_seed(seed: int, channel: int) -> int:
    return u32(seed ^ u32((channel + 1) * 0x9E37_79B9))


def parse_u32_auto(text: str) -> int:
    return int(text, 0) & U32


def pcm_i16(channels: int, frames: int, spec: list[str]) -> list[int]:
    kind = spec[0]
    if kind == "silence":
        return [0] * (frames * channels)

    out: list[int] = []
    if kind == "alt":
        amp = int(spec[1])
        for i in range(frames):
            for channel in range(channels):
                out.append(amp if ((i + channel) & 1) == 1 else -amp)
        return out

    if kind == "noise":
        seed = int(spec[1])
        shift = int(spec[2])
        states = [ch_seed(seed, c) for c in range(channels)]
        for _ in range(frames):
            for c in range(channels):
                states[c] = u32(states[c] * 1_664_525 + 1_013_904_223)
                value = ((states[c] >> 16) & 0xFFFF) - 32768
                if shift:
                    value = trunc_div(value, 1 << shift)
                out.append(clamp16(value))
        return out

    if kind == "square":
        period, amp, channel_offset = int(spec[1]), int(spec[2]), int(spec[3])
        half = max(period // 2, 1)
        for i in range(frames):
            for channel in range(channels):
                phase = (i + channel * channel_offset) % period
                out.append(amp if phase < half else -amp)
        return out

    if kind == "triangle":
        period, amp, channel_offset = int(spec[1]), int(spec[2]), int(spec[3])
        half = max(period // 2, 1)
        tail = max(period - half, 1)
        for i in range(frames):
            for channel in range(channels):
                phase = (i + channel * channel_offset) % period
                if phase < half:
                    value = -amp + trunc_div(2 * amp * phase, half)
                else:
                    value = amp - trunc_div(2 * amp * (phase - half), tail)
                out.append(clamp16(value))
        return out

    if kind == "two_square":
        period1, amp1 = int(spec[1]), int(spec[2])
        period2, amp2 = int(spec[3]), int(spec[4])
        channel_offset = int(spec[5])
        half1, half2 = max(period1 // 2, 1), max(period2 // 2, 1)
        for i in range(frames):
            for channel in range(channels):
                phase = i + channel * channel_offset
                one = amp1 if phase % period1 < half1 else -amp1
                two = amp2 if phase % period2 < half2 else -amp2
                out.append(clamp16(one + two))
        return out

    if kind == "dds_square":
        inc0, inc1 = parse_u32_auto(spec[1]), parse_u32_auto(spec[2])
        amp, seed = int(spec[3]), parse_u32_auto(spec[4])
        phases = [u32(seed + u32(c * 0x1357_9BDF)) for c in range(channels)]
        denominator = max(frames - 1, 1)
        delta = inc1 - inc0
        for i in range(frames):
            increment = inc0 + trunc_div(delta * i, denominator)
            for channel in range(channels):
                phases[channel] = u32(
                    phases[channel] + u32(increment) + u32(channel * 97)
                )
                out.append(amp if phases[channel] & 0x8000_0000 else -amp)
        return out

    if kind == "dc":
        base, step = int(spec[1]), int(spec[2])
        for _ in range(frames):
            for channel in range(channels):
                value = base + channel * step
                if channel & 1:
                    value = -value
                out.append(clamp16(value))
        return out

    if kind == "impulse":
        position, stride, amp = int(spec[1]), int(spec[2]), int(spec[3])
        out = [0] * (frames * channels)
        for channel in range(channels):
            frame = position + channel * stride
            if frame < frames:
                out[frame * channels + channel] = clamp16(amp - channel * 777)
        return out

    if kind == "impulse_train":
        period, amp, channel_offset = int(spec[1]), int(spec[2]), int(spec[3])
        out = [0] * (frames * channels)
        for channel in range(channels):
            frame = channel * channel_offset
            index = 0
            while frame < frames:
                out[frame * channels + channel] = amp if (index & 1) == 0 else -amp
                index += 1
                frame += period
        return out

    if kind == "saw":
        period, amp, channel_offset = int(spec[1]), int(spec[2]), int(spec[3])
        denominator = max(period - 1, 1)
        for i in range(frames):
            for channel in range(channels):
                phase = (i + channel * channel_offset) % period
                out.append(clamp16(-amp + trunc_div(2 * amp * phase, denominator)))
        return out

    raise ValueError(f"unknown i16 stress signal {kind}")


def pcm_f32(channels: int, frames: int, spec: list[str]) -> list[float]:
    if spec[0] != "f32_noise":
        raise ValueError(f"unknown f32 stress signal {spec[0]}")
    seed = int(spec[1])
    gain = f32(float(spec[2]))
    states = [ch_seed(seed, c) for c in range(channels)]
    out: list[float] = []
    denom = f32(32768.0)
    for _ in range(frames):
        for c in range(channels):
            states[c] = u32(states[c] * 1_664_525 + 1_013_904_223)
            value = ((states[c] >> 16) & 0xFFFF) - 32768
            sample = f32(f32(f32(value) / denom) * gain)
            out.append(sample)
    return out


def write_wav_i16(path: Path, sample_rate: int, channels: int, samples: list[int]) -> None:
    data = struct.pack("<" + "h" * len(samples), *samples)
    write_riff_wav(path, sample_rate, channels, 1, 16, data)


def write_wav_f32(path: Path, sample_rate: int, channels: int, samples: list[float]) -> None:
    data = struct.pack("<" + "f" * len(samples), *samples)
    write_riff_wav(path, sample_rate, channels, 3, 32, data)


def write_riff_wav(
    path: Path,
    sample_rate: int,
    channels: int,
    format_tag: int,
    bits_per_sample: int,
    data: bytes,
) -> None:
    block_align = channels * bits_per_sample // 8
    byte_rate = sample_rate * block_align
    fmt = struct.pack(
        "<HHIIHH",
        format_tag,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    )
    riff_size = 4 + (8 + len(fmt)) + (8 + len(data))
    with path.open("wb") as fh:
        fh.write(b"RIFF")
        fh.write(struct.pack("<I", riff_size))
        fh.write(b"WAVEfmt ")
        fh.write(struct.pack("<I", len(fmt)))
        fh.write(fmt)
        fh.write(b"data")
        fh.write(struct.pack("<I", len(data)))
        fh.write(data)


def iter_manifest(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        for raw in fh:
            if not raw.strip() or raw.startswith("#"):
                continue
            cols = raw.rstrip("\n").split("\t")
            if len(cols) != 9:
                raise ValueError(f"expected 9 TSV fields: {raw!r}")
            yield cols


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tests/data/spectral_stress_oracle.tsv"),
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for cols in iter_manifest(args.manifest):
        name, sr, channels, sample_type, frames, _, _, _, spec_text = cols
        sample_rate = int(sr)
        nch = int(channels)
        nframes = int(frames)
        spec = spec_text.split(",")
        path = args.out_dir / f"{name}.wav"
        if sample_type == "i16":
            write_wav_i16(path, sample_rate, nch, pcm_i16(nch, nframes, spec))
        elif sample_type == "f32":
            write_wav_f32(path, sample_rate, nch, pcm_f32(nch, nframes, spec))
        else:
            raise ValueError(f"unsupported sample type {sample_type}")
        print(path)
        count += 1
    print(f"generated {count} stress media files")


if __name__ == "__main__":
    main()
