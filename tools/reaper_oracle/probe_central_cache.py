#!/usr/bin/env python3
"""Probe REAPER's peak-cache path policy and central-cache name derivation.

The script launches a fresh REAPER process for each configuration so preference
state and existing cache files cannot leak between cases. It is primarily an
oracle/maintenance tool; normal applications should use the recovered algorithm
implemented in ``examples/player_common.py``.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import wave


def write_pcm16_wav(path: Path, value: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48_000)
        output.writeframes(
            struct.pack("<hhhh", value, -value, 1000 + value, -1000 - value)
        )


def reaper_hash(source: str) -> str:
    return hashlib.sha1(source.lower().encode("utf-8")).hexdigest()


def central_candidate(
    source: str, cache_directory: Path, extension: str = ".reapeaks"
) -> Path:
    digest = reaper_hash(source)
    return cache_directory / digest[:2] / f"{digest}{extension}"


def write_query_script(path: Path) -> None:
    path.write_text(
        r'''local manifest = assert(os.getenv("REAPEAKS_MANIFEST"))
local result = assert(os.getenv("REAPEAKS_RESULT"))
local scenario = assert(os.getenv("REAPEAKS_SCENARIO"))
local altpeaks = assert(os.getenv("REAPEAKS_ALTPEAKS"))
local altpeakspath = os.getenv("REAPEAKS_ALTPEAKSPATH") or ""
local altpeaksopathlist = os.getenv("REAPEAKS_ALTPEAKSOPATHLIST") or ""

local function clean(value)
  value = tostring(value or "")
  value = value:gsub("\\", "\\\\")
  value = value:gsub("\t", "\\t")
  value = value:gsub("\r", "\\r")
  value = value:gsub("\n", "\\n")
  return value
end

local function peak(fn, for_write)
  local ok, value = pcall(function()
    return reaper.GetPeakFileNameEx(fn, "", for_write)
  end)
  if ok then return clean(value) end
  return "ERROR:" .. clean(value)
end

local function source_name(fn)
  local ok, source = pcall(function() return reaper.PCM_Source_CreateFromFile(fn) end)
  if not ok or not source then return "" end
  local name_ok, name = pcall(function()
    return reaper.GetMediaSourceFileName(source, "")
  end)
  reaper.PCM_Source_Destroy(source)
  if not name_ok then return "" end
  return clean(name)
end

local out = assert(io.open(result, "w"))
out:write("scenario\taltpeaks\taltpeakspath\taltpeaksopathlist\tsource\tmedia_source_name\tread\twrite\n")
for fn in io.lines(manifest) do
  out:write(clean(scenario), "\t", clean(altpeaks), "\t", clean(altpeakspath), "\t",
            clean(altpeaksopathlist), "\t", clean(fn), "\t", source_name(fn), "\t",
            peak(fn, false), "\t", peak(fn, true), "\n")
end
out:close()
reaper.Main_OnCommand(40004, 0)
''',
        encoding="utf-8",
    )


def write_ini(
    path: Path,
    *,
    altpeaks: int,
    altpeakspath: str,
    altpeaksopathlist: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[REAPER]\n"
        f"altpeaks={altpeaks}\n"
        f"altpeakspath={altpeakspath}\n"
        f"altpeaksopathlist={altpeaksopathlist}\n"
        "peakcachegenmode=3\n"
        "peakcachegenrs=300\n"
        "showpeaks=64\n",
        encoding="utf-8",
    )


def unescape_tsv(value: str) -> str:
    # The Lua output escapes only characters that would break TSV records.
    return (
        value.replace("\\t", "\t")
        .replace("\\r", "\r")
        .replace("\\n", "\n")
        .replace("\\\\", "\\")
    )


def run_case(
    *,
    reaper: Path,
    script: Path,
    manifest: Path,
    work_root: Path,
    display: str,
    name: str,
    altpeaks: int,
    altpeakspath: str,
    altpeaksopathlist: str = "",
    cwd: Path | None = None,
    timeout: float = 90.0,
) -> list[dict[str, str]]:
    case = work_root / "cases" / name
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    ini = case / "reaper.ini"
    output = case / "result.tsv"
    log = case / "reaper.log"
    write_ini(
        ini,
        altpeaks=altpeaks,
        altpeakspath=altpeakspath,
        altpeaksopathlist=altpeaksopathlist,
    )
    env = os.environ.copy()
    env.update(
        DISPLAY=display,
        REAPEAKS_MANIFEST=str(manifest),
        REAPEAKS_RESULT=str(output),
        REAPEAKS_SCENARIO=name,
        REAPEAKS_ALTPEAKS=str(altpeaks),
        REAPEAKS_ALTPEAKSPATH=altpeakspath,
        REAPEAKS_ALTPEAKSOPATHLIST=altpeaksopathlist,
    )
    with log.open("wb") as handle:
        completed = subprocess.run(
            [
                str(reaper),
                "-newinst",
                "-cfgfile",
                str(ini),
                "-new",
                "-nosplash",
                str(script),
            ],
            cwd=cwd or case,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    if completed.returncode != 0 or not output.is_file():
        raise RuntimeError(
            f"REAPER central-cache probe {name!r} failed: "
            f"rc={completed.returncode}, log={log}"
        )
    with output.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    for row in rows:
        for key, value in list(row.items()):
            row[key] = unescape_tsv(value or "")
    return rows


def create_corpus(root: Path) -> list[str]:
    media = root / "media"
    files = [
        media / "A/same.wav",
        media / "B/same.wav",
        media / "A/other.wav",
        media / "sp ace/name with spaces.wav",
        media / "unicode-音声/曲.wav",
        media / ".hidden.wav",
        media / "dots/a/b/dotted.name.v1.wav",
        media / "UPPER.WAV",
        media / "lower.wav",
        media / "no-extension",
        media / "A/a.wav",
        media / "readonly/tone.wav",
        media / "unicode/ÄÖÜ.WAV",
        media / "unicode/äöü.wav",
        media / "unicode/İ.WAV",
        media / "unicode/i̇.wav",
        media / "unicode/I.WAV",
        media / "unicode/i.wav",
        media / "unicode/Σ.WAV",
        media / "unicode/σ.wav",
        media / "unicode/ς.wav",
        media / "unicode/ẞ.WAV",
        media / "unicode/ß.wav",
        media / "unicode/Ж.WAV",
        media / "unicode/ж.wav",
    ]
    for index, path in enumerate(files):
        write_pcm16_wav(path, index)
    symlink = media / "symlink-same.wav"
    try:
        symlink.symlink_to(media / "A/same.wav")
        files.append(symlink)
    except OSError:
        pass

    aliases = [
        str(media / "A/../A/same.wav"),
        str(media / "A/./same.wav"),
        str(media / "A/SAME.wav"),
        str(media / "A/nonexistent.wav"),
        "relative/path.wav",
        r"C:\\Users\\Example\\Audio\\Take.WAV",
        "C:/Users/Example/Audio/Take.WAV",
        r"\\\\server\\share\\Folder\\Take.WAV",
        "/tmp/mixed\\separator/Take.WAV",
    ]
    return [str(path) for path in files] + aliases


def clear_peak_candidates(sources: list[str], central_dir: Path) -> None:
    for source in sources:
        path = Path(source)
        for candidate in (
            Path(str(path) + ".reapeaks"),
            path.parent / "peaks" / (path.name + ".reapeaks"),
            central_candidate(source, central_dir),
        ):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def create_marker(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"cache-marker")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reaper", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--display", default=":97")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    reaper = args.reaper.resolve()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    sources = create_corpus(root)
    manifest = root / "paths.txt"
    manifest.write_text("\n".join(sources) + "\n", encoding="utf-8")
    script = root / "query.lua"
    write_query_script(script)

    all_rows: list[dict[str, str]] = []

    def probe(
        name: str,
        flags: int,
        cache_path: str,
        path_list: str = "",
        *,
        cwd: Path | None = None,
    ) -> None:
        all_rows.extend(
            run_case(
                reaper=reaper,
                script=script,
                manifest=manifest,
                work_root=root,
                display=args.display,
                name=name,
                altpeaks=flags,
                altpeakspath=cache_path,
                altpeaksopathlist=path_list,
                cwd=cwd,
                timeout=args.timeout,
            )
        )

    for flags in range(8):
        clear_peak_candidates(sources, cache)
        probe(f"flags-{flags}", flags, str(cache))

    clear_peak_candidates(sources, cache)
    probe("selective-A", 0, str(cache), str(root / "media/A"))
    clear_peak_candidates(sources, cache)
    probe(
        "selective-two",
        0,
        str(cache),
        f"{root / 'media/A'};{root / 'media/sp ace'}",
    )
    clear_peak_candidates(sources, cache)
    probe("selective-file", 0, str(cache), str(root / "media/B/same.wav"))

    relative_cwd = root / "relative-cwd"
    relative_cwd.mkdir(exist_ok=True)
    clear_peak_candidates(sources, cache)
    probe("relative-altpeakspath", 1, "relative-cache", cwd=relative_cwd)
    clear_peak_candidates(sources, cache)
    probe("empty-altpeakspath", 1, "")

    target_source = str(root / "media/A/same.wav")
    sidecar = Path(target_source + ".reapeaks")
    subdir = (
        Path(target_source).parent
        / "peaks"
        / (Path(target_source).name + ".reapeaks")
    )
    central = central_candidate(target_source, cache)
    for label, markers in (
        ("existing-sidecar", (sidecar,)),
        ("existing-subdir", (subdir,)),
        ("existing-central", (central,)),
        ("existing-all", (sidecar, subdir, central)),
    ):
        clear_peak_candidates(sources, cache)
        for marker in markers:
            create_marker(marker)
        probe(label, 1, str(cache))

    readonly_dir = root / "media/readonly"
    original_mode = stat.S_IMODE(readonly_dir.stat().st_mode)
    try:
        readonly_dir.chmod(0o555)
        for flags in (0, 2, 4, 6):
            clear_peak_candidates(sources, cache)
            probe(f"readonly-flags-{flags}", flags, str(cache))
    finally:
        readonly_dir.chmod(original_mode)

    output_tsv = root / "central-cache-probe.tsv"
    fields = [
        "scenario",
        "altpeaks",
        "altpeakspath",
        "altpeaksopathlist",
        "source",
        "media_source_name",
        "read",
        "write",
    ]
    with output_tsv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fields, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(all_rows)

    analysis: dict[str, object] = {
        "reaper": str(reaper),
        "row_count": len(all_rows),
        "sha1_lower_utf8_mismatches": [],
        "unicode_pairs": {},
    }
    mismatches: list[dict[str, str]] = []
    for row in all_rows:
        if row["scenario"] != "flags-1":
            continue
        expected = central_candidate(row["source"], cache)
        if Path(row["write"]) != expected:
            mismatches.append(
                {
                    "source": row["source"],
                    "expected": str(expected),
                    "actual": row["write"],
                }
            )
    analysis["sha1_lower_utf8_mismatches"] = mismatches

    baseline = {
        row["source"]: Path(row["write"]).stem
        for row in all_rows
        if row["scenario"] == "flags-1"
    }
    pairs = [
        (
            "latin-umlaut",
            root / "media/unicode/ÄÖÜ.WAV",
            root / "media/unicode/äöü.wav",
        ),
        (
            "turkish-dotted-i",
            root / "media/unicode/İ.WAV",
            root / "media/unicode/i̇.wav",
        ),
        ("ascii-i", root / "media/unicode/I.WAV", root / "media/unicode/i.wav"),
        (
            "greek-sigma",
            root / "media/unicode/Σ.WAV",
            root / "media/unicode/σ.wav",
        ),
        (
            "german-sharp-s",
            root / "media/unicode/ẞ.WAV",
            root / "media/unicode/ß.wav",
        ),
        (
            "cyrillic",
            root / "media/unicode/Ж.WAV",
            root / "media/unicode/ж.wav",
        ),
    ]
    analysis["unicode_pairs"] = {
        label: {
            "upper": baseline.get(str(upper), ""),
            "lower": baseline.get(str(lower), ""),
            "same": baseline.get(str(upper), "") == baseline.get(str(lower), ""),
        }
        for label, upper, lower in pairs
    }
    (root / "central-cache-analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
