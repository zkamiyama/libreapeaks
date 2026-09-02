#!/usr/bin/env python3
"""Fresh-process REAPER oracle helper.

Compatibility goldens must never batch multiple media sources through one REAPER
process.  Early reverse-engineering runs observed spectral output that depended
on the preceding source.  This module makes the safe policy structural:

    one run_one_source() call = one source = one new REAPER process

An Xvfb server may be shared across calls; REAPER and its configuration are not.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import tempfile


@dataclass(frozen=True)
class FreshPeakResult:
    source: Path
    peak_path: Path
    copied_peak: Path
    sha256: str
    source_type: str
    status: str
    log_path: Path


def _status_value(status: str, prefix: str) -> str:
    return next((line[len(prefix):] for line in status.splitlines() if line.startswith(prefix)), "")


def _remove_stale_sidecars(source: Path) -> None:
    peak_name = source.name + ".reapeaks"
    for old in source.parent.rglob(peak_name):
        old.unlink(missing_ok=True)


def run_one_source(
    *,
    reaper: Path,
    source: Path,
    probe: Path,
    results_dir: Path,
    label: str,
    display: str,
    peakcachegenrs: int,
    showpeaks: int = 1345,
    peakcachegenmode: int = 3,
    expected_source_type: str | None = "WAVE",
    timeout: int = 120,
) -> FreshPeakResult:
    """Build exactly one peak file in exactly one fresh REAPER process."""

    source = source.resolve()
    probe = probe.resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    _remove_stale_sidecars(source)

    cfg_dir = Path(tempfile.mkdtemp(prefix=f"reapeaks-fresh-{label}-"))
    config = cfg_dir / "reaper.ini"
    config.write_text(
        "[REAPER]\n"
        f"peakcachegenmode={int(peakcachegenmode)}\n"
        f"peakcachegenrs={int(peakcachegenrs)}\n"
        f"showpeaks={int(showpeaks)}\n",
        encoding="utf-8",
    )
    status_path = cfg_dir / "result.txt"
    log_path = results_dir / f"{label}.reaper.log"

    env = os.environ.copy()
    env.update(
        DISPLAY=display,
        REAPEAKS_MEDIA=str(source),
        REAPEAKS_RESULT=str(status_path),
    )

    with log_path.open("wb") as log:
        completed = subprocess.run(
            [
                str(reaper),
                "-newinst",
                "-cfgfile",
                str(config),
                "-new",
                "-nosplash",
                str(probe),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )

    status = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
    if completed.returncode != 0 or "OK loops=" not in status:
        raise RuntimeError(
            f"{label}: REAPER failed rc={completed.returncode}: {status!r}; log={log_path}"
        )

    source_type = _status_value(status, "TYPE=")
    if expected_source_type is not None and source_type != expected_source_type:
        raise RuntimeError(
            f"{label}: expected source type {expected_source_type!r}, got {source_type!r}"
        )

    reported_write = _status_value(status, "PEAK_WRITE=")
    candidates: list[Path] = []
    if reported_write:
        candidates.append(Path(reported_write))
    candidates.extend(source.parent.rglob(source.name + ".reapeaks"))
    peak_path = next((p for p in candidates if p.exists()), None)
    if peak_path is None:
        raise RuntimeError(f"{label}: REAPER reported success but no peak file was found")

    copied_peak = results_dir / f"{label}.reapeaks"
    shutil.copy2(peak_path, copied_peak)
    digest = hashlib.sha256(copied_peak.read_bytes()).hexdigest()
    return FreshPeakResult(
        source=source,
        peak_path=peak_path,
        copied_peak=copied_peak,
        sha256=digest,
        source_type=source_type,
        status=status,
        log_path=log_path,
    )
