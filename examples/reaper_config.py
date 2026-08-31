"""Read REAPER peak-cache preferences and resolve canonical cache paths.

This module belongs to the application layer. The Rust library deliberately
does not read user configuration or launch REAPER.

Two separate concerns are represented:

* ``reaper.ini`` supplies cache-generation preferences such as
  ``peakcachegenrs``.
* REAPER's ``GetPeakFileNameEx`` API is the canonical path-policy oracle. It
  can be queried live, or its answers can be saved as a portable cache map.

Querying a path does not ask REAPER to decode audio or build a peak file.
libreapeaks can still generate the bytes itself and publish them at the returned
write path.
"""

from __future__ import annotations

from dataclasses import dataclass
import configparser
import json
import locale
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable, Mapping

DEFAULT_PEAK_RATE = 300
MAX_PEAK_RATE = 1_000_000
DEFAULT_QUERY_TIMEOUT = 30.0
CACHE_MAP_VERSION = 2


class ReaperConfigError(ValueError):
    """Invalid REAPER configuration, path query, or cache-map data."""


@dataclass(frozen=True)
class ReaperPeakSettings:
    """Peak-related values loaded from one ``reaper.ini`` file."""

    ini_path: Path
    peak_rate: int = DEFAULT_PEAK_RATE
    generation_mode: int | None = None
    altpeaks_flags: int = 0
    alternate_cache_path: Path | None = None
    alternate_source_paths: tuple[Path, ...] = ()
    raw_alternate_source_path_list: str = ""

    @property
    def has_alternate_cache_path(self) -> bool:
        return self.alternate_cache_path is not None


@dataclass(frozen=True)
class ReaperPeakPaths:
    """Canonical read/write peak paths returned by REAPER."""

    media: Path
    read: Path | None
    write: Path
    source_type: str = ""
    origin: str = "GetPeakFileNameEx"

    def to_json(self) -> dict[str, str]:
        return {
            "media": str(self.media),
            "read": "" if self.read is None else str(self.read),
            "write": str(self.write),
            "source_type": self.source_type,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class EffectivePeakPolicy:
    """Resolved generation preference and its provenance."""

    peak_rate: int
    settings: ReaperPeakSettings | None
    source: str


def _resolved(path: str | Path, *, base: Path | None = None) -> Path:
    value = os.path.expandvars(os.fspath(path)).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    result = Path(value).expanduser()
    if not result.is_absolute() and base is not None:
        result = base / result
    return result.resolve(strict=False)


def _read_ini_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", locale.getpreferredencoding(False), "cp1252"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="surrogateescape")


def _parse_bounded_int(
    section: Mapping[str, str],
    key: str,
    *,
    minimum: int,
    maximum: int,
    default: int | None,
) -> int | None:
    raw = section.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip(), 0)
    except ValueError as exc:
        raise ReaperConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ReaperConfigError(
            f"{key} must be in {minimum}..{maximum}, got {value}"
        )
    return value


def _split_path_list(value: str, *, base: Path) -> tuple[Path, ...]:
    """Parse historically delimiter-varied REAPER path lists.

    The value is retained verbatim as well. Exact applicability decisions are
    delegated to ``GetPeakFileNameEx`` because the path-list matching rules are
    not a stable public file-format contract.
    """

    if not value.strip():
        return ()
    normalized = value.replace("\r", "\n").replace("\0", "\n")
    chunks: list[str] = []
    for line in normalized.split("\n"):
        for pipe_part in line.split("|"):
            chunks.extend(pipe_part.split(";"))
    result: list[Path] = []
    seen: set[str] = set()
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        path = _resolved(chunk, base=base)
        key = os.path.normcase(str(path)) if os.name == "nt" else str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return tuple(result)


def load_reaper_ini(path: str | Path) -> ReaperPeakSettings:
    """Load peak-cache settings from an explicit ``reaper.ini``."""

    ini_path = _resolved(path)
    if not ini_path.is_file():
        raise ReaperConfigError(f"REAPER.ini not found: {ini_path}")
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=False,
        empty_lines_in_values=True,
    )
    parser.optionxform = str.lower
    try:
        parser.read_string(_read_ini_text(ini_path))
    except configparser.Error as exc:
        raise ReaperConfigError(f"cannot parse {ini_path}: {exc}") from exc
    if not parser.has_section("REAPER"):
        raise ReaperConfigError(f"{ini_path} has no [REAPER] section")
    section = {key.lower(): value for key, value in parser.items("REAPER")}

    peak_rate = _parse_bounded_int(
        section,
        "peakcachegenrs",
        minimum=1,
        maximum=MAX_PEAK_RATE,
        default=DEFAULT_PEAK_RATE,
    )
    assert peak_rate is not None
    generation_mode = _parse_bounded_int(
        section,
        "peakcachegenmode",
        minimum=0,
        maximum=0x7FFF_FFFF,
        default=None,
    )
    flags = _parse_bounded_int(
        section,
        "altpeaks",
        minimum=0,
        maximum=0x7FFF_FFFF,
        default=0,
    )
    assert flags is not None

    raw_cache_path = section.get("altpeakspath", "").strip()
    alternate_cache_path = (
        _resolved(raw_cache_path, base=ini_path.parent)
        if raw_cache_path
        else None
    )
    raw_source_paths = section.get("altpeaksopathlist", "")
    source_paths = _split_path_list(raw_source_paths, base=ini_path.parent)

    return ReaperPeakSettings(
        ini_path=ini_path,
        peak_rate=peak_rate,
        generation_mode=generation_mode,
        altpeaks_flags=flags,
        alternate_cache_path=alternate_cache_path,
        alternate_source_paths=source_paths,
        raw_alternate_source_path_list=raw_source_paths,
    )


