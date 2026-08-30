"""Shared helpers for the libreapeaks GUI player demos.

The demos can open an existing REAPER-generated ``.reapeaks`` file.  For
uncompressed WAV sources they can also build a compatible cache themselves so
that ``generate_pcm16()``, ``generate_f32()`` and ``default_divisions()`` are
exercised by the reference applications.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Optional

import reapeaks


@dataclass(frozen=True)
class WavCacheInput:
    sample_rate: int
    channels: int
    frames: int
    sample_type: str  # "i16" or "f32"
    pcm_bytes: bytes


def peak_path_for_audio(audio_path: str | Path) -> Path:
    audio = Path(audio_path)
    return audio.with_name(audio.name + ".reapeaks")


def find_existing_peaks(audio_path: str | Path) -> Optional[Path]:
    audio = Path(audio_path)
    candidates = [
        peak_path_for_audio(audio),
        audio.with_name(audio.name + ".ReaPeaks"),
        audio.parent / "peaks" / (audio.name + ".reapeaks"),
        audio.parent / "peaks" / (audio.name + ".ReaPeaks"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def read_wav_cache_input(path: str | Path) -> WavCacheInput:
    """Read PCM16 or IEEE-float32 WAV without third-party decoder packages."""
    path = Path(path)
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("cache auto-generation currently supports RIFF/WAVE only")

    fmt: bytes | None = None
    pcm: bytes | None = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        start = pos + 8
        end = start + size
        if end > len(data):
            raise ValueError("truncated WAV chunk")
        if chunk_id == b"fmt ":
            fmt = data[start:end]
        elif chunk_id == b"data":
            pcm = data[start:end]
        pos = end + (size & 1)

    if fmt is None or pcm is None or len(fmt) < 16:
        raise ValueError("WAV is missing fmt/data chunks")

    format_tag, channels, sample_rate, _byte_rate, block_align, bits = struct.unpack_from(
        "<HHIIHH", fmt, 0
    )

    # WAVE_FORMAT_EXTENSIBLE stores the original format tag in the first two
    # bytes of the SubFormat GUID.
    if format_tag == 0xFFFE:
        if len(fmt) < 40:
            raise ValueError("truncated WAVE_FORMAT_EXTENSIBLE fmt chunk")
        format_tag = struct.unpack_from("<H", fmt, 24)[0]

    if channels <= 0 or block_align <= 0 or len(pcm) % block_align:
        raise ValueError("invalid WAV channel/block alignment")
    frames = len(pcm) // block_align

    if format_tag == 1 and bits == 16 and block_align == channels * 2:
        sample_type = "i16"
    elif format_tag == 3 and bits == 32 and block_align == channels * 4:
        sample_type = "f32"
    else:
        raise ValueError(
            f"cache auto-generation supports PCM16/float32 WAV; got tag={format_tag}, bits={bits}"
        )

    return WavCacheInput(sample_rate, channels, frames, sample_type, pcm)


def ensure_reapeaks(
    audio_path: str | Path,
    peaks_path: str | Path | None = None,
    *,
    rebuild: bool = False,
    spectral: bool = True,
) -> tuple[Path, bool]:
    """Return a cache path, generating it for PCM16/float32 WAV if necessary.

    Returns ``(path, generated_now)``.
    """
    audio = Path(audio_path).resolve()
    if peaks_path is None:
        existing = find_existing_peaks(audio)
        target = existing or peak_path_for_audio(audio)
    else:
        target = Path(peaks_path).resolve()

    if target.is_file() and not rebuild:
        return target, False

    source = read_wav_cache_input(audio)
    divisions = reapeaks.default_divisions(source.sample_rate)
    stat = audio.stat()
    kwargs = dict(
        sample_rate=source.sample_rate,
        channels=source.channels,
        divisions=divisions,
        source_mtime_low32=int(stat.st_mtime) & 0xFFFF_FFFF,
        source_size_low32=stat.st_size & 0xFFFF_FFFF,
        spectral=spectral,
    )
    if source.sample_type == "i16":
        blob = reapeaks.generate_pcm16(source.pcm_bytes, **kwargs)
    else:
        # REAPER 7.79 uses RPKL for floating-point media; large_range=True
        # selects that cache encoding while spectral payload generation is shared.
        blob = reapeaks.generate_f32(source.pcm_bytes, large_range=True, **kwargs)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(blob))
    return target, True


def exact_audio_frames(audio_path: str | Path, sample_rate: int) -> int | None:
    """Return exact frames for supported WAV, otherwise None.

    Playback backends report duration asynchronously; this helper lets the demos
    have an exact initial viewport for common uncompressed test media.
    """
    try:
        info = read_wav_cache_input(audio_path)
    except (OSError, ValueError):
        return None
    if info.sample_rate != sample_rate:
        return None
    return info.frames


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, sec = divmod(seconds, 60.0)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{sec:05.2f}"
    return f"{minutes:02d}:{sec:05.2f}"
