"""Persistent cache-placement settings shared by the reference demo apps.

The default policy is deliberately standalone and unsurprising: write a
``.reapeaks`` sidecar beside the source media.  REAPER-compatible placement is
opt-in.  In particular, the recovered central-cache filename can be derived
without launching REAPER; ``GetPeakFileNameEx`` remains an optional oracle for
verification and for REAPER path-policy cases that are not safely reproduced
locally.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Literal, Mapping

from player_common import reaper_mapped_peak_path, sidecar_peak_path, subdir_peak_path
from reaper_config import (
    DEFAULT_PEAK_RATE,
    ReaperConfigError,
    discover_reaper_ini,
    load_reaper_ini,
    resolve_exact_reaper_peak_paths,
)


DemoCachePolicy = Literal[
    "sidecar",
    "subdir",
    "reaper-central",
    "reaper-config",
    "private-central",
]
DEMO_CACHE_POLICIES: tuple[str, ...] = (
    "sidecar",
    "subdir",
    "reaper-central",
    "reaper-config",
    "private-central",
)
VISIBLE_DEMO_CACHE_POLICIES: tuple[str, ...] = (
    "sidecar",
    "subdir",
    "reaper-central",
    "reaper-config",
)
CONFIG_VERSION = 1
ENV_CONFIG_PATH = "LIBREAPEAKS_DEMO_CONFIG"


class DemoConfigError(ValueError):
    """Invalid persistent demo configuration or unresolved cache policy."""


@dataclass(frozen=True)
class DemoCacheConfig:
    version: int = CONFIG_VERSION
    policy: DemoCachePolicy = "sidecar"
    cache_directory: str = ""
    reaper_ini: str = ""
    auto_reaper_ini: bool = False
    verify_with_reaper: bool = False
    reaper_executable: str = ""
    peak_rate: int | None = None


@dataclass(frozen=True)
class ResolvedDemoCachePlan:
    peaks_path: Path
    peak_rate: int
    policy: DemoCachePolicy
    path_origin: str
    reaper_ini: Path | None = None


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def demo_config_path() -> Path:
    override = os.environ.get(ENV_CONFIG_PATH)
    if override:
        return _resolved(override)
    home = Path.home()
    if sys.platform.startswith("win"):
        root = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        root = home / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return root / "libreapeaks" / "demo-config.json"


def _optional_string(value: object, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DemoConfigError(f"{name} must be a string")
    return value.strip()


def config_from_mapping(payload: Mapping[str, object]) -> DemoCacheConfig:
    raw_cache = payload.get("cache", payload)
    if not isinstance(raw_cache, Mapping):
        raise DemoConfigError("demo config cache section must be an object")
    version = raw_cache.get("version", payload.get("version", CONFIG_VERSION))
    if version != CONFIG_VERSION:
        raise DemoConfigError(f"unsupported demo config version: {version!r}")
    policy = raw_cache.get("policy", "sidecar")
    if not isinstance(policy, str) or policy not in DEMO_CACHE_POLICIES:
        raise DemoConfigError(f"unknown demo cache policy: {policy!r}")
    peak_rate = raw_cache.get("peak_rate")
    if peak_rate in (None, ""):
        parsed_peak_rate = None
    elif isinstance(peak_rate, bool) or not isinstance(peak_rate, int):
        raise DemoConfigError("peak_rate must be an integer or null")
    elif peak_rate <= 0:
        raise DemoConfigError("peak_rate must be positive")
    else:
        parsed_peak_rate = peak_rate
    return DemoCacheConfig(
        policy=policy,  # type: ignore[arg-type]
        cache_directory=_optional_string(raw_cache.get("cache_directory", ""), "cache_directory"),
        reaper_ini=_optional_string(raw_cache.get("reaper_ini", ""), "reaper_ini"),
        auto_reaper_ini=bool(raw_cache.get("auto_reaper_ini", False)),
        verify_with_reaper=bool(raw_cache.get("verify_with_reaper", False)),
        reaper_executable=_optional_string(raw_cache.get("reaper_executable", ""), "reaper_executable"),
        peak_rate=parsed_peak_rate,
    )


def load_demo_cache_config(path: str | Path | None = None) -> DemoCacheConfig:
    config_path = demo_config_path() if path is None else _resolved(path)
    if not config_path.is_file():
        return DemoCacheConfig()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoConfigError(f"cannot read demo config {config_path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise DemoConfigError("demo config root must be an object")
    return config_from_mapping(payload)


def save_demo_cache_config(
    config: DemoCacheConfig, path: str | Path | None = None
) -> Path:
    config_path = demo_config_path() if path is None else _resolved(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CONFIG_VERSION, "cache": asdict(config)}
    payload["cache"].pop("version", None)
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, config_path)
    return config_path


def reaper_central_peak_path(
    audio_path: str | Path, cache_directory: str | Path
) -> Path:
    """Derive the recovered REAPER central-cache filename without REAPER.

    The central-cache probe validates the REAPER 7.79 scheme as SHA-1 of the
    lower-cased UTF-8 source path, stored below a two-hex-character shard.
    ``GetPeakFileNameEx`` can still be enabled as an optional verifier.
    """

    source = str(_resolved(audio_path))
    digest = hashlib.sha1(source.lower().encode("utf-8")).hexdigest()
    return _resolved(cache_directory) / digest[:2] / f"{digest}.reapeaks"


def _within(path: Path, parent: Path) -> bool:
    path_s = os.path.normcase(str(path.resolve(strict=False)))
    parent_s = os.path.normcase(str(parent.resolve(strict=False)))
    try:
        return os.path.commonpath([path_s, parent_s]) == parent_s
    except ValueError:
        return False


def _matches_alternate_source(audio: Path, candidates: tuple[Path, ...]) -> bool:
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if audio == resolved or _within(audio, resolved):
            return True
    return False


def _selected_ini(config: DemoCacheConfig) -> Path | None:
    if config.reaper_ini:
        path = _resolved(config.reaper_ini)
        if not path.is_file():
            raise DemoConfigError(f"REAPER.ini not found: {path}")
        return path
    if config.auto_reaper_ini:
        executable = config.reaper_executable or None
        return discover_reaper_ini(executable=executable)
    return None


def _rate(config: DemoCacheConfig, ini_path: Path | None, explicit: int | None) -> int:
    if explicit is not None:
        if explicit <= 0:
            raise DemoConfigError("fine peak rate must be positive")
        return explicit
    if config.peak_rate is not None:
        return config.peak_rate
    if ini_path is not None:
        return load_reaper_ini(ini_path).peak_rate
    return DEFAULT_PEAK_RATE


def _oracle_path(
    audio: Path,
    *,
    cache_map: str | Path | None,
    executable: str | Path | None,
    ini_path: Path | None,
) -> Path:
    paths = resolve_exact_reaper_peak_paths(
        audio,
        cache_map=cache_map,
        reaper_executable=executable,
        reaper_ini=ini_path,
    )
    return paths.write


def resolve_demo_cache_plan(
    audio_path: str | Path,
    config: DemoCacheConfig,
    *,
    explicit_peaks: str | Path | None = None,
    legacy_cache_mode: str | None = None,
    legacy_cache_directory: str | Path | None = None,
    reaper_cache_map: str | Path | None = None,
    reaper_executable: str | Path | None = None,
    explicit_peak_rate: int | None = None,
) -> ResolvedDemoCachePlan:
    """Resolve one cache target with CLI/legacy overrides ahead of saved config."""

    audio = _resolved(audio_path)
    policy: DemoCachePolicy = config.policy
    cache_directory = config.cache_directory

    # Preserve the existing CLI spellings while moving the GUI/default policy
    # to the clearer persistent names. ``auto`` means "use saved config".
    if legacy_cache_mode and legacy_cache_mode != "auto":
        if legacy_cache_mode in ("sidecar", "subdir"):
            policy = legacy_cache_mode  # type: ignore[assignment]
        elif legacy_cache_mode == "central":
            policy = "reaper-central"
        elif legacy_cache_mode == "reaper":
            ini_path = _selected_ini(config)
            rate = _rate(config, ini_path, explicit_peak_rate)
            if reaper_cache_map is None and reaper_executable is None:
                raise DemoConfigError(
                    "cache_mode=reaper requires --reaper-cache-map or a REAPER executable"
                )
            target = _oracle_path(
                audio,
                cache_map=reaper_cache_map,
                executable=reaper_executable or config.reaper_executable or None,
                ini_path=ini_path,
            )
            return ResolvedDemoCachePlan(target, rate, "reaper-config", "reaper-oracle", ini_path)
        else:
            raise DemoConfigError(f"unknown cache mode: {legacy_cache_mode}")
    if legacy_cache_directory is not None:
        cache_directory = str(_resolved(legacy_cache_directory))

    ini_path = _selected_ini(config) if policy == "reaper-config" else None
    rate = _rate(config, ini_path, explicit_peak_rate)
    if explicit_peaks is not None:
        return ResolvedDemoCachePlan(
            _resolved(explicit_peaks), rate, policy, "explicit", ini_path
        )

    if policy == "sidecar":
        return ResolvedDemoCachePlan(
            _resolved(sidecar_peak_path(audio)), rate, policy, "sidecar", None
        )
    if policy == "subdir":
        return ResolvedDemoCachePlan(
            _resolved(subdir_peak_path(audio)), rate, policy, "subdir", None
        )
    if policy == "private-central":
        if not cache_directory:
            raise DemoConfigError("private-central requires a cache directory")
        # Keep the existing private namespace accessible only as an advanced
        # compatibility setting. The public GUI does not expose it.
        from player_common import central_peak_path

        return ResolvedDemoCachePlan(
            central_peak_path(audio, cache_directory),
            rate,
            policy,
            "libreapeaks-private-sha256",
            None,
        )
    if policy == "reaper-central":
        if not cache_directory:
            raise DemoConfigError("REAPER central cache requires a cache directory")
        derived = reaper_central_peak_path(audio, cache_directory)
        verifier = reaper_executable or config.reaper_executable or None
        if config.verify_with_reaper and (verifier is not None or reaper_cache_map is not None):
            check_ini = _selected_ini(config)
            exact = _oracle_path(
                audio,
                cache_map=reaper_cache_map,
                executable=verifier,
                ini_path=check_ini,
            )
            if exact != derived:
                return ResolvedDemoCachePlan(
                    exact, rate, policy, "reaper-oracle-override", check_ini
                )
            return ResolvedDemoCachePlan(
                derived, rate, policy, "reaper-central-sha1-verified", check_ini
            )
        return ResolvedDemoCachePlan(
            derived, rate, policy, "reaper-central-sha1", None
        )

    if policy != "reaper-config":
        raise DemoConfigError(f"unhandled cache policy: {policy}")
    if ini_path is None:
        raise DemoConfigError(
            "Follow REAPER settings requires a REAPER.ini path or Auto-detect REAPER.ini"
        )
    settings = load_reaper_ini(ini_path)

    # The recovered offline policy covers the common REAPER choices: bit 0
    # selects the alternate central cache globally, while altpeaksopathlist can
    # select source trees individually. Other non-default flag combinations are
    # deliberately delegated to the oracle rather than guessed.
    selected_by_list = _matches_alternate_source(audio, settings.alternate_source_paths)
    if settings.altpeaks_flags & 1 or selected_by_list:
        if settings.alternate_cache_path is None:
            raise DemoConfigError(
                "REAPER.ini selects alternate peak storage but altpeakspath is empty"
            )
        derived = reaper_central_peak_path(audio, settings.alternate_cache_path)
        origin = "reaper.ini-central-sha1"
    elif settings.altpeaks_flags == 0:
        derived = _resolved(sidecar_peak_path(audio))
        origin = "reaper.ini-sidecar"
    else:
        verifier = reaper_executable or config.reaper_executable or None
        if verifier is None and reaper_cache_map is None:
            raise DemoConfigError(
                "REAPER.ini uses an alternate peak-path flag combination that the "
                "offline resolver does not reproduce; provide a REAPER executable "
                "or --reaper-cache-map to resolve this source exactly"
            )
        exact = _oracle_path(
            audio,
            cache_map=reaper_cache_map,
            executable=verifier,
            ini_path=ini_path,
        )
        return ResolvedDemoCachePlan(
            exact, rate, policy, "reaper-oracle-unknown-flags", ini_path
        )

    verifier = reaper_executable or config.reaper_executable or None
    if config.verify_with_reaper and (verifier is not None or reaper_cache_map is not None):
        exact = _oracle_path(
            audio,
            cache_map=reaper_cache_map,
            executable=verifier,
            ini_path=ini_path,
        )
        if exact != derived:
            return ResolvedDemoCachePlan(
                exact, rate, policy, "reaper-oracle-override", ini_path
            )
        origin += "-verified"
    return ResolvedDemoCachePlan(derived, rate, policy, origin, ini_path)


def resolve_worker_options(
    audio_path: str | Path, options: Mapping[str, object]
) -> tuple[dict[str, object], ResolvedDemoCachePlan]:
    """Turn existing player-worker options into an explicit resolved target."""

    result = dict(options)
    try:
        config = load_demo_cache_config()
    except DemoConfigError:
        # A malformed persisted config should be visible to the GUI/worker, not
        # silently reset to a different write location.
        raise
    legacy_mode = str(result.get("cache_mode") or "auto")
    explicit_rate = result.get("fine_peaks_per_second")
    # Existing demo CLIs historically used 300 as their parser default. Treat
    # that value as non-explicit while in auto mode so saved config / REAPER.ini
    # can supply peakcachegenrs. Explicit non-auto CLI modes retain it.
    if legacy_mode == "auto" and explicit_rate == DEFAULT_PEAK_RATE:
        explicit_rate = None
    plan = resolve_demo_cache_plan(
        audio_path,
        config,
        explicit_peaks=result.get("peaks_path"),
        legacy_cache_mode=legacy_mode,
        legacy_cache_directory=result.get("cache_directory"),
        reaper_cache_map=result.get("reaper_cache_map"),
        explicit_peak_rate=(
            int(explicit_rate) if explicit_rate is not None else None
        ),
    )
    result["peaks_path"] = plan.peaks_path
    result["cache_mode"] = "sidecar"
    result["cache_directory"] = None
    result["reaper_cache_map"] = None
    result["fine_peaks_per_second"] = plan.peak_rate
    return result, plan