def _candidate_reaper_ini_paths(
    *,
    executable: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: str | Path | None = None,
) -> list[Path]:
    env = os.environ if environment is None else environment
    platform_name = sys.platform if platform is None else platform
    home_path = Path.home() if home is None else Path(home).expanduser()
    candidates: list[Path] = []

    configured = env.get("REAPER_INI")
    if configured:
        candidates.append(_resolved(configured))

    if executable is not None:
        exe = _resolved(executable)
        candidates.extend(
            [
                exe.parent / "reaper.ini",
                exe.parent / "REAPER" / "reaper.ini",
            ]
        )

    if platform_name.startswith("win"):
        appdata = env.get("APPDATA")
        if appdata:
            candidates.append(_resolved(appdata) / "REAPER" / "reaper.ini")
    elif platform_name == "darwin":
        candidates.append(
            home_path / "Library" / "Application Support" / "REAPER" / "reaper.ini"
        )
    else:
        xdg = env.get("XDG_CONFIG_HOME")
        config_root = _resolved(xdg) if xdg else home_path / ".config"
        candidates.extend(
            [
                config_root / "REAPER" / "reaper.ini",
                config_root / "reaper" / "reaper.ini",
            ]
        )

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.resolve(strict=False)
        key = (
            os.path.normcase(str(candidate))
            if platform_name.startswith("win")
            else str(candidate)
        )
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def discover_reaper_ini(
    *,
    executable: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: str | Path | None = None,
) -> Path | None:
    """Return the first existing platform/portable REAPER configuration."""

    for candidate in _candidate_reaper_ini_paths(
        executable=executable,
        environment=environment,
        platform=platform,
        home=home,
    ):
        if candidate.is_file():
            return candidate
    return None


def resolve_peak_rate(
    *,
    explicit_peak_rate: int | None,
    reaper_ini: str | Path | None = None,
    auto_reaper_ini: bool = False,
    reaper_executable: str | Path | None = None,
) -> EffectivePeakPolicy:
    """Apply ``explicit CLI > REAPER.ini > 300`` precedence."""

    if explicit_peak_rate is not None:
        if not 1 <= explicit_peak_rate <= MAX_PEAK_RATE:
            raise ReaperConfigError(
                f"peak rate must be in 1..{MAX_PEAK_RATE}, got {explicit_peak_rate}"
            )
        settings = load_reaper_ini(reaper_ini) if reaper_ini is not None else None
        return EffectivePeakPolicy(explicit_peak_rate, settings, "explicit")

    ini_path: Path | None
    if reaper_ini is not None:
        ini_path = _resolved(reaper_ini)
    elif auto_reaper_ini:
        ini_path = discover_reaper_ini(executable=reaper_executable)
    else:
        ini_path = None

    if ini_path is not None:
        settings = load_reaper_ini(ini_path)
        return EffectivePeakPolicy(settings.peak_rate, settings, "reaper.ini")
    return EffectivePeakPolicy(DEFAULT_PEAK_RATE, None, "default")


_QUERY_SCRIPT = r'''
local media = os.getenv("LIBREAPEAKS_MEDIA")
local result = os.getenv("LIBREAPEAKS_RESULT")
assert(media and media ~= "", "LIBREAPEAKS_MEDIA missing")
assert(result and result ~= "", "LIBREAPEAKS_RESULT missing")

local function esc(s)
  s = tostring(s or "")
  return s:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r")
end

local function returned_path(a, b)
  if type(b) == "string" and b ~= "" then return b end
  if type(a) == "string" then return a end
  return ""
end

local source = reaper.PCM_Source_CreateFromFile(media)
assert(source, "PCM_Source_CreateFromFile failed")
local source_type = reaper.GetMediaSourceType(source, "") or ""
local r1, r2 = reaper.GetPeakFileNameEx(source, "", false)
local w1, w2 = reaper.GetPeakFileNameEx(source, "", true)
local read_path = returned_path(r1, r2)
local write_path = returned_path(w1, w2)
reaper.PCM_Source_Destroy(source)

local file = assert(io.open(result, "wb"))
file:write('{"media":"' .. esc(media) .. '","read":"' .. esc(read_path)
  .. '","write":"' .. esc(write_path) .. '","source_type":"' .. esc(source_type) .. '"}\n')
file:close()
reaper.Main_OnCommand(40004, 0)
'''


