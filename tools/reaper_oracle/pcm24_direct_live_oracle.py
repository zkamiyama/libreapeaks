#!/usr/bin/env python3
"""Live REAPER 7.79 byte-exact gate for direct PCM24/i32 generation.

Two real source containers represent the same normalized signal:

- packed signed 24-bit PCM WAV for generate_pcm24_reaper();
- signed 32-bit PCM WAV containing the same 24-bit values shifted left by 8 for
  generate_pcm24_i32_reaper().

REAPER itself builds the oracle .reapeaks files in fresh processes.  The Rust
fixture then consumes the original right-justified 24-bit values and must match
those oracle files byte-for-byte, including each source's own mtime/size stamp.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import wave

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
from fresh_process import run_one_source  # noqa: E402

SAMPLE_RATE = 48_000
CHANNELS = 2
FRAMES = SAMPLE_RATE * 2 + 137
PEAK_RATE = 300
PEAKCACHEGENMODE = 3
FIXED_MTIME = 1_700_000_000
MASK64 = (1 << 64) - 1


def make_samples() -> list[int]:
    state = 0x7A15_5EED_D15C_A11E
    out: list[int] = []
    for index in range(FRAMES * CHANNELS):
        state = (
            state * 6_364_136_223_846_793_005 + 1_442_695_040_888_963_407
        ) & MASK64
        raw = (state >> 17) & 0x00FF_FFFF
        sample = raw - (1 << 24) if raw & 0x0080_0000 else raw
        special = index % 997
        if special == 0:
            sample = -8_388_608
        elif special == 1:
            sample = 8_388_607
        elif special == 2:
            sample = -1
        elif special == 3:
            sample = 0
        elif special == 4:
            sample = 1
        out.append(sample)
    return out


def pack_pcm24le(samples: list[int]) -> bytes:
    raw = bytearray(len(samples) * 3)
    offset = 0
    for sample in samples:
        value = sample & 0x00FF_FFFF
        raw[offset] = value & 0xFF
        raw[offset + 1] = (value >> 8) & 0xFF
        raw[offset + 2] = (value >> 16) & 0xFF
        offset += 3
    return bytes(raw)


def make_media(root: Path) -> tuple[Path, Path, Path]:
    media = root / "media"
    media.mkdir(parents=True, exist_ok=True)
    source24 = media / "pcm24.wav"
    source32 = media / "pcm24-in-i32.wav"
    raw_path = media / "source.pcm24le"
    samples = make_samples()
    packed24 = pack_pcm24le(samples)
    raw_path.write_bytes(packed24)

    with wave.open(str(source24), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(3)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(packed24)

    packed32 = struct.pack("<" + "i" * len(samples), *(sample << 8 for sample in samples))
    with wave.open(str(source32), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(4)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(packed32)

    fixed_ns = FIXED_MTIME * 1_000_000_000
    for source in (source24, source32):
        os.utime(source, ns=(fixed_ns, fixed_ns))
    return source24, source32, raw_path


def parse_header(data: bytes) -> dict[str, object]:
    if len(data) < 18:
        raise RuntimeError("truncated .reapeaks header")
    count = data[5]
    if len(data) < 18 + count * 8:
        raise RuntimeError("truncated .reapeaks layer table")
    return {
        "magic": data[:4].decode("ascii", "replace"),
        "channels": data[4],
        "layer_count": count,
        "sample_rate": struct.unpack_from("<I", data, 6)[0],
        "source_mtime_low32": struct.unpack_from("<I", data, 10)[0],
        "source_size_low32": struct.unpack_from("<I", data, 14)[0],
        "layers": [
            list(struct.unpack_from("<iI", data, 18 + 8 * index))
            for index in range(count)
        ],
    }


def native_mode(header: dict[str, object]) -> str:
    raw_layers = header.get("layers")
    if not isinstance(raw_layers, list):
        raise RuntimeError("missing .reapeaks layer table")
    tokens = [int(layer[0]) for layer in raw_layers]
    positives = [token for token in tokens if token > 0]
    if not positives or tokens[: len(positives)] != positives:
        raise RuntimeError(f"unexpected waveform-layer ordering: {tokens}")
    count = len(positives)
    shapes = {
        "waveform": positives,
        "spectral": positives + [-115] * count + [-114] * max(0, count - 1),
        "spectrogram": (
            positives
            + [-115] * count
            + [-103] * max(0, count - 1)
            + [-114] * max(0, count - 1)
        ),
    }
    for mode, expected in shapes.items():
        if tokens == expected:
            return mode
    raise RuntimeError(f"oracle produced an unknown native layer shape: {tokens}")


def discover_showpeaks(reaper: Path, root: Path, display: str) -> dict[str, int]:
    work = Path(tempfile.mkdtemp(prefix="reapeaks-pcm24-actions-"))
    result = root / "results" / "action-map.tsv"
    log = root / "results" / "action-map.reaper.log"
    lua = work / "discover.lua"
    lua.write_text(
        """local out_path=os.getenv('REAPEAKS_RESULT')
