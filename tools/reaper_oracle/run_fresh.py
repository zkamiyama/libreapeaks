#!/usr/bin/env python3
"""Run REAPER once per media file and collect .reapeaks spectral hashes.

The X server may be reused. The REAPER process may not: reverse-engineering
probes observed source-to-source spectral state leakage inside one process.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import struct
import subprocess
import tempfile
import time

FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3


def fnv64(data: bytes) -> int:
    h = FNV_OFFSET
    for byte in data:
        h ^= byte
        h = (h * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h


def parse_spectral(path: pathlib.Path):
    data = path.read_bytes()
    if len(data) < 18:
        raise ValueError(f"truncated reapeaks: {path}")
    magic = data[:4].decode("ascii", "replace")
    channels = data[4]
    mipmaps = data[5]
    sample_rate = struct.unpack_from("<I", data, 6)[0]
    headers = [struct.unpack_from("<iI", data, 18 + 8 * i) for i in range(mipmaps)]
    positive_divs = [div for div, _ in headers if div > 0]
    pos = 18 + 8 * mipmaps
    spectral = []
    spectral_index = 0

    for div, count in headers:
        if div > 0 or div == -115:
            size = count * channels * 4
        elif div == -114:
            # REAPER 7.79 Linux live fixtures occupy 4 bytes/channel/sample.
            size = count * channels * 4
        else:
            raise ValueError(f"unsupported oracle token {div} in {path}")

        payload = data[pos : pos + size]
        if len(payload) != size:
            raise ValueError(f"truncated payload in {path}")
        if div == -115:
            spectral.append(
                {
                    "division": positive_divs[spectral_index],
                    "count": count,
                    "codes": count * channels,
                    "fnv64": fnv64(payload),
                }
            )
            spectral_index += 1
        pos += size

    if pos != len(data):
        raise ValueError(f"layout did not consume file: {pos} != {len(data)} for {path}")
    return magic, sample_rate, channels, spectral


def peak_candidates(media: pathlib.Path):
    name = media.name + ".reapeaks"
    yield media.parent / name
    yield media.parent / "peaks" / name
    yield from media.parent.rglob(name)


def remove_old_peaks(media: pathlib.Path):
    seen = set()
    for path in peak_candidates(media):
        try:
            resolved = path.resolve()
        except FileNotFoundError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file():
            path.unlink()


def find_peak(media: pathlib.Path) -> pathlib.Path:
    found = []
    seen = set()
    for path in peak_candidates(media):
        if path.is_file():
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(path)
    if len(found) != 1:
        raise RuntimeError(f"expected one peak file for {media}, found {found}")
    return found[0]


def write_config(path: pathlib.Path, peak_rate: int):
    path.write_text(
        "[REAPER]\n"
        "peakcachegenmode=3\n"
        f"peakcachegenrs={peak_rate}\n"
        "showpeaks=64\n",
        encoding="utf-8",
    )


def media_files(root: pathlib.Path):
    extensions = {".wav", ".flac", ".aif", ".aiff", ".ogg", ".mp3", ".opus"}
    return sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in extensions)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reaper", required=True, type=pathlib.Path)
    parser.add_argument("--media-dir", required=True, type=pathlib.Path)
    parser.add_argument("--script", type=pathlib.Path, default=pathlib.Path(__file__).with_name("build_one.lua"))
    parser.add_argument("--peak-rate", type=int, default=300)
    parser.add_argument("--display", default=":99")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--log-dir", type=pathlib.Path)
    args = parser.parse_args()

    if not args.reaper.is_file():
        parser.error(f"REAPER not found: {args.reaper}")
    if not args.media_dir.is_dir():
        parser.error(f"media directory not found: {args.media_dir}")
    if shutil.which("Xvfb") is None:
        parser.error("Xvfb not found")

    logs = args.log_dir or (args.media_dir / "oracle-logs")
    logs.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="libreapeaks-reaper-oracle-") as temp:
        tempdir = pathlib.Path(temp)
        config = tempdir / "reaper.ini"
        write_config(config, args.peak_rate)

        xvfb_log = (logs / "xvfb.log").open("wb")
        xvfb = subprocess.Popen(
            ["Xvfb", args.display, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
            stdout=xvfb_log,
            stderr=subprocess.STDOUT,
        )
        try:
            time.sleep(0.35)
            print("# name\tmagic\tsample_rate\tchannels\tdivisions\tlevel_counts\tlevel_fnv64")
            for media in media_files(args.media_dir):
                remove_old_peaks(media)
                result = tempdir / "result.txt"
                result.unlink(missing_ok=True)
                env = os.environ.copy()
                env.update(
                    DISPLAY=args.display,
                    REAPEAKS_MEDIA=str(media.resolve()),
                    REAPEAKS_RESULT=str(result),
                )
                log_path = logs / f"{media.name}.log"
                with log_path.open("wb") as log:
                    completed = subprocess.run(
                        [
                            str(args.reaper),
                            "-cfgfile",
                            str(config),
                            "-new",
                            "-nosplash",
                            str(args.script.resolve()),
                        ],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=args.timeout,
                        check=False,
                    )
                status = result.read_text(encoding="utf-8") if result.exists() else ""
                if completed.returncode != 0 or "OK loops=" not in status:
                    raise RuntimeError(
                        f"REAPER failed for {media}: returncode={completed.returncode}, status={status!r}, log={log_path}"
                    )

                peak = find_peak(media)
                magic, sample_rate, channels, levels = parse_spectral(peak)
                divisions = ",".join(str(level["division"]) for level in levels)
                counts = ",".join(str(level["count"]) for level in levels)
                hashes = ",".join(f"{level['fnv64']:016x}" for level in levels)
                print(f"{media.stem}\t{magic}\t{sample_rate}\t{channels}\t{divisions}\t{counts}\t{hashes}")
        finally:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=3)
            except subprocess.TimeoutExpired:
                xvfb.kill()
            xvfb_log.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
