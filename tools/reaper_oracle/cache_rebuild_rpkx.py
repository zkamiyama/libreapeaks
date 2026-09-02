#!/usr/bin/env python3
"""Force REAPER to rebuild plain and RPKX-appended caches and compare outputs.

The input source is the deterministic WAV created by cache_extension_oracle.
Both cache inputs carry the original source stamp. We move the source mtime by
120 seconds, which is outside the small tolerance observed/documented for the
normal freshness check, then let a fresh REAPER process run the ordinary
PCM_Source_BuildPeaks Begin/Run/Finish lifecycle.

If REAPER rewrites the cache from scratch, the output produced from the RPKX
input should become byte-identical to the output produced from the plain input
and the appended extension should disappear.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
PEAK_RATE = 300
SHOWPEAKS = 64
PEAKCACHEGENMODE = 3
MTIME_DELTA_SECONDS = 120


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def status_value(status: str, prefix: str) -> str:
    return next(
        (line[len(prefix) :] for line in status.splitlines() if line.startswith(prefix)),
        "",
    )


def write_config(path: Path) -> None:
    path.write_text(
        "[REAPER]\n"
        f"peakcachegenmode={PEAKCACHEGENMODE}\n"
        f"peakcachegenrs={PEAK_RATE}\n"
        f"showpeaks={SHOWPEAKS}\n",
        encoding="utf-8",
    )


def run_rebuild(
    *,
    reaper: Path,
    source: Path,
    peak_path: Path,
    cache_input: bytes,
    name: str,
    display: str,
    results: Path,
    timeout: int,
) -> dict[str, object]:
    case_dir = results / "rebuild-cases" / name
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "input.reapeaks").write_bytes(cache_input)
    peak_path.parent.mkdir(parents=True, exist_ok=True)
    peak_path.write_bytes(cache_input)

    cfg_dir = Path(tempfile.mkdtemp(prefix=f"reapeaks-rebuild-{name}-"))
    config = cfg_dir / "reaper.ini"
    write_config(config)
    status_path = cfg_dir / "result.txt"
    env = os.environ.copy()
    env.update(
        DISPLAY=display,
        REAPEAKS_MEDIA=str(source.resolve()),
        REAPEAKS_RESULT=str(status_path),
    )

    timed_out = False
    returncode: int | None = None
    try:
        with (case_dir / "reaper.log").open("wb") as log:
            completed = subprocess.run(
                [
                    str(reaper),
                    "-newinst",
                    "-cfgfile",
                    str(config),
                    "-new",
                    "-nosplash",
                    str(HERE / "build_one.lua"),
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout,
            )
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True

    status = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
    (case_dir / "status.txt").write_text(status, encoding="utf-8")
    output = peak_path.read_bytes() if peak_path.exists() else b""
    if output:
        (case_dir / "output.reapeaks").write_bytes(output)
    begin_text = status_value(status, "BEGIN=")
    begin = int(begin_text) if begin_text else None

    return {
        "name": name,
        "begin": begin,
        "reuse": begin == 0 if begin is not None else None,
        "returncode": returncode,
        "timed_out": timed_out,
        "status": status.strip(),
        "input_size": len(cache_input),
        "output_size": len(output),
        "input_sha256": sha256(cache_input),
        "output_sha256": sha256(output) if output else None,
        "output": output,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("reaper", type=Path)
    parser.add_argument("--display", default=":96")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    root = args.root.resolve()
    results = root / "results"
    report = json.loads((results / "cache-extension-oracle.json").read_text(encoding="utf-8"))
    source = Path(report["source"])
    peak_path = Path(report["peak_path"])
    plain_input = (results / "cases" / "control" / "input.reapeaks").read_bytes()
    rpkx_input = (results / "cases" / "trailing-rpkx-timeline" / "input.reapeaks").read_bytes()
    if not rpkx_input.startswith(plain_input):
        raise RuntimeError("RPKX fixture is not a pure EOF append of the control cache")
    extension = rpkx_input[len(plain_input) :]
    if not extension.startswith(b"RPKX"):
        raise RuntimeError("RPKX fixture has no extension magic at the standard EOF")

    original_stat = source.stat()
    changed_ns = original_stat.st_mtime_ns + MTIME_DELTA_SECONDS * 1_000_000_000
    os.utime(source, ns=(changed_ns, changed_ns))

    try:
        plain = run_rebuild(
            reaper=args.reaper.resolve(),
            source=source,
            peak_path=peak_path,
            cache_input=plain_input,
            name="plain-mtime-mismatch",
            display=args.display,
            results=results,
            timeout=args.timeout,
        )
        rpkx = run_rebuild(
            reaper=args.reaper.resolve(),
            source=source,
            peak_path=peak_path,
            cache_input=rpkx_input,
            name="rpkx-mtime-mismatch",
            display=args.display,
            results=results,
            timeout=args.timeout,
        )
    finally:
        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    plain_output = plain.pop("output")
    rpkx_output = rpkx.pop("output")
    outputs_equal = plain_output == rpkx_output
    extension_preserved = extension in rpkx_output
    suffix_preserved = rpkx_output.endswith(extension)

    summary = {
        "source_mtime_delta_seconds": MTIME_DELTA_SECONDS,
        "standard_input_size": len(plain_input),
        "rpkx_input_size": len(rpkx_input),
        "extension_size": len(extension),
        "extension_sha256": sha256(extension),
        "plain": plain,
        "rpkx": rpkx,
        "rebuilt_outputs_byte_identical": outputs_equal,
        "rpkx_extension_present_after_rebuild": extension_preserved,
        "rpkx_suffix_preserved_after_rebuild": suffix_preserved,
    }
    (results / "cache-rebuild-rpkx.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    print("# RPKX forced rebuild")
    print(f"plain BEGIN={plain['begin']} size={plain['input_size']}->{plain['output_size']}")
    print(f"rpkx  BEGIN={rpkx['begin']} size={rpkx['input_size']}->{rpkx['output_size']}")
    print(f"rebuilt outputs byte-identical: {outputs_equal}")
    print(f"RPKX extension present after rebuild: {extension_preserved}")
    print(f"RPKX suffix preserved after rebuild: {suffix_preserved}")

    if plain["begin"] in (None, 0) or rpkx["begin"] in (None, 0):
        raise RuntimeError("mtime mismatch did not force both caches to rebuild")
    if plain["timed_out"] or rpkx["timed_out"]:
        raise RuntimeError("REAPER rebuild probe timed out")
    if plain["returncode"] != 0 or rpkx["returncode"] != 0:
        raise RuntimeError("REAPER rebuild probe failed")
    if not outputs_equal:
        raise RuntimeError("plain and RPKX inputs produced different rebuilt caches")
    if extension_preserved or suffix_preserved:
        raise RuntimeError("REAPER unexpectedly preserved the appended RPKX extension")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