def _resolve_reaper_executable(value: str | Path | None) -> Path:
    if value is not None:
        executable = _resolved(value)
        if not executable.is_file():
            raise ReaperConfigError(f"REAPER executable not found: {executable}")
        return executable
    env_value = os.environ.get("REAPER_EXE")
    if env_value:
        return _resolve_reaper_executable(env_value)
    found = shutil.which("reaper") or shutil.which("reaper.exe")
    if found:
        return _resolved(found)
    raise ReaperConfigError(
        "REAPER executable is required for a live peak-path query "
        "(pass --reaper-executable, set REAPER_EXE, or provide a cache map)"
    )


def query_reaper_peak_paths(
    media: str | Path,
    *,
    reaper_executable: str | Path | None = None,
    reaper_ini: str | Path | None = None,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
    extra_environment: Mapping[str, str] | None = None,
) -> ReaperPeakPaths:
    """Query canonical peak-cache paths without asking REAPER to build peaks."""

    if timeout <= 0:
        raise ReaperConfigError("REAPER path-query timeout must be positive")
    media_path = _resolved(media)
    if not media_path.is_file():
        raise ReaperConfigError(f"media file not found: {media_path}")
    executable = _resolve_reaper_executable(reaper_executable)
    ini_path = _resolved(reaper_ini) if reaper_ini is not None else None
    if ini_path is not None and not ini_path.is_file():
        raise ReaperConfigError(f"REAPER.ini not found: {ini_path}")

    with tempfile.TemporaryDirectory(prefix="libreapeaks-path-query-") as temp:
        root = Path(temp)
        script = root / "query_peak_path.lua"
        result = root / "result.json"
        script.write_text(_QUERY_SCRIPT, encoding="utf-8")

        command = [str(executable), "-newinst"]
        if ini_path is not None:
            command.extend(["-cfgfile", str(ini_path)])
        command.extend(["-new", "-nosplash", str(script)])

        environment = os.environ.copy()
        if extra_environment:
            environment.update({str(k): str(v) for k, v in extra_environment.items()})
        environment.update(
            LIBREAPEAKS_MEDIA=str(media_path),
            LIBREAPEAKS_RESULT=str(result),
        )
        try:
            completed = subprocess.run(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReaperConfigError(
                f"REAPER peak-path query timed out after {timeout:g}s"
            ) from exc
        if completed.returncode != 0 or not result.is_file():
            diagnostic = completed.stdout[-8192:].decode(errors="replace")
            raise ReaperConfigError(
                "REAPER peak-path query failed: "
                f"exit={completed.returncode}, output={diagnostic!r}"
            )
        try:
            payload = json.loads(result.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReaperConfigError(f"invalid REAPER path-query result: {exc}") from exc

    if not isinstance(payload, dict):
        raise ReaperConfigError("REAPER path-query result must be a JSON object")
    raw_write = payload.get("write")
    if not isinstance(raw_write, str) or not raw_write:
        raise ReaperConfigError("REAPER returned an empty peak-cache write path")
    raw_read = payload.get("read", "")
    if not isinstance(raw_read, str):
        raise ReaperConfigError("REAPER returned a non-string peak-cache read path")
    source_type = payload.get("source_type", "")
    if not isinstance(source_type, str):
        raise ReaperConfigError("REAPER returned a non-string media source type")
    return ReaperPeakPaths(
        media=media_path,
        read=_resolved(raw_read) if raw_read else None,
        write=_resolved(raw_write),
        source_type=source_type,
        origin="GetPeakFileNameEx",
    )


def _map_key(path: str | Path) -> str:
    resolved = str(_resolved(path))
    return os.path.normcase(resolved) if os.name == "nt" else resolved


def build_reaper_cache_map(
    media_files: Iterable[str | Path],
    *,
    reaper_executable: str | Path | None = None,
    reaper_ini: str | Path | None = None,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
) -> dict[str, object]:
    """Build a reusable map of canonical REAPER read/write paths."""

    entries: dict[str, dict[str, str]] = {}
    for media in media_files:
        paths = query_reaper_peak_paths(
            media,
            reaper_executable=reaper_executable,
            reaper_ini=reaper_ini,
            timeout=timeout,
        )
        entries[_map_key(paths.media)] = paths.to_json()
    return {
        "version": CACHE_MAP_VERSION,
        "source": "REAPER GetPeakFileNameEx",
        "reaper_ini": "" if reaper_ini is None else str(_resolved(reaper_ini)),
        "entries": entries,
    }


def write_reaper_cache_map(path: str | Path, payload: Mapping[str, object]) -> Path:
    """Atomically publish a UTF-8 JSON cache map."""

    target = _resolved(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=target.name + ".",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_reaper_cache_map(path: str | Path) -> dict[str, ReaperPeakPaths]:
    """Load the current map format and legacy one-record/v1 maps."""

    map_path = _resolved(path)
    try:
        payload = json.loads(map_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ReaperConfigError(f"cannot read cache map {map_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReaperConfigError(f"invalid cache-map JSON {map_path}: {exc}") from exc

    if isinstance(payload, dict) and isinstance(payload.get("media"), str):
        raw_entries: Mapping[str, object] = {payload["media"]: payload}
    elif isinstance(payload, dict):
        if "version" in payload:
            version = payload["version"]
            if type(version) is not int or version not in (1, CACHE_MAP_VERSION):
                raise ReaperConfigError(
                    f"unsupported cache-map version: {version!r}"
                )
        raw = payload.get("entries", payload)
        if not isinstance(raw, dict):
            raise ReaperConfigError("cache-map entries must be a JSON object")
        raw_entries = raw
    else:
        raise ReaperConfigError("cache map must be a JSON object")

    result: dict[str, ReaperPeakPaths] = {}
    for raw_media, raw_entry in raw_entries.items():
        if not isinstance(raw_media, str):
            raise ReaperConfigError("cache-map media keys must be strings")
        media = _resolved(raw_media, base=map_path.parent)
        if isinstance(raw_entry, str):
            read = write = raw_entry
            source_type = ""
            origin = "legacy-cache-map"
        elif isinstance(raw_entry, dict):
            read = raw_entry.get("read", "")
            write = raw_entry.get("write", "") or read
            source_type = raw_entry.get("source_type", raw_entry.get("type", ""))
            origin = raw_entry.get("origin", "cache-map")
            if not all(isinstance(item, str) for item in (read, write, source_type, origin)):
                raise ReaperConfigError(
                    f"cache-map entry for {raw_media!r} contains non-string fields"
                )
        else:
            raise ReaperConfigError(
                f"cache-map entry for {raw_media!r} must be a string or object"
            )
        if not write:
            raise ReaperConfigError(
                f"cache-map entry for {raw_media!r} has no write path"
            )
        paths = ReaperPeakPaths(
            media=media,
            read=_resolved(read, base=map_path.parent) if read else None,
            write=_resolved(write, base=map_path.parent),
            source_type=source_type,
            origin=origin,
        )
        result[_map_key(media)] = paths
    return result


def peak_paths_from_cache_map(media: str | Path, map_path: str | Path) -> ReaperPeakPaths:
    entries = load_reaper_cache_map(map_path)
    key = _map_key(media)
    try:
        return entries[key]
    except KeyError as exc:
        raise ReaperConfigError(f"cache map has no entry for {_resolved(media)}") from exc


def resolve_exact_reaper_peak_paths(
    media: str | Path,
    *,
    cache_map: str | Path | None = None,
    reaper_executable: str | Path | None = None,
    reaper_ini: str | Path | None = None,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
) -> ReaperPeakPaths:
    """Resolve exact paths from a saved map or a live official API query."""

    if cache_map is not None:
        return peak_paths_from_cache_map(media, cache_map)
    return query_reaper_peak_paths(
        media,
        reaper_executable=reaper_executable,
        reaper_ini=reaper_ini,
        timeout=timeout,
    )


def _path_is_within(path: Path, directory: Path) -> bool:
    path_s = os.path.normcase(str(path.resolve(strict=False)))
    directory_s = os.path.normcase(str(directory.resolve(strict=False)))
    try:
        return os.path.commonpath([path_s, directory_s]) == directory_s
    except ValueError:
        return False


def validate_central_peak_paths(
    paths: ReaperPeakPaths,
    settings: ReaperPeakSettings,
) -> ReaperPeakPaths:
    """Fail closed when a central result is outside ``altpeakspath``."""

    directory = settings.alternate_cache_path
    if directory is None:
        raise ReaperConfigError(f"{settings.ini_path} does not define altpeakspath")
    if not _path_is_within(paths.write, directory):
        raise ReaperConfigError(
            "REAPER did not select the configured alternate cache directory: "
            f"write={paths.write}, altpeakspath={directory}"
        )
    if paths.read is not None and not _path_is_within(paths.read, directory):
        raise ReaperConfigError(
            "REAPER returned a non-central read path in strict central mode: "
            f"read={paths.read}, altpeakspath={directory}"
        )
    return paths
