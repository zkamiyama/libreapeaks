#!/usr/bin/env python3
"""Package one already-verified normal REAPER reference extension.

The release workflow runs the real-host hard gates before invoking this helper.
Diagnostic/fault-hook binaries are deliberately excluded.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[3]


def main() -> None:
    suffix = os.environ["EXTENSION_SUFFIX"]
    asset = ROOT / os.environ["RELEASE_ASSET"]
    candidates = sorted((ROOT / "host-build").rglob(f"reaper_rpkx.{suffix}"))
    if len(candidates) != 1:
        raise SystemExit(f"expected exactly one normal extension, found: {candidates}")

    stage = ROOT / "release-package"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    shutil.copy2(candidates[0], stage / f"reaper_rpkx.{suffix}")
    shutil.copy2(ROOT / "examples/reaper_rpkx_extension/USER_GUIDE.md", stage / "USER_GUIDE.md")
    shutil.copy2(ROOT / "LICENSE", stage / "LICENSE")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", stage / "THIRD_PARTY_NOTICES.md")

    verification = stage / "verification"
    verification.mkdir()
    for name in (
        "completion.json",
        "benchmark.json",
        "adversarial-report.json",
        "cross-process-race-report.json",
        "cross-process-completion.json",
    ):
        shutil.copy2(ROOT / "host-results" / name, verification / name)

    (stage / "RELEASE_INFO.txt").write_text(
        "libreapeaks REAPER RPKX reference extension\n"
        "Experimental example; not part of the libreapeaks public API.\n"
        "Validated host: REAPER 7.79\n"
        "Release hard gates exclude the timing-sensitive source-change race;\n"
        "source-stamp validation remains implemented and covered by deterministic tests.\n"
        f"Target: {os.environ['TARGET_SLUG']}\n"
        f"Tag: {os.environ.get('RELEASE_TAG', 'v0.1.0')}\n"
        f"Commit: {os.environ.get('SOURCE_SHA', '')}\n",
        encoding="utf-8",
    )

    with zipfile.ZipFile(asset, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(stage))
    print(asset)


if __name__ == "__main__":
    main()
