"""REAPER-native cache generation helper for the interactive demo player.

This module deliberately sits at the application layer.  It keeps the existing
path/locking/decoder policy in :mod:`player_common`, but selects one of the three
cache shapes observed from REAPER 7.79 and reports coarse progress stages for a
responsive GUI worker thread.
"""
from __future__ import annotations

import os
from pathlib import Path
import struct
from typing import Callable, Literal, Sequence

import reapeaks
from player_common import (
    CacheDecoder,
    CacheMode,
    DEFAULT_DECODE_TIMEOUT,
    DEFAULT_MAX_DECODE_BYTES,
    PlayerCacheError,
    WaveEncodingOption,
    _exclusive_cache_lock,
    _i16_to_f32le,
    _publish_cache,
    _read_candidates,
    _resolved,
    _source_fingerprint,
    _source_metadata,
    _validate_divisions,
    _write_target,
    decode_audio_for_cache,
    inspect_reapeaks_cache,
)

NativeGenerationMode = Literal["waveform", "spectral", "spectrogram"]
ProgressCallback = Callable[[str, int], None]


def _progress(callback: ProgressCallback | None, stage: str, value: int) -> None:
    if callback is not None:
        callback(stage, max(0, min(100, int(value))))


def _validate_mode(mode: str) -> NativeGenerationMode:
    if mode not in ("waveform", "spectral", "spectrogram"):
        raise PlayerCacheError(
            "generation_mode must be waveform, spectral, or spectrogram"
        )
    return mode  # type: ignore[return-value]


def _layer_tokens(path: Path) -> list[int]:
    try:
        with path.open("rb") as handle:
            header = handle.read(18)
            if len(header) != 18:
                return []
            count = header[5]
            table = handle.read(count * 8)
    except OSError:
        return []
    if len(table) != count * 8:
        return []
    return [struct.unpack_from("<iI", table, index * 8)[0] for index in range(count)]


def cache_matches_native_mode(path: str | Path, mode: NativeGenerationMode) -> bool:
    """Return whether a cache has exactly the selected REAPER-native layer shape."""

    mode = _validate_mode(mode)
    tokens = _layer_tokens(_resolved(path))
    if not tokens:
        return False
    positives = [token for token in tokens if token > 0]
    if not positives or tokens[: len(positives)] != positives:
        return False
    count = len(positives)
    if mode == "waveform":
        expected = positives
    elif mode == "spectral":
        expected = positives + [-115] * count + [-114] * max(0, count - 1)
    else:
        expected = (
            positives
            + [-115] * count
            + [-103] * max(0, count - 1)
            + [-114] * max(0, count - 1)
        )
    return tokens == expected


def _generate_native_blob(
    source,
    source_stat: os.stat_result,
    *,
    divisions: Sequence[int],
    generation_mode: NativeGenerationMode,
    wave_encoding: WaveEncodingOption,
) -> bytes:
    mode = _validate_mode(generation_mode)
    selected = (
        source.recommended_wave_encoding
        if wave_encoding == "auto"
        else wave_encoding
    )
    if selected not in ("rpkn", "rpkl"):
        raise PlayerCacheError(f"unknown wave encoding: {wave_encoding}")

    division_count = len(divisions)
    if mode == "waveform":
        layer_count = division_count
    elif mode == "spectral":
        if division_count < 2:
            raise PlayerCacheError("spectral mode requires at least two divisions")
        layer_count = division_count * 3 - 1
    else:
        if division_count < 2:
            raise PlayerCacheError("spectrogram mode requires at least two divisions")
        layer_count = division_count * 4 - 2
    if layer_count > 255:
        raise PlayerCacheError(
            f"{mode} mode would exceed the .reapeaks 255-layer header limit"
        )

    if mode == "spectrogram" and not (
        source.sample_type == "i16" and selected == "rpkn"
    ):
        raise PlayerCacheError(
            "exact spectrogram mode currently requires PCM16 input with RPKN "
            "wave encoding; use a PCM16 WAV/decoder path or select spectral mode"
        )

    source_mtime, source_size = _source_metadata(source_stat)
    kwargs = dict(
        sample_rate=source.sample_rate,
        channels=source.channels,
        divisions=list(divisions),
        mode=mode,
        source_mtime_low32=source_mtime,
        source_size_low32=source_size,
    )
    try:
        if source.sample_type == "i16" and selected == "rpkn":
            output = reapeaks.generate_pcm16_reaper(source.pcm_bytes, **kwargs)
        else:
            pcm_f32 = (
                source.pcm_bytes
                if source.sample_type == "f32"
                else _i16_to_f32le(source.pcm_bytes)
            )
            output = reapeaks.generate_f32_reaper(
                pcm_f32,
                large_range=(selected == "rpkl"),
                **kwargs,
            )
    except Exception as exc:
        raise PlayerCacheError(f"libreapeaks cache generation failed: {exc}") from exc
    return bytes(output)


def ensure_reapeaks_native(
    audio_path: str | Path,
    peaks_path: str | Path | None = None,
    *,
    generation_mode: NativeGenerationMode = "spectral",
    rebuild: bool = False,
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
    progress: ProgressCallback | None = None,
) -> tuple[Path, bool]:
    """Reuse or generate exactly one of REAPER 7.79's observed cache shapes."""

    mode = _validate_mode(generation_mode)
    _progress(progress, "Checking cache", 0)
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

    def reusable(candidate: Path) -> bool:
        inspection = inspect_reapeaks_cache(candidate, audio)
        return bool(
            inspection.parseable
            and (inspection.fresh or allow_stale_cache)
            and cache_matches_native_mode(candidate, mode)
        )

    if not rebuild:
        for candidate in candidates:
            if reusable(candidate):
                _progress(progress, "Reusing matching cache", 100)
                return candidate, False

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PlayerCacheError(f"cannot create cache directory {target.parent}: {exc}") from exc

    _progress(progress, "Waiting for cache lock", 8)
    with _exclusive_cache_lock(target, lock_timeout):
        if not rebuild and reusable(target):
            _progress(progress, "Reusing matching cache", 100)
            return target, False

        before = _source_fingerprint(audio)
        try:
            source_stat = audio.stat()
        except OSError as exc:
            raise PlayerCacheError(f"cannot stat source audio {audio}: {exc}") from exc

        _progress(progress, "Decoding source audio", 18)
        decoded = decode_audio_for_cache(
            audio,
            decoder=decoder,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            timeout=decode_timeout,
            max_decode_bytes=max_decode_bytes,
        )
        _progress(
            progress,
            f"Decoded {decoded.frames:,} frames ({decoded.sample_type})",
            42,
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
        _progress(progress, f"Generating {mode} cache", 50)
        blob = _generate_native_blob(
            decoded,
            source_stat,
            divisions=selected_divisions,
            generation_mode=mode,
            wave_encoding=wave_encoding,
        )
        _progress(progress, "Validating generated cache", 88)

        after = _source_fingerprint(audio)
        if before != after:
            raise PlayerCacheError(
                "source audio changed while its peak cache was being generated"
            )
        _progress(progress, "Publishing cache atomically", 95)
        _publish_cache(target, blob, audio)
        if not cache_matches_native_mode(target, mode):
            raise PlayerCacheError(
                "published cache does not match the requested REAPER-native mode"
            )
        _progress(progress, "Cache ready", 100)
        return target, True
