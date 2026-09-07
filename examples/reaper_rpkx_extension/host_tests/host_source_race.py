#!/usr/bin/env python3
"""Adversarial real-REAPER gate for a source mutation during peak generation.

The normal distributable extension must never commit a standard peak image that
was generated from a source whose file stamp changed after the job began. The
pre-existing cache (including its RPKX suffix) must remain byte-for-byte intact.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys
import threading
import time

import host_acceptance as base
import host_extended as ext
from host_process import launch

OUT = base.OUT
INFO = base.INFO
SCRIPT = pathlib.Path(__file__).with_name("host_actions.lua")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def native_wave_standard() -> bytes:
    summary = json.loads((OUT / "native-wave" / "summary.json").read_text(encoding="utf-8"))
    data = pathlib.Path(summary["cache_path"]).read_bytes()
    return data[: base.standard_end(data)]


def wait_and_mutate(trace_path: pathlib.Path, media: pathlib.Path, seen: dict[str, object]) -> None:
    deadline = time.monotonic() + 30.0
    needle = f"file={media}"
    while time.monotonic() < deadline:
        try:
            trace = trace_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            trace = ""
        if "BEGIN\t" in trace and needle in trace:
            # Keep size/geometry stable but change the source identity while the
            # normal raw PCM16 job is still decoding. A +600 s mtime jump avoids
            # filesystem timestamp-resolution ambiguity on every CI platform.
            st = media.stat()
            target_ns = max(st.st_mtime_ns + 600_000_000_000, (base.FIXED_MTIME + 600) * 1_000_000_000)
            for _ in range(50):
                try:
                    os.utime(media, ns=(target_ns, target_ns))
                    if media.stat().st_mtime_ns == target_ns:
                        seen["mutated"] = True
                        seen["mtime_ns"] = target_ns
                        return
                except OSError as exc:
                    seen["last_error"] = repr(exc)
                time.sleep(0.01)
            return
        time.sleep(0.001)
    seen["timeout"] = True


def main() -> None:
    case = OUT / "source-change-race"
    case.mkdir(parents=True, exist_ok=False)
    media = case / "audio.wav"
    # 25 minutes makes the BEGIN->final-stamp window comfortably observable even
    # on the fastest raw-PCM16 CI host while reusing an already validated long
    # source geometry.
    ext.repeated_pcm16(media, 1500)
    standard = native_wave_standard()
    tail = base.rpkx_tail(standard, 1)
    cache = pathlib.Path(str(media) + ".reapeaks")
    initial = standard + tail
    cache.write_bytes(initial)

    cfg = case / "reaper.ini"
    cfg.write_text(
        "[REAPER]\npeakcachegenmode=3\npeakcachegenrs=300\nshowpeaks=1\n"
        "[audioconfig]\nmode=5\ndummy_srate=48000\ndummy_blocksize=512\n",
        encoding="utf-8",
    )
    (case / "UserPlugins").mkdir()
    plugin = pathlib.Path(INFO["plugin"])
    shutil.copy2(plugin, case / "UserPlugins" / plugin.name)

    trace_path = case / "plugin.tsv"
    env = dict(
        os.environ,
        LRPK_CASE=str(case),
        LRPK_MEDIA=str(media),
        # Import starts the real production peak job but deliberately performs
        # no second explicit REAPER rebuild after the injected source change.
        # The gate therefore observes the atomicity of the raced job itself.
        LRPK_ACTION="import",
        LRPK_EXPECT_PLUGIN="1",
        LIBREAPEAKS_PLUGIN_LOG=str(trace_path),
    )
    env.pop("LIBREAPEAKS_TEST_FAIL_AFTER_GENERATE", None)

    seen: dict[str, object] = {}
    mutator = threading.Thread(target=wait_and_mutate, args=(trace_path, media, seen), daemon=True)
    mutator.start()
    rc = launch(
        [INFO["reaper"], "-newinst", "-cfgfile", str(cfg), "-new", "-nosplash", str(SCRIPT)],
        env,
        case,
        timeout=140,
    )
    mutator.join(timeout=2.0)

    result = (case / "result.txt").read_text(encoding="utf-8", errors="replace") if (case / "result.txt").exists() else ""
    trace = trace_path.read_text(encoding="utf-8", errors="replace") if trace_path.exists() else ""
    after = cache.read_bytes() if cache.exists() else None
    errors: list[str] = []

    def require(ok: bool, message: str) -> None:
        if not ok:
            errors.append(message)

    require(rc == 0, "REAPER process did not exit cleanly")
    require(seen.get("mutated") is True, f"source mutation was not injected after BEGIN: {seen}")
    require("finished=true" in result, "host action script did not finish")
    require("plugin=true" in result, "normal extension API was not loaded")
    require("DIAGNOSTIC_BUILD" not in trace, "race gate accidentally used diagnostic binary")
    require("raw_pcm16=1" in trace, "race gate did not exercise the raw PCM16 production path")
    require("source changed during decode" in trace, "source-stamp race was not detected before commit")
    require("final_status=-1" in result or "failure_after_action=true" in result, "source race was not surfaced as a plugin failure")
    require(after == initial, "source race changed the pre-existing cache/RPKX despite refusal")
    require(not base.real_done_fields(trace), "source race unexpectedly reached a successful real DONE commit")

    row = {
        "name": "source-change-race",
        "passed": not errors,
        "errors": errors,
        "exit": rc,
        "mutation": seen,
        "before_sha256": sha(initial),
        "after_sha256": sha(after) if after is not None else None,
        "whole_cache_unchanged": after == initial,
        "result": result,
        "trace": trace,
        "scope": "Normal distributable extension, initial/import-triggered 25-minute PCM16 WAVE generation, mtime mutation after real BEGIN and before commit; requires explicit source-change refusal and whole-cache/RPKX no-write proof, with no follow-up rebuild allowed to mask the raced job.",
    }
    (OUT / "source-race-report.json").write_text(json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = [
        "# Source-change race acceptance",
        f"Commit: {INFO['commit']}",
        "",
        "| Case | Result | Error |",
        "|---|---|---|",
        "| source-change-race | " + ("PASS" if row["passed"] else "FAIL") + " | " + "; ".join(errors) + " |",
    ]
    (OUT / "SOURCE_RACE_SUMMARY.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("\n".join(summary) + "\n")
    print(json.dumps(row, ensure_ascii=False), flush=True)
    if errors:
        for filename in ("console.txt", "actions.txt", "startup-windows.json", "startup-macos.txt", "host-process.json"):
            path = case / filename
            if path.exists():
                print("SOURCE_RACE_DIAGNOSTIC", filename, path.read_text(errors="replace")[-16000:], flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