local f=assert(io.open(out_path,'w'))
local section=reaper.SectionFromUniqueID(0)
for idx=0,30000 do
  local cmd,name=reaper.kbd_enumerateActions(section,idx)
  if not cmd or cmd==0 then break end
  local lower=string.lower(name or '')
  if string.find(lower,'peaks:',1,true) and
     (string.find(lower,'spectral',1,true) or
      string.find(lower,'spectrogram',1,true) or
      string.find(lower,'normal',1,true)) then
    reaper.set_config_var_string('showpeaks','1',0)
    reaper.Main_OnCommand(cmd,0)
    local ok,value=reaper.get_config_var_string('showpeaks')
    f:write('ACTION\\t',cmd,'\\t',string.gsub(name or '','[\\t\\r\\n]',' '),'\\t',tostring(ok),'\\t',tostring(value),'\\n')
  end
end
f:close()
reaper.Main_OnCommand(40004,0)
""",
        encoding="utf-8",
    )
    config = work / "reaper.ini"
    config.write_text(
        "[REAPER]\npeakcachegenmode=3\npeakcachegenrs=300\nshowpeaks=1\naltpeaks=2\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(DISPLAY=display, REAPEAKS_RESULT=str(result))
    with log.open("wb") as output:
        completed = subprocess.run(
            [
                str(reaper),
                "-newinst",
                "-cfgfile",
                str(config),
                "-new",
                "-nosplash",
                str(lua),
            ],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=60,
        )
    if completed.returncode != 0 or not result.exists():
        raise RuntimeError(f"REAPER action discovery failed rc={completed.returncode}; log={log}")

    values: dict[str, int] = {}
    for line in result.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 5 and parts[0] == "ACTION":
            try:
                values[parts[2]] = int(parts[4], 0)
            except ValueError:
                continue
    required = {
        "waveform": "Peaks: Show normal peaks",
        "spectral": "Peaks: Toggle spectral peaks",
        "spectrogram": "Peaks: Toggle spectrogram",
    }
    missing = [action for action in required.values() if action not in values]
    if missing:
        raise RuntimeError(f"missing REAPER native peak actions: {missing}; got={sorted(values)}")
    return {mode: values[action] for mode, action in required.items()}


def source_stamp(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return int(stat.st_mtime) & 0xFFFF_FFFF, stat.st_size & 0xFFFF_FFFF


def first_difference(left: bytes, right: bytes) -> dict[str, int] | None:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return {"offset": index, "oracle": a, "libreapeaks": b}
    if len(left) != len(right):
        return {"offset": min(len(left), len(right)), "oracle_length": len(left), "libreapeaks_length": len(right)}
    return None


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def assert_oracle_stamp(source: Path, data: bytes, label: str) -> None:
    header = parse_header(data)
    observed = (int(header["source_mtime_low32"]), int(header["source_size_low32"]))
    expected = source_stamp(source)
    if observed != expected:
        raise RuntimeError(f"{label}: REAPER source stamp differs from stat(): {observed} != {expected}")


def build_libreapeaks(
    *,
    source24: Path,
    source32: Path,
    raw: Path,
    results: Path,
    mode: str,
) -> tuple[Path, Path]:
    packed = results / f"libreapeaks-{mode}-pcm24.reapeaks"
    i32_cache = results / f"libreapeaks-{mode}-i32.reapeaks"
    subprocess.run(
        [
            "cargo",
            "run",
            "--quiet",
            "--example",
            "pcm24_oracle_fixture",
            "--",
            str(source24),
            str(source32),
            str(raw),
            str(packed),
            str(i32_cache),
            mode,
        ],
        cwd=REPO,
        check=True,
        timeout=240,
    )
    return packed, i32_cache


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("reaper", type=Path)
    parser.add_argument("--display", default=":97")
    args = parser.parse_args()

    root = args.root.resolve()
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    source24, source32, raw = make_media(root)
    showpeaks = discover_showpeaks(args.reaper.resolve(), root, args.display)

    rows: list[dict[str, object]] = []
    all_exact = True
    for mode in ("waveform", "spectral", "spectrogram"):
        sp = showpeaks[mode]
        oracle24 = run_one_source(
            reaper=args.reaper.resolve(),
            source=source24,
            probe=HERE / "build_one.lua",
            results_dir=results,
            label=f"oracle-{mode}-pcm24",
            display=args.display,
            peakcachegenrs=PEAK_RATE,
            showpeaks=sp,
            peakcachegenmode=PEAKCACHEGENMODE,
            expected_source_type="WAVE",
        )
        oracle32 = run_one_source(
            reaper=args.reaper.resolve(),
            source=source32,
            probe=HERE / "build_one.lua",
            results_dir=results,
            label=f"oracle-{mode}-i32",
            display=args.display,
            peakcachegenrs=PEAK_RATE,
            showpeaks=sp,
            peakcachegenmode=PEAKCACHEGENMODE,
            expected_source_type="WAVE",
        )
        oracle24_bytes = oracle24.copied_peak.read_bytes()
        oracle32_bytes = oracle32.copied_peak.read_bytes()
        assert_oracle_stamp(source24, oracle24_bytes, f"{mode}/pcm24")
        assert_oracle_stamp(source32, oracle32_bytes, f"{mode}/i32")
        if native_mode(parse_header(oracle24_bytes)) != mode:
            raise RuntimeError(f"{mode}/pcm24: showpeaks={sp} produced wrong native mode")
        if native_mode(parse_header(oracle32_bytes)) != mode:
            raise RuntimeError(f"{mode}/i32: showpeaks={sp} produced wrong native mode")

        packed_path, i32_path = build_libreapeaks(
            source24=source24,
            source32=source32,
            raw=raw,
            results=results,
            mode=mode,
        )
        packed = packed_path.read_bytes()
        i32_cache = i32_path.read_bytes()
        packed_exact = packed == oracle24_bytes
        i32_exact = i32_cache == oracle32_bytes
        all_exact = all_exact and packed_exact and i32_exact
        rows.append(
            {
                "mode": mode,
                "showpeaks": sp,
                "pcm24": {
                    "oracle_magic": parse_header(oracle24_bytes)["magic"],
                    "oracle_sha256": digest(oracle24_bytes),
                    "libreapeaks_sha256": digest(packed),
                    "oracle_size": len(oracle24_bytes),
                    "libreapeaks_size": len(packed),
                    "byte_exact": packed_exact,
                    "first_difference": first_difference(oracle24_bytes, packed),
                },
                "i32": {
                    "oracle_magic": parse_header(oracle32_bytes)["magic"],
                    "oracle_sha256": digest(oracle32_bytes),
                    "libreapeaks_sha256": digest(i32_cache),
                    "oracle_size": len(oracle32_bytes),
                    "libreapeaks_size": len(i32_cache),
                    "byte_exact": i32_exact,
                    "first_difference": first_difference(oracle32_bytes, i32_cache),
                },
            }
        )

    report = {
        "oracle": "REAPER 7.79 Linux x86_64",
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "frames": FRAMES,
        "peak_rate": PEAK_RATE,
        "source24_stamp": source_stamp(source24),
        "source32_stamp": source_stamp(source32),
        "showpeaks": showpeaks,
        "cases": rows,
        "all_byte_exact": all_exact,
    }
    report_path = results / "pcm24-direct-live-oracle.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not all_exact:
        raise RuntimeError("PCM24/i32 direct generation is not byte-exact against REAPER; see report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
