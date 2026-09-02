#!/usr/bin/env python3
"""Probe how pinned REAPER treats extended and malformed .reapeaks files.

Each mutation starts from one fresh REAPER-generated cache and is tested in a
new REAPER process with the same source and peak preferences. The key signal is
PCM_Source_BuildPeaks(src, 0): zero means REAPER accepts the existing cache,
nonzero means REAPER requests rebuilding it. Inputs and outputs are hashed so
accepted extensions can also be checked for byte-for-byte preservation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
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
TOKEN_SPECTRAL = -ord("s")
TOKEN_SPECTROGRAM = -ord("g")
TOKEN_LOUDNESS = -ord("r")
TOKEN_LOUDNESS_LEGACY = -ord("l")


@dataclass(frozen=True)
class Layer:
    token: int
    count: int
    payload: bytes


@dataclass(frozen=True)
class Layout:
    fixed_header: bytes
    channels: int
    magic: bytes
    layers: tuple[Layer, ...]


def make_media(wav_path: Path) -> None:
    values: list[int] = []
    for frame in range(FRAMES):
        sample = round(24_000 * math.sin(2 * math.pi * 997 * frame / SAMPLE_RATE))
        if frame == 0:
            sample = -32768
        elif frame == 1:
            sample = 32767
        values.append(sample)
    raw = struct.pack("<" + "h" * len(values), *values)
    with wave.open(str(wav_path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(raw)
    fixed_ns = FIXED_MTIME * 1_000_000_000
    os.utime(wav_path, ns=(fixed_ns, fixed_ns))


def payload_size(magic: bytes, channels: int, token: int, count: int) -> int:
    if token > 0:
        bytes_per_channel_peak = 2 if magic == b"RPKM" else 4
        return count * channels * bytes_per_channel_peak
    if token == TOKEN_SPECTRAL:
        return count * channels * 4
    if token == TOKEN_SPECTROGRAM:
        return count * channels * 192
    if token == TOKEN_LOUDNESS:
        return count * channels * 8
    if token == TOKEN_LOUDNESS_LEGACY:
        raise ValueError("legacy loudness size is deliberately not inferred")
    raise ValueError(f"unknown layer token {token}")


def parse_layout(blob: bytes) -> Layout:
    if len(blob) < 18:
        raise ValueError("truncated header")
    magic = blob[:4]
    channels = blob[4]
    layer_count = blob[5]
    table_end = 18 + layer_count * 8
    if len(blob) < table_end:
        raise ValueError("truncated layer table")
    headers = [
        struct.unpack_from("<iI", blob, 18 + index * 8)
        for index in range(layer_count)
    ]
    pos = table_end
    layers: list[Layer] = []
    for token, count in headers:
        size = payload_size(magic, channels, token, count)
        end = pos + size
        if end > len(blob):
            raise ValueError(f"truncated payload for token {token}")
        layers.append(Layer(token, count, blob[pos:end]))
        pos = end
    if pos != len(blob):
        raise ValueError(f"baseline contains {len(blob) - pos} trailing bytes")
    return Layout(blob[:18], channels, magic, tuple(layers))


def assemble(layout: Layout, layers: list[Layer], trailing: bytes = b"") -> bytes:
    if len(layers) > 255:
        raise ValueError("too many layers")
    fixed = bytearray(layout.fixed_header)
    fixed[5] = len(layers)
    out = bytearray(fixed)
    for layer in layers:
        out += struct.pack("<iI", layer.token, layer.count)
    for layer in layers:
        out += layer.payload
    out += trailing
    return bytes(out)


def patch_u8(blob: bytes, offset: int, value: int) -> bytes:
    out = bytearray(blob)
    out[offset] = value & 0xFF
    return bytes(out)


def patch_u32(blob: bytes, offset: int, value: int) -> bytes:
    out = bytearray(blob)
    struct.pack_into("<I", out, offset, value & 0xFFFF_FFFF)
    return bytes(out)


def patch_i32(blob: bytes, offset: int, value: int) -> bytes:
    out = bytearray(blob)
    struct.pack_into("<i", out, offset, value)
    return bytes(out)


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def status_value(status: str, prefix: str) -> str:
    return next(
        (line[len(prefix) :] for line in status.splitlines() if line.startswith(prefix)),
        "",
    )


def write_config(path: Path) -> None:
    path.write_text(
        "[REAPER]\n"
        f"peakcachegenmode={PEAKCACHEGENMODE}\n"
        f"peakcachegenrs={PEAK_RATE}\n"
        f"showpeaks={SHOWPEAKS}\n",
        encoding="utf-8",
    )


def run_case(
    *,
    reaper: Path,
    source: Path,
    peak_path: Path,
    baseline: bytes,
    name: str,
    description: str,
    mutated: bytes,
    display: str,
    results: Path,
    timeout: int,
) -> dict[str, object]:
    case_dir = results / "cases" / name
    case_dir.mkdir(parents=True, exist_ok=True)
    input_path = case_dir / "input.reapeaks"
    output_path = case_dir / "output.reapeaks"
    status_copy = case_dir / "status.txt"
    log_path = case_dir / "reaper.log"
    input_path.write_bytes(mutated)
    peak_path.parent.mkdir(parents=True, exist_ok=True)
    peak_path.write_bytes(mutated)
    before_stat = peak_path.stat()

    cfg_dir = Path(tempfile.mkdtemp(prefix=f"reapeaks-extension-{name}-"))
    config = cfg_dir / "reaper.ini"
    write_config(config)
    status_path = cfg_dir / "result.txt"
    env = os.environ.copy()
    env.update(
        DISPLAY=display,
        REAPEAKS_MEDIA=str(source.resolve()),
        REAPEAKS_RESULT=str(status_path),
    )

    timed_out = False
    returncode: int | None = None
    try:
        with log_path.open("wb") as log:
            completed = subprocess.run(
                [
                    str(reaper),
                    "-newinst",
                    "-cfgfile",
                    str(config),
                    "-new",
                    "-nosplash",
                    str(HERE / "build_one.lua"),
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout,
            )
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True

    status = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
    status_copy.write_text(status, encoding="utf-8")
    after_exists = peak_path.exists()
    after = peak_path.read_bytes() if after_exists else b""
    if after_exists:
        output_path.write_bytes(after)
    after_stat = peak_path.stat() if after_exists else None
    begin_text = status_value(status, "BEGIN=")
    begin = int(begin_text) if begin_text else None

    if timed_out:
        classification = "timeout"
    elif returncode != 0 or begin is None:
        classification = "process-or-script-error"
    elif begin == 0 and after == mutated:
        classification = "accepted-preserved"
    elif begin == 0:
        classification = "accepted-but-modified"
    elif after == baseline:
        classification = "rebuild-to-baseline"
    else:
        classification = "rebuild-other"

    return {
        "name": name,
        "description": description,
        "classification": classification,
        "begin": begin,
        "reuse": begin == 0 if begin is not None else None,
        "returncode": returncode,
        "timed_out": timed_out,
        "status": status.strip(),
        "input_size": len(mutated),
        "output_size": len(after) if after_exists else None,
        "input_sha256": sha256(mutated),
        "output_sha256": sha256(after) if after_exists else None,
        "baseline_sha256": sha256(baseline),
        "output_equals_input": after_exists and after == mutated,
        "output_equals_baseline": after_exists and after == baseline,
        "peak_mtime_unchanged": bool(
            after_stat is not None and after_stat.st_mtime_ns == before_stat.st_mtime_ns
        ),
    }


def extension_blob() -> bytes:
    payload = json.dumps(
        {
            "schema": "libreapeaks.experimental.timeline.v1",
            "tempo": [
                {"frame": 0, "bpm": 120.0},
                {"frame": 48_000, "bpm": 128.0},
            ],
            "chords": [
                {"start_frame": 0, "end_frame": 48_000, "name": "Cmaj7"},
                {"start_frame": 48_000, "end_frame": 96_000, "name": "Am7"},
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return b"RPKX" + struct.pack("<II", 1, len(payload)) + payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("reaper", type=Path)
    parser.add_argument("--display", default=":96")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    root = args.root.resolve()
    results = root / "results"
    media = root / "media"
    results.mkdir(parents=True, exist_ok=True)
    media.mkdir(parents=True, exist_ok=True)
    source = media / "extension-probe.wav"
    make_media(source)

    oracle = run_one_source(
        reaper=args.reaper.resolve(),
        source=source,
        probe=HERE / "build_one.lua",
        results_dir=results,
        label="baseline",
        display=args.display,
        peakcachegenrs=PEAK_RATE,
        showpeaks=SHOWPEAKS,
        peakcachegenmode=PEAKCACHEGENMODE,
        expected_source_type="WAVE",
        timeout=120,
    )
    peak_path = oracle.peak_path.resolve()
    baseline = peak_path.read_bytes()
    (results / "baseline.reapeaks").write_bytes(baseline)
    layout = parse_layout(baseline)
    if layout.magic not in (b"RPKN", b"RPKL"):
        raise RuntimeError(f"unexpected baseline magic {layout.magic!r}")

    tokens = [layer.token for layer in layout.layers]
    positive_count = sum(token > 0 for token in tokens)
    if positive_count == 0:
        raise RuntimeError("baseline has no positive waveform layers")
    first_s = next((i for i, token in enumerate(tokens) if token == TOKEN_SPECTRAL), None)
    first_r = next((i for i, token in enumerate(tokens) if token == TOKEN_LOUDNESS), None)

    cases: list[tuple[str, str, bytes]] = []
    cases.append(("control", "Unmodified REAPER cache.", baseline))

    # EOF-only extensions: no header or layer offsets are touched.
    cases.extend(
        [
            ("trailing-zero-1", "Append one zero byte after the final layer.", baseline + b"\0"),
            ("trailing-ff-1", "Append one nonzero byte after the final layer.", baseline + b"\xff"),
            ("trailing-zero-16", "Append 16 zero bytes after the final layer.", baseline + bytes(16)),
            (
                "trailing-rpkx-empty",
                "Append a 12-byte experimental RPKX header with zero payload.",
                baseline + b"RPKX" + struct.pack("<II", 1, 0),
            ),
            (
                "trailing-rpkx-timeline",
                "Append an RPKX length-delimited JSON tempo/chord timeline.",
                baseline + extension_blob(),
            ),
            (
                "trailing-rpkx-timeline-repeat",
                "Repeat the same RPKX timeline mutation in an independent fresh REAPER process.",
                baseline + extension_blob(),
            ),
            (
                "trailing-deterministic-4k",
                "Append 4096 deterministic arbitrary bytes.",
                baseline + bytes((index * 73 + 19) & 0xFF for index in range(4096)),
            ),
        ]
    )

    # Unknown/invalid layer tokens. count=0 isolates token acceptance from payload sizing.
    end_layers = list(layout.layers)
    cases.append(
        (
            "unknown-neg-x-count0",
            "Append unknown negative token -'x' with count 0.",
            assemble(layout, end_layers + [Layer(-ord("x"), 0, b"")]),
        )
    )
    cases.append(
        (
            "unknown-neg-one-count0",
            "Append unknown negative token -1 with count 0.",
            assemble(layout, end_layers + [Layer(-1, 0, b"")]),
        )
    )
    cases.append(
        (
            "zero-token-count0",
            "Append division/token 0 with count 0.",
            assemble(layout, end_layers + [Layer(0, 0, b"")]),
        )
    )
    cases.append(
        (
            "unknown-neg-x-count1-payload4",
            "Append unknown -'x' token with count 1 and four arbitrary payload bytes.",
            assemble(layout, end_layers + [Layer(-ord("x"), 1, b"META")]),
        )
    )

    # Structurally self-consistent but non-native positive layers.
    positive_insert = positive_count
    extra_division = max(layer.token for layer in layout.layers if layer.token > 0) * 2
    layers = list(layout.layers)
    layers.insert(positive_insert, Layer(extra_division, 0, b""))
    cases.append(
        (
            "extra-positive-count0",
            "Insert an additional coarser positive waveform layer with count 0 before special layers.",
            assemble(layout, layers),
        )
    )
    layers = list(layout.layers)
    layers.insert(positive_insert, Layer(extra_division, 1, struct.pack("<hh", 0, 0)))
    cases.append(
        (
            "extra-positive-count1",
            "Insert an additional coarser positive waveform layer with one zero peak.",
            assemble(layout, layers),
        )
    )
    layers = list(layout.layers)
    layers.insert(positive_insert, Layer(1, 1, struct.pack("<hh", 0, 0)))
    cases.append(
        (
            "extra-positive-nonmonotonic",
            "Insert positive division=1 after native positive levels, violating increasing division order.",
            assemble(layout, layers),
        )
    )
    cases.append(
        (
            "positive-after-special",
            "Append a positive waveform layer after spectral/loudness layers.",
            assemble(layout, end_layers + [Layer(extra_division, 0, b"")]),
        )
    )

    # Known special tokens with non-native shape/count placement.
    cases.append(
        (
            "extra-spectral-count0",
            "Append a known -'s' layer with count 0 beyond the native spectral set.",
            assemble(layout, end_layers + [Layer(TOKEN_SPECTRAL, 0, b"")]),
        )
    )
    cases.append(
        (
            "extra-loudness-count0",
            "Append a known -'r' layer with count 0 beyond the native loudness set.",
            assemble(layout, end_layers + [Layer(TOKEN_LOUDNESS, 0, b"")]),
        )
    )
    if first_s is not None:
        layers = list(layout.layers)
        del layers[first_s]
        cases.append(
            (
                "missing-first-spectral",
                "Remove the first native spectral layer and its payload while keeping a self-consistent file.",
                assemble(layout, layers),
            )
        )
    if first_r is not None:
        layers = list(layout.layers)
        del layers[first_r]
        cases.append(
            (
                "missing-first-loudness",
                "Remove the first native loudness layer and its payload while keeping a self-consistent file.",
                assemble(layout, layers),
            )
        )
    if first_s is not None and first_r is not None:
        layers = list(layout.layers)
        layers[first_s], layers[first_r] = layers[first_r], layers[first_s]
        cases.append(
            (
                "swap-spectral-loudness",
                "Swap one spectral and loudness layer, moving payloads with their headers.",
                assemble(layout, layers),
            )
        )

    # Structural corruption and documented header bounds.
    cases.extend(
        [
            ("truncate-1", "Remove the final byte of the cache.", baseline[:-1]),
            ("truncate-16", "Remove the final 16 bytes of the cache.", baseline[:-16]),
            (
                "bad-magic-rpkx",
                "Replace the RPKN/RPKL magic with unknown RPKX.",
                b"RPKX" + baseline[4:],
            ),
            (
                "channels-zero",
                "Set source channel count to zero.",
                patch_u8(baseline, 4, 0),
            ),
            (
                "samplerate-zero",
                "Set source sample rate to zero.",
                patch_u32(baseline, 6, 0),
            ),
            (
                "mipmap-count-zero",
                "Set layer count to zero while leaving the layer table and payload bytes present.",
                patch_u8(baseline, 5, 0),
            ),
            (
                "mipmap-count-plus1-no-header",
                "Increment layer count without inserting another layer header.",
                patch_u8(baseline, 5, baseline[5] + 1),
            ),
        ]
    )

    # Modify the final known layer's count without adjusting bytes.
    last_index = len(layout.layers) - 1
    last_count = layout.layers[last_index].count
    count_offset = 18 + last_index * 8 + 4
    cases.append(
        (
            "last-count-plus1-no-payload",
            "Increment final layer count without appending the corresponding payload.",
            patch_u32(baseline, count_offset, last_count + 1),
        )
    )
    if last_count > 0:
        cases.append(
            (
                "last-count-minus1-trailing",
                "Decrement final layer count while retaining the now-extra final record as trailing bytes.",
                patch_u32(baseline, count_offset, last_count - 1),
            )
        )

    # Exceed the officially documented maximum of 16 mipmaps with zero-count extras.
    if len(layout.layers) < 17:
        layers = list(layout.layers)
        while len(layers) < 17:
            layers.insert(positive_insert, Layer(extra_division + len(layers), 0, b""))
        cases.append(
            (
                "seventeen-layers",
                "Construct a self-consistent 17-layer cache, exceeding the documented maximum 16.",
                assemble(layout, layers),
            )
        )

    report_cases: list[dict[str, object]] = []
    for index, (name, description, mutated) in enumerate(cases, 1):
        print(f"[{index:02d}/{len(cases):02d}] {name}", flush=True)
        report_cases.append(
            run_case(
                reaper=args.reaper.resolve(),
                source=source,
                peak_path=peak_path,
                baseline=baseline,
                name=name,
                description=description,
                mutated=mutated,
                display=args.display,
                results=results,
                timeout=args.timeout,
            )
        )

    control = next(row for row in report_cases if row["name"] == "control")
    if control["classification"] != "accepted-preserved":
        raise RuntimeError(f"control cache was not accepted unchanged: {control}")

    summary: dict[str, int] = {}
    for row in report_cases:
        key = str(row["classification"])
        summary[key] = summary.get(key, 0) + 1
    report = {
        "reaper": str(args.reaper.resolve()),
        "source": str(source),
        "peak_path": str(peak_path),
        "baseline": {
            "magic": layout.magic.decode("ascii", "replace"),
            "channels": layout.channels,
            "layer_count": len(layout.layers),
            "layers": [
                {"token": layer.token, "count": layer.count, "payload_bytes": len(layer.payload)}
                for layer in layout.layers
            ],
            "size": len(baseline),
            "sha256": sha256(baseline),
        },
        "summary": summary,
        "cases": report_cases,
    }
    report_path = results / "cache-extension-oracle.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print("\n# summary")
    for classification, count in sorted(summary.items()):
        print(f"{classification}: {count}")
    for row in report_cases:
        print(
            f"{row['name']}: {row['classification']} begin={row['begin']} "
            f"size={row['input_size']}->{row['output_size']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
