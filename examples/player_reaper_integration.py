"""Resolve demo-player cache policy from CLI arguments and REAPER settings."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal

from player_common import (
    CacheMode,
    PlayerCacheError,
    central_peak_path,
    find_existing_peaks,
    inspect_reapeaks_cache,
    sidecar_peak_path,
    subdir_peak_path,
)
from reaper_config import (
    ReaperConfigError,
    ReaperPeakPaths,
    ReaperPeakSettings,
    resolve_exact_reaper_peak_paths,
    resolve_peak_rate,
    validate_central_peak_paths,
)

ApplicationCacheMode = Literal[
    "auto",
    "sidecar",
    "subdir",
    "central",
    "reaper",
    "private-central",
]
APPLICATION_CACHE_MODES: tuple[str, ...] = (
    "auto",
    "sidecar",
    "subdir",
    "central",
    "reaper",
    "private-central",
)


@dataclass(frozen=True)
class PlayerPeakPolicy:
    """A fully resolved target suitable for ``ensure_reapeaks``."""

    peaks_path: Path
    peak_rate: int
    cache_mode: CacheMode
    cache_directory: Path | None
    settings: ReaperPeakSettings | None
    path_origin: str
    canonical_paths: ReaperPeakPaths | None = None


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _within(path: Path, directory: Path) -> bool:
    path_s = os.path.normcase(str(path.resolve(strict=False)))
    directory_s = os.path.normcase(str(directory.resolve(strict=False)))
    try:
        return os.path.commonpath([path_s, directory_s]) == directory_s
    except ValueError:
        return False


def _select_canonical_target(
    audio: Path,
    paths: ReaperPeakPaths,
    *,
    allow_stale_cache: bool,
) -> Path:
    if paths.read is not None and paths.read.is_file():
        inspection = inspect_reapeaks_cache(paths.read, audio)
        if inspection.parseable and (inspection.fresh or allow_stale_cache):
            return paths.read
    return paths.write


def resolve_player_peak_policy(
    audio_path: str | Path,
    *,
    explicit_peaks: str | Path | None = None,
    cache_mode: ApplicationCacheMode = "auto",
    cache_directory: str | Path | None = None,
    reaper_cache_map: str | Path | None = None,
    reaper_ini: str | Path | None = None,
    auto_reaper_ini: bool = False,
    reaper_executable: str | Path | None = None,
    explicit_peak_rate: int | None = None,
    allow_stale_cache: bool = False,
    query_timeout: float = 30.0,
) -> PlayerPeakPolicy:
    """Apply application-layer precedence and choose one unambiguous target.

    ``central`` means REAPER-compatible central storage and fails closed when
    no canonical path resolver is available. The old libreapeaks SHA-256
    namespace remains available as ``private-central``.
    """

    if cache_mode not in APPLICATION_CACHE_MODES:
        raise ReaperConfigError(f"unknown cache mode: {cache_mode}")

    audio = _resolved(audio_path)
    effective = resolve_peak_rate(
        explicit_peak_rate=explicit_peak_rate,
        reaper_ini=reaper_ini,
        auto_reaper_ini=auto_reaper_ini,
        reaper_executable=reaper_executable,
    )
    settings = effective.settings
    directory = _resolved(cache_directory) if cache_directory is not None else None

    if explicit_peaks is not None:
        return PlayerPeakPolicy(
            peaks_path=_resolved(explicit_peaks),
            peak_rate=effective.peak_rate,
            cache_mode="sidecar",
            cache_directory=None,
            settings=settings,
            path_origin="explicit",
        )

    if cache_mode == "sidecar":
        return PlayerPeakPolicy(
            _resolved(sidecar_peak_path(audio)),
            effective.peak_rate,
            "sidecar",
            None,
            settings,
            "sidecar",
        )

    if cache_mode == "subdir":
        return PlayerPeakPolicy(
            _resolved(subdir_peak_path(audio)),
            effective.peak_rate,
            "subdir",
            None,
            settings,
            "subdir",
        )

    if cache_mode == "private-central":
        if directory is None:
            raise ReaperConfigError(
                "cache_mode=private-central requires --cache-dir"
            )
        return PlayerPeakPolicy(
            central_peak_path(audio, directory),
            effective.peak_rate,
            "central",
            directory,
            settings,
            "libreapeaks-private-sha256",
        )

    can_query = reaper_cache_map is not None or reaper_executable is not None
    if cache_mode in ("central", "reaper") or can_query:
        if not can_query:
            raise ReaperConfigError(
                f"cache_mode={cache_mode} requires --reaper-cache-map or "
                "--reaper-executable; this protects against writing a "
                "REAPER-incompatible central filename"
            )
        paths = resolve_exact_reaper_peak_paths(
            audio,
            cache_map=reaper_cache_map,
            reaper_executable=reaper_executable,
            reaper_ini=None if settings is None else settings.ini_path,
            timeout=query_timeout,
        )
        if cache_mode == "central":
            if settings is not None:
                paths = validate_central_peak_paths(paths, settings)
                directory = settings.alternate_cache_path
            elif directory is not None:
                if not _within(paths.write, directory):
                    raise ReaperConfigError(
                        "canonical REAPER write path is outside --cache-dir: "
                        f"{paths.write}"
                    )
                if paths.read is not None and not _within(paths.read, directory):
                    raise ReaperConfigError(
                        "canonical REAPER read path is outside --cache-dir: "
                        f"{paths.read}"
                    )
            else:
                raise ReaperConfigError(
                    "cache_mode=central also requires --reaper-ini or "
                    "--cache-dir so the returned path can be validated"
                )
        target = _select_canonical_target(
            audio, paths, allow_stale_cache=allow_stale_cache
        )
        return PlayerPeakPolicy(
            target,
            effective.peak_rate,
            "sidecar",
            directory,
            settings,
            paths.origin,
            paths,
        )

    if (
        cache_mode == "auto"
        and settings is not None
        and settings.altpeaks_flags != 0
    ):
        raise ReaperConfigError(
            "REAPER.ini enables a non-default peak path but no exact path "
            "resolver is available; pass --reaper-executable, "
            "--reaper-cache-map, or choose --cache-mode private-central"
        )

    if cache_mode == "auto":
        existing = find_existing_peaks(
            audio,
            cache_mode="auto",
            cache_directory=directory,
            reaper_cache_map=None,
        )
        target = existing or _resolved(sidecar_peak_path(audio))
        return PlayerPeakPolicy(
            target,
            effective.peak_rate,
            "sidecar",
            directory,
            settings,
            "existing" if existing is not None else "sidecar-fallback",
        )

    raise PlayerCacheError(f"unhandled cache mode: {cache_mode}")
