"""Shared, defensive helpers for the libreapeaks reference players.

The player demos deliberately keep media decoding outside the Rust core.  They
can reuse an existing REAPER cache or generate one from checked WAV data or an
explicit FFmpeg decode.  Cache placement, publication and decoder selection are
kept here so the desktop and browser demos exercise exactly the same behavior.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import array
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat as stat_module
import struct
import subprocess
import sys
import tempfile
import time
from typing import Iterator, Literal, Optional, Sequence

import reapeaks

CacheDecoder = Literal["auto", "wav", "ffmpeg"]
PlaybackDecoder = Literal["native", "ffmpeg"]
CacheMode = Literal["auto", "sidecar", "subdir", "central", "reaper"]
WaveEncodingOption = Literal["auto", "rpkn", "rpkl"]

DEFAULT_DECODE_TIMEOUT = 300.0
DEFAULT_MAX_DECODE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_TOOL_DIAGNOSTIC_BYTES = 64 * 1024
_MAX_WAV_FMT_BYTES = 1024 * 1024


class PlayerCacheError(ValueError):
    """A media decode, cache validation or cache publication failure."""


class UnsupportedWavError(PlayerCacheError):
    """A valid RIFF/WAVE container whose sample representation is unsupported."""


@dataclass(frozen=True)
class AudioCacheInput:
    sample_rate: int
    channels: int
    frames: int
    sample_type: Literal["i16", "f32"]
    pcm_bytes: bytes
    source_codec: str = ""
    source_container: str = ""
    recommended_wave_encoding: Literal["rpkn", "rpkl"] = "rpkn"


# Backward-compatible name used by the first version of the demos.
WavCacheInput = AudioCacheInput


@dataclass(frozen=True)
class ReaPeaksHeader:
    magic: bytes
    channels: int
    mipmap_count: int
    sample_rate: int
    source_mtime_low32: int
    source_size_low32: int


@dataclass(frozen=True)
class CacheInspection:
    path: Path
    exists: bool
    parseable: bool
    fresh: bool
    reason: str
    header: ReaPeaksHeader | None = None


@dataclass
class PreparedPlayback:
    """A path ready for a playback backend, with optional temporary ownership."""

    path: Path
    decoder: PlaybackDecoder
    _temporary: tempfile.TemporaryDirectory[str] | None = field(
        default=None, repr=False
    )

    def close(self) -> None:
        temporary, self._temporary = self._temporary, None
        if temporary is not None:
            temporary.cleanup()

    def __enter__(self) -> "PreparedPlayback":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def sidecar_peak_path(audio_path: str | Path) -> Path:
    audio = Path(audio_path)
    return audio.with_name(audio.name + ".reapeaks")


def peak_path_for_audio(audio_path: str | Path) -> Path:
    """Compatibility alias for the original demo helper."""

    return sidecar_peak_path(audio_path)


def subdir_peak_path(audio_path: str | Path) -> Path:
    audio = Path(audio_path)
    return audio.parent / "peaks" / (audio.name + ".reapeaks")


def _path_name_max(directory: Path) -> int:
    probe = directory
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        value = int(os.pathconf(probe, "PC_NAME_MAX"))
    except (AttributeError, OSError, ValueError):
        value = 255
    return max(64, value)


def _truncate_fs_component(component: str, byte_budget: int) -> str:
    if byte_budget <= 0:
        return ""
    encoded = os.fsencode(component)
    if len(encoded) <= byte_budget:
        return component
    encoded = encoded[:byte_budget]
    # os.fsdecode uses surrogateescape, so no filesystem byte is lost or made
    # invalid when a component ends in the middle of a UTF-8 sequence.
    return os.fsdecode(encoded)


def central_peak_path(audio_path: str | Path, cache_directory: str | Path) -> Path:
    audio = _resolved(audio_path)
    directory = _resolved(cache_directory)
    digest = hashlib.sha256(os.fsencode(str(audio))).hexdigest()[:24]
    suffix = f".{digest}.reapeaks"
    budget = _path_name_max(directory) - len(os.fsencode(suffix))
    prefix = _truncate_fs_component(audio.name, budget)
    if not prefix:
        prefix = "media"
    return directory / f"{prefix}{suffix}"


def _normalise_map_source(value: str, map_path: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = map_path.parent / path
    return str(path.resolve(strict=False))


def _normalise_map_target(value: str, map_path: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = map_path.parent / path
    return path.resolve(strict=False)


def _load_reaper_cache_map(path: str | Path) -> dict[str, dict[str, str]]:
    map_path = _resolved(path)
    try:
        text = map_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise PlayerCacheError(f"cannot read REAPER cache map {map_path}: {exc}") from exc

    entries: dict[str, dict[str, str]] = {}
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlayerCacheError(f"invalid REAPER cache-map JSON: {exc}") from exc
        if isinstance(payload, dict) and isinstance(payload.get("media"), str):
            # query_peak_path.lua emits one self-contained record. Accept it
            # directly as well as the aggregate {"entries": {...}} format.
            raw_entries = {
                payload["media"]: {
                    "read": payload.get("read", ""),
                    "write": payload.get("write", ""),
                }
            }
        else:
            raw_entries = payload.get("entries", payload) if isinstance(payload, dict) else None
        if not isinstance(raw_entries, dict):
            raise PlayerCacheError("REAPER cache-map JSON must contain an object")
        for raw_source, raw_value in raw_entries.items():
            if not isinstance(raw_source, str):
                continue
            source = _normalise_map_source(raw_source, map_path)
            if isinstance(raw_value, str):
                entries[source] = {"read": raw_value, "write": raw_value}
            elif isinstance(raw_value, dict):
                read = raw_value.get("read", "")
                write = raw_value.get("write", "")
                if not isinstance(read, str) or not isinstance(write, str):
                    raise PlayerCacheError(
                        f"REAPER cache-map entry for {raw_source!r} has non-string paths"
                    )
                entries[source] = {"read": read, "write": write}
            else:
                raise PlayerCacheError(
                    f"REAPER cache-map entry for {raw_source!r} has an invalid value"
                )
    else:
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line or line.lstrip().startswith("#"):
                continue
            columns = line.split("\t")
            if len(columns) == 2:
                raw_source, raw_path = columns
                read = write = raw_path
            elif len(columns) >= 3:
                raw_source, read, write = columns[:3]
            else:
                raise PlayerCacheError(
                    f"invalid REAPER cache-map TSV line {line_number}"
                )
            source = _normalise_map_source(raw_source, map_path)
            entries[source] = {"read": read, "write": write}
    return entries


def reaper_mapped_peak_path(
    audio_path: str | Path,
    map_path: str | Path,
    *,
    for_write: bool,
) -> Path:
    map_file = _resolved(map_path)
    entries = _load_reaper_cache_map(map_file)
    source = str(_resolved(audio_path))
    entry = entries.get(source)
    if entry is None and os.name == "nt":
        folded = os.path.normcase(source)
        entry = next(
            (value for key, value in entries.items() if os.path.normcase(key) == folded),
            None,
        )
    if entry is None:
        raise PlayerCacheError(f"no REAPER cache-map entry for {source}")
    preferred = entry["write" if for_write else "read"]
    fallback = entry["read" if for_write else "write"]
    value = preferred or fallback
    if not value:
        raise PlayerCacheError(f"REAPER cache-map entry has no usable path for {source}")
    return _normalise_map_target(value, map_file)


def _case_variants(path: Path) -> tuple[Path, ...]:
    name = path.name
    if name.lower().endswith(".reapeaks"):
        stem = name[: -len(".reapeaks")]
    else:
        stem = name
    lower = path.with_name(stem + ".reapeaks")
    upper = path.with_name(stem + ".ReaPeaks")
    return (lower, upper)


def _unique_paths(paths: Sequence[Path | None]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in paths:
        if value is None:
            continue
        path = _resolved(value)
        key = os.path.normcase(str(path)) if os.name == "nt" else str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _read_candidates(
    audio: Path,
    cache_mode: CacheMode,
    cache_directory: str | Path | None,
    reaper_cache_map: str | Path | None,
) -> list[Path]:
    mapped: Path | None = None
    if reaper_cache_map is not None:
        try:
            mapped = reaper_mapped_peak_path(audio, reaper_cache_map, for_write=False)
        except PlayerCacheError:
            if cache_mode == "reaper":
                raise
    sidecar = _case_variants(sidecar_peak_path(audio))
    subdir = _case_variants(subdir_peak_path(audio))
    central = (
        _case_variants(central_peak_path(audio, cache_directory))
        if cache_directory is not None
        else ()
    )
    if cache_mode == "sidecar":
        return _unique_paths(sidecar)
    if cache_mode == "subdir":
        return _unique_paths(subdir)
    if cache_mode == "central":
        if cache_directory is None:
            raise PlayerCacheError("cache_mode=central requires cache_directory")
        return _unique_paths(central)
    if cache_mode == "reaper":
        if mapped is None:
            raise PlayerCacheError("cache_mode=reaper requires a matching cache map")
        return _unique_paths([mapped])
    return _unique_paths([mapped, *sidecar, *subdir, *central])


def _write_target(
    audio: Path,
    cache_mode: CacheMode,
    cache_directory: str | Path | None,
    reaper_cache_map: str | Path | None,
) -> Path:
    if cache_mode in ("auto", "sidecar"):
        if cache_mode == "auto" and reaper_cache_map is not None:
            try:
                return reaper_mapped_peak_path(audio, reaper_cache_map, for_write=True)
            except PlayerCacheError:
                pass
        return _resolved(sidecar_peak_path(audio))
    if cache_mode == "subdir":
        return _resolved(subdir_peak_path(audio))
    if cache_mode == "central":
        if cache_directory is None:
            raise PlayerCacheError("cache_mode=central requires cache_directory")
        return central_peak_path(audio, cache_directory)
    if cache_mode == "reaper":
        if reaper_cache_map is None:
            raise PlayerCacheError("cache_mode=reaper requires reaper_cache_map")
        return reaper_mapped_peak_path(audio, reaper_cache_map, for_write=True)
    raise PlayerCacheError(f"unknown cache mode: {cache_mode}")


def find_existing_peaks(
    audio_path: str | Path,
    *,
    cache_mode: CacheMode = "auto",
    cache_directory: str | Path | None = None,
    reaper_cache_map: str | Path | None = None,
) -> Optional[Path]:
    audio = _resolved(audio_path)
    for candidate in _read_candidates(
        audio, cache_mode, cache_directory, reaper_cache_map
    ):
        if candidate.is_file():
            return candidate
    return None


def read_reapeaks_header(path: str | Path) -> ReaPeaksHeader:
    file_path = _resolved(path)
    try:
        with file_path.open("rb") as handle:
            raw = handle.read(18)
    except OSError as exc:
        raise PlayerCacheError(f"cannot read cache header {file_path}: {exc}") from exc
    if len(raw) != 18:
        raise PlayerCacheError(f"truncated .reapeaks header: {file_path}")
    magic = raw[:4]
    if magic not in (b"RPKM", b"RPKN", b"RPKL"):
        raise PlayerCacheError(f"unsupported .reapeaks magic {magic!r}: {file_path}")
    channels = raw[4]
    sample_rate = struct.unpack_from("<I", raw, 6)[0]
    if channels == 0 or sample_rate == 0:
        raise PlayerCacheError(f"invalid .reapeaks header: {file_path}")
    return ReaPeaksHeader(
        magic=magic,
        channels=channels,
        mipmap_count=raw[5],
        sample_rate=sample_rate,
        source_mtime_low32=struct.unpack_from("<I", raw, 10)[0],
        source_size_low32=struct.unpack_from("<I", raw, 14)[0],
    )


def _source_metadata(stat_result: os.stat_result) -> tuple[int, int]:
    try:
        mtime, size = reapeaks.source_stamp_from_unix_seconds(
            int(stat_result.st_mtime), int(stat_result.st_size)
        )
    except Exception as exc:
        raise PlayerCacheError(f"cannot build REAPER source stamp: {exc}") from exc
    return int(mtime), int(size)


def inspect_reapeaks_cache(
    peaks_path: str | Path, audio_path: str | Path
) -> CacheInspection:
    peaks = _resolved(peaks_path)
    audio = _resolved(audio_path)
    if not peaks.is_file():
        return CacheInspection(peaks, False, False, False, "missing")
    try:
        header = read_reapeaks_header(peaks)
        # Use the Rust parser for structural validation, then the library's
        # source-stamp comparator instead of duplicating .reapeaks semantics here.
        parsed = reapeaks.ReaPeaks.open(str(peaks))
        source_stat = audio.stat()
        expected_mtime, expected_size = _source_metadata(source_stat)
        fresh = bool(parsed.matches_source_stamp(expected_mtime, expected_size))
    except Exception as exc:
        # PyO3 exceptions do not share a stable class across supported Python
        # versions, so parser failures are intentionally normalised here.
        return CacheInspection(peaks, True, False, False, str(exc))
    reason = "fresh" if fresh else (
        "source metadata mismatch: "
        f"cache(mtime={header.source_mtime_low32},size={header.source_size_low32}) "
        f"source(mtime={expected_mtime},size={expected_size})"
    )
    return CacheInspection(peaks, True, True, fresh, reason, header)


def _read_exact(handle, count: int, what: str) -> bytes:
    data = handle.read(count)
    if len(data) != count:
        raise PlayerCacheError(f"truncated WAV {what}")
    return data


def read_wav_cache_input(
    path: str | Path,
    *,
    max_decode_bytes: int = DEFAULT_MAX_DECODE_BYTES,
) -> AudioCacheInput:
    """Read checked PCM16 or IEEE-float32 RIFF/WAVE without third parties."""

    if max_decode_bytes < 0:
        raise PlayerCacheError("max_decode_bytes must be non-negative")
    file_path = _resolved(path)
    try:
        file_size = file_path.stat().st_size
        handle = file_path.open("rb")
    except OSError as exc:
        raise PlayerCacheError(f"cannot open WAV {file_path}: {exc}") from exc

    with handle:
        header = _read_exact(handle, 12, "header")
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise UnsupportedWavError("not a RIFF/WAVE file")
        riff_size = struct.unpack_from("<I", header, 4)[0]
        riff_end = riff_size + 8
        if riff_end > file_size:
            raise PlayerCacheError(
                f"truncated WAV RIFF payload: declared={riff_end}, actual={file_size}"
            )
        if riff_end < 12:
            raise PlayerCacheError("invalid WAV RIFF size")

        fmt: bytes | None = None
        pcm_parts: list[bytes] = []
        decoded_bytes = 0
        saw_data = False
        pos = 12
        while pos < riff_end:
            if pos + 8 > riff_end:
                raise PlayerCacheError("truncated WAV chunk header")
            handle.seek(pos)
            chunk_header = _read_exact(handle, 8, "chunk header")
            chunk_id = chunk_header[:4]
            chunk_size = struct.unpack_from("<I", chunk_header, 4)[0]
            start = pos + 8
            end = start + chunk_size
            padded_end = end + (chunk_size & 1)
            if end > riff_end or padded_end > riff_end:
                raise PlayerCacheError(
                    f"truncated WAV chunk {chunk_id!r}: size={chunk_size}"
                )
            if chunk_id == b"fmt ":
                if chunk_size > _MAX_WAV_FMT_BYTES:
                    raise PlayerCacheError("unreasonably large WAV fmt chunk")
                handle.seek(start)
                candidate = _read_exact(handle, chunk_size, "fmt chunk")
                if fmt is None:
                    fmt = candidate
                elif fmt != candidate:
                    raise PlayerCacheError("conflicting WAV fmt chunks")
            elif chunk_id == b"data":
                saw_data = True
                decoded_bytes += chunk_size
                if decoded_bytes > max_decode_bytes:
                    raise PlayerCacheError(
                        f"decoded WAV exceeds max_decode_bytes={max_decode_bytes}"
                    )
                handle.seek(start)
                pcm_parts.append(_read_exact(handle, chunk_size, "data chunk"))
            pos = padded_end

        if pos != riff_end:
            raise PlayerCacheError("WAV chunks do not exactly consume RIFF payload")
        if fmt is None or not saw_data or len(fmt) < 16:
            raise PlayerCacheError("WAV is missing a valid fmt/data chunk")

    format_tag, channels, sample_rate, byte_rate, block_align, bits = struct.unpack_from(
        "<HHIIHH", fmt, 0
    )
    if format_tag == 0xFFFE:
        if len(fmt) < 40:
            raise PlayerCacheError("truncated WAVE_FORMAT_EXTENSIBLE fmt chunk")
        cb_size = struct.unpack_from("<H", fmt, 16)[0]
        if cb_size < 22 or 18 + cb_size > len(fmt):
            raise PlayerCacheError("invalid WAVE_FORMAT_EXTENSIBLE cbSize")
        valid_bits = struct.unpack_from("<H", fmt, 18)[0]
        if valid_bits not in (0, bits) and not (0 < valid_bits <= bits):
            raise PlayerCacheError("invalid WAVE_FORMAT_EXTENSIBLE valid bits")
        # KSDATAFORMAT_SUBTYPE_* has Data1 equal to the original format tag and
        # a fixed GUID tail. Looking only at the first two bytes would accept
        # unrelated or malformed GUIDs as PCM/float audio.
        guid_tail = bytes.fromhex("000000001000800000aa00389b71")
        if fmt[26:40] != guid_tail:
            raise UnsupportedWavError("unsupported WAVE_FORMAT_EXTENSIBLE subformat GUID")
        format_tag = struct.unpack_from("<H", fmt, 24)[0]

    if not (1 <= channels <= 255):
        raise PlayerCacheError(f"invalid WAV channel count: {channels}")
    if sample_rate <= 0:
        raise PlayerCacheError(f"invalid WAV sample rate: {sample_rate}")
    if block_align <= 0:
        raise PlayerCacheError("invalid WAV block alignment")

    if format_tag == 1 and bits == 16 and block_align == channels * 2:
        sample_type: Literal["i16", "f32"] = "i16"
        encoding: Literal["rpkn", "rpkl"] = "rpkn"
        codec = "pcm_s16le"
    elif format_tag == 3 and bits == 32 and block_align == channels * 4:
        sample_type = "f32"
        encoding = "rpkl"
        codec = "pcm_f32le"
    else:
        raise UnsupportedWavError(
            "cache generation's built-in WAV path supports PCM16/float32 only; "
            f"got tag={format_tag}, bits={bits}, block_align={block_align}"
        )
    expected_byte_rate = sample_rate * block_align
    if byte_rate != expected_byte_rate:
        raise PlayerCacheError(
            f"inconsistent WAV byte rate: got={byte_rate}, expected={expected_byte_rate}"
        )

    pcm = b"".join(pcm_parts)
    if len(pcm) % block_align:
        raise PlayerCacheError("WAV data does not contain whole interleaved frames")
    return AudioCacheInput(
        sample_rate=sample_rate,
        channels=channels,
        frames=len(pcm) // block_align,
        sample_type=sample_type,
        pcm_bytes=pcm,
        source_codec=codec,
        source_container="wav",
        recommended_wave_encoding=encoding,
    )


def _resolve_tool(command: str | Path, name: str) -> str:
    value = os.fspath(command)
    if os.path.dirname(value):
        path = _resolved(value)
        if not path.is_file():
            raise PlayerCacheError(f"{name} executable not found: {path}")
        return str(path)
    found = shutil.which(value)
    if found is None:
        raise PlayerCacheError(f"{name} executable not found on PATH: {value}")
    return found


def _diagnostic_tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _MAX_TOOL_DIAGNOSTIC_BYTES))
            raw = handle.read(_MAX_TOOL_DIAGNOSTIC_BYTES)
    except OSError:
        return ""
    return raw.decode("utf-8", "replace").strip()


def _probe_with_ffprobe(
    path: Path,
    *,
    ffprobe: str | Path,
    timeout: float,
) -> dict[str, object]:
    executable = _resolve_tool(ffprobe, "ffprobe")
    command = [
        executable,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index,codec_name,sample_rate,channels,sample_fmt,bits_per_sample,bits_per_raw_sample:format=format_name,duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PlayerCacheError(f"ffprobe timed out after {timeout:g}s") from exc
    stderr = completed.stderr[-_MAX_TOOL_DIAGNOSTIC_BYTES :].decode(
        "utf-8", "replace"
    )
    if completed.returncode != 0:
        raise PlayerCacheError(
            f"ffprobe failed with exit {completed.returncode}: {stderr.strip()}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PlayerCacheError(f"ffprobe returned invalid JSON: {exc}") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
        raise PlayerCacheError("ffprobe found no audio stream")
    stream = streams[0]
    try:
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlayerCacheError("ffprobe audio stream lacks sample rate/channels") from exc
    if not (1 <= sample_rate <= 0xFFFF_FFFF) or not (1 <= channels <= 255):
        raise PlayerCacheError(
            f"ffprobe returned invalid audio geometry: {sample_rate} Hz, {channels} ch"
        )
    codec = str(stream.get("codec_name") or "").lower()
    format_info = payload.get("format")
    container = (
        str(format_info.get("format_name") or "").lower()
        if isinstance(format_info, dict)
        else ""
    )
    integer_lossless = codec in {"flac", "alac", "wavpack", "tta"} or (
        codec.startswith("pcm_") and "f32" not in codec and "f64" not in codec
    )
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "codec": codec,
        "container": container,
        "recommended_wave_encoding": "rpkn" if integer_lossless else "rpkl",
    }


def decode_with_ffmpeg(
    path: str | Path,
    *,
    ffmpeg: str | Path = "ffmpeg",
    ffprobe: str | Path = "ffprobe",
    timeout: float = DEFAULT_DECODE_TIMEOUT,
    max_decode_bytes: int = DEFAULT_MAX_DECODE_BYTES,
) -> AudioCacheInput:
    """Decode the first audio stream to deterministic interleaved f32le."""

    source = _resolved(path)
    if timeout <= 0:
        raise PlayerCacheError("decode timeout must be positive")
    if max_decode_bytes < 0:
        raise PlayerCacheError("max_decode_bytes must be non-negative")
    if not source.is_file():
        raise PlayerCacheError(f"audio file not found: {source}")
    probe = _probe_with_ffprobe(source, ffprobe=ffprobe, timeout=timeout)
    sample_rate = int(probe["sample_rate"])
    channels = int(probe["channels"])
    frame_bytes = channels * 4
    # Ask FFmpeg to stop one complete frame beyond our accepted maximum.  A
    # result larger than the limit is therefore distinguishable from a stream
    # whose natural size happens to equal the limit exactly.
    ffmpeg_limit = max_decode_bytes + frame_bytes
    executable = _resolve_tool(ffmpeg, "ffmpeg")

    with tempfile.TemporaryDirectory(prefix="libreapeaks-decode-") as directory:
        root = Path(directory)
        raw_path = root / "decoded.f32le"
        stderr_path = root / "ffmpeg.stderr"
        command = [
            executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "-fs",
            str(ffmpeg_limit),
            "-y",
            str(raw_path),
        ]
        try:
            with stderr_path.open("wb") as stderr_handle:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_handle,
                    check=False,
                    timeout=timeout,
                )
        except subprocess.TimeoutExpired as exc:
            raise PlayerCacheError(f"ffmpeg timed out after {timeout:g}s") from exc
        diagnostic = _diagnostic_tail(stderr_path)
        if completed.returncode != 0:
            raise PlayerCacheError(
                f"ffmpeg failed with exit {completed.returncode}: {diagnostic}"
            )
        try:
            size = raw_path.stat().st_size
        except OSError as exc:
            raise PlayerCacheError("ffmpeg produced no decoded audio") from exc
        if size > max_decode_bytes:
            raise PlayerCacheError(
                f"decoded audio exceeds max_decode_bytes={max_decode_bytes}"
            )
        if size % frame_bytes:
            raise PlayerCacheError(
                f"ffmpeg produced a partial interleaved frame: {size} bytes"
            )
        try:
            pcm = raw_path.read_bytes()
        except OSError as exc:
            raise PlayerCacheError(f"cannot read FFmpeg output: {exc}") from exc

    return AudioCacheInput(
        sample_rate=sample_rate,
        channels=channels,
        frames=len(pcm) // frame_bytes,
        sample_type="f32",
        pcm_bytes=pcm,
        source_codec=str(probe["codec"]),
        source_container=str(probe["container"]),
        recommended_wave_encoding=str(probe["recommended_wave_encoding"]),  # type: ignore[arg-type]
    )


def decode_audio_for_cache(
    path: str | Path,
    *,
    decoder: CacheDecoder = "auto",
    ffmpeg: str | Path = "ffmpeg",
    ffprobe: str | Path = "ffprobe",
    timeout: float = DEFAULT_DECODE_TIMEOUT,
    max_decode_bytes: int = DEFAULT_MAX_DECODE_BYTES,
) -> AudioCacheInput:
    if decoder == "wav":
        return read_wav_cache_input(path, max_decode_bytes=max_decode_bytes)
    if decoder == "ffmpeg":
        return decode_with_ffmpeg(
            path,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            timeout=timeout,
            max_decode_bytes=max_decode_bytes,
        )
    if decoder != "auto":
        raise PlayerCacheError(f"unknown cache decoder: {decoder}")
    try:
        return read_wav_cache_input(path, max_decode_bytes=max_decode_bytes)
    except UnsupportedWavError:
        return decode_with_ffmpeg(
            path,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            timeout=timeout,
            max_decode_bytes=max_decode_bytes,
        )


def _source_fingerprint(path: Path) -> tuple[int, int, int, int]:
    try:
        value = path.stat()
    except OSError as exc:
        raise PlayerCacheError(f"cannot stat source audio {path}: {exc}") from exc
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _validate_divisions(divisions: Sequence[int]) -> list[int]:
    values = [int(value) for value in divisions]
    if not values:
        raise PlayerCacheError("at least one waveform division is required")
    if any(value <= 0 or value > 0x7FFF_FFFF for value in values):
        raise PlayerCacheError("waveform divisions must be in 1..2147483647")
    return values


def _i16_to_f32le(raw: bytes) -> bytes:
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    floats = array.array("f", (sample / 32768.0 for sample in samples))
    if sys.byteorder != "little":
        floats.byteswap()
    return floats.tobytes()


def _auxiliary_path(target: Path, suffix: str) -> Path:
    candidate = target.with_name(target.name + suffix)
    if len(os.fsencode(candidate.name)) <= _path_name_max(target.parent):
        return candidate
    digest = hashlib.sha256(os.fsencode(target.name)).hexdigest()[:32]
    return target.parent / f".libreapeaks-{digest}{suffix}"


def _generate_cache_blob(
    source: AudioCacheInput,
    source_stat: os.stat_result,
    *,
    divisions: Sequence[int],
    spectral: bool,
    wave_encoding: WaveEncodingOption,
) -> bytes:
    selected = (
        source.recommended_wave_encoding
        if wave_encoding == "auto"
        else wave_encoding
    )
    if selected not in ("rpkn", "rpkl"):
        raise PlayerCacheError(f"unknown wave encoding: {wave_encoding}")
    if spectral and len(divisions) * 2 > 255:
        raise PlayerCacheError("too many divisions for wave+spectral .reapeaks layers")
    if not spectral and len(divisions) > 255:
        raise PlayerCacheError("too many divisions for .reapeaks layer table")
    source_mtime, source_size = _source_metadata(source_stat)
    kwargs = dict(
        sample_rate=source.sample_rate,
        channels=source.channels,
        divisions=list(divisions),
        source_mtime_low32=source_mtime,
        source_size_low32=source_size,
        spectral=spectral,
    )
    try:
        if source.sample_type == "i16" and selected == "rpkn":
            output = reapeaks.generate_pcm16(source.pcm_bytes, **kwargs)
        else:
            pcm_f32 = (
                source.pcm_bytes
                if source.sample_type == "f32"
                else _i16_to_f32le(source.pcm_bytes)
            )
            output = reapeaks.generate_f32(
                pcm_f32, large_range=(selected == "rpkl"), **kwargs
            )
    except Exception as exc:
        raise PlayerCacheError(f"libreapeaks cache generation failed: {exc}") from exc
    return bytes(output)


@contextmanager
def _exclusive_cache_lock(target: Path, timeout: float) -> Iterator[None]:
    if timeout <= 0:
        raise PlayerCacheError("lock_timeout must be positive")
    lock = _auxiliary_path(target, ".lock")
    deadline = time.monotonic() + timeout
    stale_after = max(30.0, timeout * 4.0)
    identity: tuple[int, int] | None = None
    while True:
        try:
            descriptor = os.open(
                lock,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                lock_stat = lock.lstat()
            except FileNotFoundError:
                continue
            if not stat_module.S_ISREG(lock_stat.st_mode):
                raise PlayerCacheError(f"cache lock is not a regular file: {lock}")
            if time.time() - lock_stat.st_mtime > stale_after:
                # Do not unlink a lock that was replaced between lstat() and
                # cleanup. This matters on heavily contended shared caches.
                stale_identity = (lock_stat.st_dev, lock_stat.st_ino)
                try:
                    current = lock.lstat()
                    if stale_identity == (current.st_dev, current.st_ino):
                        lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise PlayerCacheError(f"timed out waiting for cache lock: {lock}")
            time.sleep(0.05)
            continue
        except OSError as exc:
            raise PlayerCacheError(f"cannot create cache lock {lock}: {exc}") from exc
        try:
            payload = f"pid={os.getpid()}\ntime={time.time():.6f}\n".encode("ascii")
            os.write(descriptor, payload)
            os.fsync(descriptor)
            created = os.fstat(descriptor)
            identity = (created.st_dev, created.st_ino)
        finally:
            os.close(descriptor)
        break
    try:
        yield
    finally:
        try:
            current = lock.lstat()
            if identity == (current.st_dev, current.st_ino):
                lock.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(directory: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _publish_cache(target: Path, blob: bytes, audio: Path) -> None:
    temporary_path: Path | None = None
    try:
        digest = hashlib.sha256(os.fsencode(target.name)).hexdigest()[:16]
        descriptor, name = tempfile.mkstemp(
            prefix=f".libreapeaks-{digest}.", suffix=".tmp", dir=target.parent
        )
        temporary_path = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        inspection = inspect_reapeaks_cache(temporary_path, audio)
        if not inspection.parseable or not inspection.fresh:
            raise PlayerCacheError(
                f"generated cache failed validation before publication: {inspection.reason}"
            )
        os.replace(temporary_path, target)
        temporary_path = None
        _fsync_directory(target.parent)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def ensure_reapeaks(
    audio_path: str | Path,
    peaks_path: str | Path | None = None,
    *,
    rebuild: bool = False,
    spectral: bool = True,
    decoder: CacheDecoder = "auto",
    cache_mode: CacheMode = "auto",
    cache_directory: str | Path | None = None,
    reaper_cache_map: str | Path | None = None,
    allow_stale_cache: bool = False,
    ffmpeg: str | Path = "ffmpeg",
    ffprobe: str | Path = "ffprobe",
    decode_timeout: float = DEFAULT_DECODE_TIMEOUT,
    max_decode_bytes: int = DEFAULT_MAX_DECODE_BYTES,
    wave_encoding: WaveEncodingOption = "auto",
    divisions: Sequence[int] | None = None,
    fine_peaks_per_second: int = 300,
    lock_timeout: float = 30.0,
) -> tuple[Path, bool]:
    """Return a reusable cache or generate and atomically publish a new one."""

    audio = _resolved(audio_path)
    if not audio.is_file():
        raise PlayerCacheError(f"audio file not found: {audio}")
    if peaks_path is not None:
        explicit = _resolved(peaks_path)
        candidates = [explicit]
        target = explicit
    else:
        candidates = _read_candidates(
            audio, cache_mode, cache_directory, reaper_cache_map
        )
        target = _write_target(
            audio, cache_mode, cache_directory, reaper_cache_map
        )
    if target == audio:
        raise PlayerCacheError("cache target resolves to the source audio file")

    if not rebuild:
        for candidate in candidates:
            inspection = inspect_reapeaks_cache(candidate, audio)
            if inspection.parseable and (inspection.fresh or allow_stale_cache):
                return candidate, False

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PlayerCacheError(f"cannot create cache directory {target.parent}: {exc}") from exc

    with _exclusive_cache_lock(target, lock_timeout):
        # Another process may have completed while this process waited.
        if not rebuild:
            inspection = inspect_reapeaks_cache(target, audio)
            if inspection.parseable and (inspection.fresh or allow_stale_cache):
                return target, False

        before = _source_fingerprint(audio)
        try:
            source_stat = audio.stat()
        except OSError as exc:
            raise PlayerCacheError(f"cannot stat source audio {audio}: {exc}") from exc
        decoded = decode_audio_for_cache(
            audio,
            decoder=decoder,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            timeout=decode_timeout,
            max_decode_bytes=max_decode_bytes,
        )
        if not (1 <= decoded.channels <= 255) or decoded.sample_rate <= 0:
            raise PlayerCacheError("decoder returned invalid audio geometry")
        expected_bytes = decoded.frames * decoded.channels * (
            2 if decoded.sample_type == "i16" else 4
        )
        if expected_bytes != len(decoded.pcm_bytes):
            raise PlayerCacheError("decoder returned a partial interleaved frame buffer")
        selected_divisions = _validate_divisions(
            divisions
            if divisions is not None
            else reapeaks.default_divisions(
                decoded.sample_rate, max(1, int(fine_peaks_per_second))
            )
        )
        blob = _generate_cache_blob(
            decoded,
            source_stat,
            divisions=selected_divisions,
            spectral=spectral,
            wave_encoding=wave_encoding,
        )
        after = _source_fingerprint(audio)
        if before != after:
            raise PlayerCacheError(
                "source audio changed while its peak cache was being generated"
            )
        _publish_cache(target, blob, audio)
        return target, True


def available_spectral_levels(
    rp: object,
    levels: Sequence[tuple[int, int, bool]],
) -> list[tuple[int, int, int, int]]:
    """Return contiguous readable spectral layers for GUI display.

    Older bindings do not expose a spectral-layer count. Probe only tile zero
    for each native wave level; this also prevents wave-only caches from being
    advertised as spectral by the reference players. Each row is
    ``(layer_index, wave_level_index, division, wave_peak_count)``.
    """

    result: list[tuple[int, int, int, int]] = []
    native = [
        (level_index, int(division), int(peak_count))
        for level_index, (division, peak_count, is_native) in enumerate(levels)
        if is_native
    ]
    for layer_index, (level_index, division, peak_count) in enumerate(native):
        try:
            _first, width, _height, _raw = rp.spectral_tile_texture(layer_index, 0)  # type: ignore[attr-defined]
        except Exception:
            break
        if int(width) <= 0:
            break
        result.append((layer_index, level_index, division, peak_count))
    return result


def exact_audio_frames(audio_path: str | Path, sample_rate: int) -> int | None:
    """Return exact frames for a natively supported WAV, otherwise ``None``."""

    try:
        info = read_wav_cache_input(audio_path)
    except (OSError, PlayerCacheError):
        return None
    if info.sample_rate != sample_rate:
        return None
    return info.frames


def _write_float_wav(path: Path, decoded: AudioCacheInput) -> None:
    if decoded.sample_type != "f32":
        raise PlayerCacheError("temporary playback WAV requires float32 samples")
    block_align = decoded.channels * 4
    data_size = len(decoded.pcm_bytes)
    riff_size = 4 + (8 + 16) + (8 + data_size)
    if riff_size > 0xFFFF_FFFF:
        raise PlayerCacheError("decoded playback WAV exceeds RIFF's 4 GiB size limit")
    fmt = struct.pack(
        "<HHIIHH",
        3,
        decoded.channels,
        decoded.sample_rate,
        decoded.sample_rate * block_align,
        block_align,
        32,
    )
    try:
        with path.open("wb") as handle:
            handle.write(b"RIFF")
            handle.write(struct.pack("<I", riff_size))
            handle.write(b"WAVE")
            handle.write(b"fmt ")
            handle.write(struct.pack("<I", len(fmt)))
            handle.write(fmt)
            handle.write(b"data")
            handle.write(struct.pack("<I", data_size))
            handle.write(decoded.pcm_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise PlayerCacheError(f"cannot write temporary playback WAV: {exc}") from exc


def prepare_playback_audio(
    audio_path: str | Path,
    *,
    decoder: PlaybackDecoder = "native",
    ffmpeg: str | Path = "ffmpeg",
    ffprobe: str | Path = "ffprobe",
    timeout: float = DEFAULT_DECODE_TIMEOUT,
    max_decode_bytes: int = DEFAULT_MAX_DECODE_BYTES,
) -> PreparedPlayback:
    audio = _resolved(audio_path)
    if decoder == "native":
        if not audio.is_file():
            raise PlayerCacheError(f"audio file not found: {audio}")
        return PreparedPlayback(audio, "native")
    if decoder != "ffmpeg":
        raise PlayerCacheError(f"unknown playback decoder: {decoder}")
    decoded = decode_with_ffmpeg(
        audio,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        timeout=timeout,
        max_decode_bytes=max_decode_bytes,
    )
    temporary = tempfile.TemporaryDirectory(prefix="libreapeaks-playback-")
    path = Path(temporary.name) / "decoded-f32.wav"
    try:
        _write_float_wav(path, decoded)
    except Exception:
        temporary.cleanup()
        raise
    return PreparedPlayback(path, "ffmpeg", temporary)


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, sec = divmod(seconds, 60.0)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{sec:05.2f}"
    return f"{minutes:02d}:{sec:05.2f}"
