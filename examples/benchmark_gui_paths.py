"""Benchmark `.reapeaks` GUI data-preparation paths.

This benchmark intentionally isolates the CPU side that a GUI controls:

1. full `ReaPeaks.open()` parsing/materialization;
2. decoded `SpectrogramView` u16 tile preparation;
3. index-only `GpuCacheView` and packed `-'g'` tile extraction.

The GLSL demo reports GPU command time separately with QOpenGLTimerQuery on the
actual display hardware. CI numbers are useful as a regression trend, not as a
claim about a user's GPU.
"""
from __future__ import annotations

import argparse
import array
import json
from pathlib import Path
import statistics
import tempfile
import time

import reapeaks


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return ordered[index]


def timed(callable_, iterations: int) -> dict[str, float]:
    samples = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        callable_()
        samples.append((time.perf_counter_ns() - start) / 1e6)
    return {
        "median_ms": statistics.median(samples),
        "p95_ms": percentile(samples, 0.95),
        "min_ms": min(samples),
        "max_ms": max(samples),
    }


def synthetic_pcm(seconds: float, sample_rate: int, channels: int) -> bytes:
    frames = max(1, int(seconds * sample_rate))
    samples = array.array("h")
    for frame in range(frames):
        left = ((frame * 97) % 65_535) - 32_768
        for channel in range(channels):
            value = left if channel == 0 else ((frame * (313 + channel * 17)) % 65_535) - 32_768
            samples.append(max(-32_768, min(32_767, value)))
    if samples.itemsize != 2:
        raise RuntimeError("unexpected native i16 size")
    import sys

    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def make_cache(path: Path, seconds: float, sample_rate: int, channels: int) -> None:
    pcm = synthetic_pcm(seconds, sample_rate, channels)
    divisions = reapeaks.default_divisions(sample_rate, 300)
    blob = reapeaks.generate_pcm16_reaper(
        pcm,
        sample_rate=sample_rate,
        channels=channels,
        divisions=divisions,
        mode="spectrogram",
        source_mtime_low32=0,
        source_size_low32=len(pcm),
    )
    path.write_bytes(bytes(blob))


def run(cache: Path, open_iterations: int, tile_iterations: int) -> dict[str, object]:
    open_full = timed(lambda: reapeaks.ReaPeaks.open(str(cache)), open_iterations)
    open_decoded_g = timed(lambda: reapeaks.SpectrogramView.open(str(cache)), open_iterations)
    open_packed = timed(lambda: reapeaks.GpuCacheView.open(str(cache)), open_iterations)

    decoded = reapeaks.SpectrogramView.open(str(cache))
    packed = reapeaks.GpuCacheView.open(str(cache))
    decoded_levels = decoded.levels()
    packed_levels = packed.levels("spectrogram")
    if not decoded_levels or not packed_levels:
        raise RuntimeError("benchmark fixture has no spectrogram layers")
    records = min(256, int(decoded_levels[0][1]), int(packed_levels[0][1]))
    if records <= 0:
        raise RuntimeError("benchmark fixture has an empty spectrogram layer")

    def decoded_tile():
        _first, width, height, raw = decoded.tile_u16le(0, 0, records)
        return width, height, len(bytes(raw))

    def packed_tile():
        _first, width, channels, bpc, raw = packed.records(
            "spectrogram", 0, 0, records
        )
        return width, channels, bpc, len(bytes(raw))

    decoded_shape = decoded_tile()
    packed_shape = packed_tile()
    decoded_bytes = decoded_shape[2]
    packed_bytes = packed_shape[3]
    if packed_bytes * 4 != decoded_bytes * 3:
        raise RuntimeError(
            f"expected packed spectrogram transfer to be 75% of u16 expansion; "
            f"packed={packed_bytes} decoded={decoded_bytes}"
        )

    decoded_tile_time = timed(decoded_tile, tile_iterations)
    packed_tile_time = timed(packed_tile, tile_iterations)

    full_median = float(open_full["median_ms"])
    packed_median = float(open_packed["median_ms"])
    decoded_tile_median = float(decoded_tile_time["median_ms"])
    packed_tile_median = float(packed_tile_time["median_ms"])
    return {
        "cache_bytes": cache.stat().st_size,
        "channels": packed.channels,
        "sample_rate": packed.sample_rate,
        "spectrogram_records_benchmarked": records,
        "open": {
            "full_reapeaks": open_full,
            "decoded_spectrogram_view": open_decoded_g,
            "packed_gpu_view": open_packed,
            "packed_vs_full_speedup": full_median / max(1e-12, packed_median),
        },
        "spectrogram_tile": {
            "decoded_u16": decoded_tile_time,
            "packed_12bit": packed_tile_time,
            "decoded_transfer_bytes": decoded_bytes,
            "packed_transfer_bytes": packed_bytes,
            "packed_transfer_ratio": packed_bytes / decoded_bytes,
            "packed_vs_decoded_speedup": decoded_tile_median
            / max(1e-12, packed_tile_median),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", nargs="?", type=Path)
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--open-iterations", type=int, default=10)
    parser.add_argument("--tile-iterations", type=int, default=100)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.open_iterations <= 0 or args.tile_iterations <= 0:
        raise SystemExit("iteration counts must be positive")
    if args.cache is not None:
        result = run(args.cache.resolve(), args.open_iterations, args.tile_iterations)
    else:
        with tempfile.TemporaryDirectory(prefix="libreapeaks-gui-bench-") as directory:
            cache = Path(directory) / "fixture.reapeaks"
            make_cache(cache, args.seconds, args.sample_rate, args.channels)
            result = run(cache, args.open_iterations, args.tile_iterations)
    print("GUI_PATH_BENCH " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
