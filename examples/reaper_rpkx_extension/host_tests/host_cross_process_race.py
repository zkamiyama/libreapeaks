#!/usr/bin/env python3
"""Strict real-REAPER cross-process race gate for the RPKX reference extension.

Two independent REAPER 7.79 processes are released from a filesystem barrier and
then rebuild the same media/cache concurrently with different requested peak
profiles. The persistent .rpkx.lock must serialize mutations across processes so
that every observed cache state is a complete native-compatible standard image
followed by the exact original RPKX bytes. A third clean rebuild then proves no
redo/WAL residue was left behind by the race.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import pathlib
import shutil
import struct
import time

import host_acceptance as base
import host_extended as ext
from host_process import launch

OUT = base.OUT
INFO = base.INFO
SCRIPT = pathlib.Path(__file__).with_name("host_cross_process.lua")
NATIVE_SCRIPT = pathlib.Path(__file__).with_name("host_actions.lua")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def same_source_native_standard(case_name: str, source_media: pathlib.Path, operation: str) -> bytes:
    """Generate an exact native oracle from the same five-minute source bytes.

    The ordinary host acceptance native controls use a ten-second fixture.  This
    race deliberately uses a five-minute fixture, so reusing those controls would
    compare different cache lengths and make every real long-source rebuild look
    corrupt.  Build fresh plugin-free controls here instead.
    """
    root = OUT / case_name
    root.mkdir(parents=True, exist_ok=False)
    media = root / "audio.wav"
    shutil.copy2(source_media, media)
    cfg = root / "reaper.ini"
    cfg.write_text(
        "[REAPER]\npeakcachegenmode=3\npeakcachegenrs=300\nshowpeaks=1\n"
        "[audioconfig]\nmode=5\ndummy_srate=48000\ndummy_blocksize=512\n",
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        LRPK_CASE=str(root),
        LRPK_MEDIA=str(media),
        LRPK_ACTION=operation,
        LRPK_EXPECT_PLUGIN="0",
        LIBREAPEAKS_PLUGIN_LOG=str(root / "plugin.tsv"),
    )
    env.pop("LIBREAPEAKS_TEST_FAIL_AFTER_GENERATE", None)
    rc = launch(
        [INFO["reaper"], "-newinst", "-cfgfile", str(cfg), "-new", "-nosplash", str(NATIVE_SCRIPT)],
        env,
        root,
        timeout=150.0,
    )
    result = (root / "result.txt").read_text(encoding="utf-8", errors="replace") if (root / "result.txt").exists() else ""
    kv = dict(line.split("=", 1) for line in result.splitlines() if "=" in line)
    paths = [pathlib.Path(kv[k]) for k in ("peak_write", "peak_read") if kv.get(k)]
    paths.append(pathlib.Path(str(media) + ".reapeaks"))
    cache = next((path for path in paths if path.is_file()), paths[-1])
    errors: list[str] = []
    if rc != 0:
        errors.append("native oracle REAPER did not exit cleanly")
    if "finished=true" not in result:
        errors.append("native oracle host script did not finish")
    if "error=" in result:
        errors.append("native oracle host script reported an error")
    if "plugin=false" not in result:
        errors.append("native oracle unexpectedly loaded the reference plugin")
    if not cache.is_file():
        errors.append("native oracle cache was not produced")
    if errors:
        raise RuntimeError(f"{case_name}: " + "; ".join(errors))
    data = cache.read_bytes()
    standard = data[: base.standard_end(data)]
    if operation == "spectrogram":
        layers = [struct.unpack_from("<i", standard, 18 + i * 8)[0] for i in range(standard[5])]
        if -103 not in layers:
            raise RuntimeError(f"{case_name}: native spectrogram oracle has no spectrogram layer")
    return standard


def prepare_runner(root: pathlib.Path, media: pathlib.Path, operation: str, ready: pathlib.Path, go: pathlib.Path) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=False)
    cfg = root / "reaper.ini"
    cfg.write_text(
        "[REAPER]\npeakcachegenmode=3\npeakcachegenrs=300\nshowpeaks=1\n"
        "[audioconfig]\nmode=5\ndummy_srate=48000\ndummy_blocksize=512\n",
        encoding="utf-8",
    )
    (root / "UserPlugins").mkdir()
    plugin = pathlib.Path(INFO["plugin"])
    shutil.copy2(plugin, root / "UserPlugins" / plugin.name)
    env = dict(
        os.environ,
        LRPK_CASE=str(root),
        LRPK_MEDIA=str(media),
        LRPK_ACTION=operation,
        LRPK_EXPECT_PLUGIN="1",
        LRPK_BARRIER_READY=str(ready),
        LRPK_BARRIER_GO=str(go),
        LIBREAPEAKS_PLUGIN_LOG=str(root / "plugin.tsv"),
    )
    env.pop("LIBREAPEAKS_TEST_FAIL_AFTER_GENERATE", None)
    return env


def run_runner(root: pathlib.Path, env: dict[str, str], *, timeout: float = 150.0) -> dict[str, object]:
    rc = launch(
        [INFO["reaper"], "-newinst", "-cfgfile", str(root / "reaper.ini"), "-new", "-nosplash", str(SCRIPT)],
        env,
        root,
        timeout=timeout,
    )
    result = (root / "result.txt").read_text(encoding="utf-8", errors="replace") if (root / "result.txt").exists() else ""
    trace = (root / "plugin.tsv").read_text(encoding="utf-8", errors="replace") if (root / "plugin.tsv").exists() else ""
    return {"exit": rc, "result": result, "trace": trace}


def wait_ready(paths: list[pathlib.Path], timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(path.is_file() for path in paths):
            return
        time.sleep(0.01)
    missing = [str(path) for path in paths if not path.is_file()]
    raise RuntimeError(f"cross-process barrier was not reached: {missing}")


def real_done(trace: str) -> list[dict[str, str]]:
    return base.real_done_fields(trace)


def validate_runner(name: str, row: dict[str, object], errors: list[str]) -> None:
    result = str(row.get("result", ""))
    trace = str(row.get("trace", ""))
    if row.get("exit") != 0:
        errors.append(f"{name}: REAPER did not exit cleanly")
    if "finished=true" not in result:
        errors.append(f"{name}: host script did not finish")
    if "error=" in result:
        errors.append(f"{name}: host script reported an error")
    if "plugin=true" not in result:
        errors.append(f"{name}: reference extension API was not loaded")
    if "barrier_ready=true" not in result or "barrier_go=true" not in result:
        errors.append(f"{name}: deterministic start barrier was not proven")
    if "final_status=2" not in result:
        errors.append(f"{name}: plugin did not finish ready")
    if "DIAGNOSTIC_BUILD" in trace:
        errors.append(f"{name}: diagnostic binary was used")
    if "ERROR\t" in trace:
        errors.append(f"{name}: production trace contains an error")
    if not real_done(trace):
        errors.append(f"{name}: no real DONE reuse=0 commit was observed")


def split_cache(data: bytes) -> tuple[bytes, bytes]:
    end = base.standard_end(data)
    return data[:end], data[end:]


def main() -> None:
    case = OUT / "cross-process-race"
    case.mkdir(parents=True, exist_ok=False)
    shared = case / "shared"
    shared.mkdir()
    media = shared / "audio.wav"
    # Five minutes is long enough to make both independent jobs overlap in the
    # decode/generate window while keeping the release gate practical on macOS.
    ext.repeated_pcm16(media, 300)

    # The strict oracle must describe this exact five-minute fixture, not the
    # ten-second native controls produced by host_acceptance.py.
    native_wave = same_source_native_standard("cross-native-wave", media, "manual")
    native_spec = same_source_native_standard("cross-native-spectrogram", media, "spectrogram")
    tail = base.rpkx_tail(native_wave, 16)
    cache = pathlib.Path(str(media) + ".reapeaks")
    initial = native_wave + tail
    cache.write_bytes(initial)

    go = case / "GO"
    ready_a = case / "ready-a"
    ready_b = case / "ready-b"
    runner_a = case / "runner-wave"
    runner_b = case / "runner-spectrogram"
    env_a = prepare_runner(runner_a, media, "manual", ready_a, go)
    env_b = prepare_runner(runner_b, media, "spectrogram", ready_b, go)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        # Process startup is deliberately serialized only until each independent
        # REAPER reaches the filesystem barrier.  This avoids two macOS startup
        # UI handlers fighting over the frontmost application while preserving
        # the property under test: both live processes begin the shared-cache
        # rebuild only after the same GO file is published.
        future_a = pool.submit(run_runner, runner_a, env_a)
        wait_ready([ready_a])
        future_b = pool.submit(run_runner, runner_b, env_b)
        wait_ready([ready_b])
        go.write_text("go\n", encoding="ascii")
        result_a = future_a.result()
        result_b = future_b.result()

    after_race = cache.read_bytes() if cache.exists() else b""
    errors: list[str] = []
    validate_runner("wave-runner", result_a, errors)
    validate_runner("spectrogram-runner", result_b, errors)

    if not after_race:
        errors.append("shared cache disappeared after concurrent rebuilds")
        race_standard = b""
        race_tail = b""
    else:
        try:
            race_standard, race_tail = split_cache(after_race)
            if race_standard not in (native_wave, native_spec):
                errors.append("post-race standard region is neither exact native waveform nor exact native spectrogram bytes")
            if race_tail != tail:
                errors.append("post-race RPKX suffix changed during cross-process mutation")
        except Exception as exc:
            race_standard, race_tail = b"", b""
            errors.append(f"post-race cache is not a complete parseable standard+RPKX image: {exc}")

    # A clean third process must be able to rebuild the raced cache immediately.
    # This catches a transaction that looked complete but left replay metadata or
    # an inconsistent persistent lock/WAL state behind.
    runner_c = case / "runner-cleanup"
    ready_c = case / "ready-c"
    go_c = case / "GO-cleanup"
    env_c = prepare_runner(runner_c, media, "manual", ready_c, go_c)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future_c = pool.submit(run_runner, runner_c, env_c)
        wait_ready([ready_c])
        go_c.write_text("go\n", encoding="ascii")
        result_c = future_c.result()
    validate_runner("cleanup-runner", result_c, errors)

    final = cache.read_bytes() if cache.exists() else b""
    if not final:
        errors.append("shared cache disappeared after cleanup rebuild")
        final_standard = b""
        final_tail = b""
    else:
        try:
            final_standard, final_tail = split_cache(final)
            if final_standard != native_wave:
                errors.append("cleanup rebuild did not return to exact native waveform bytes")
            if final_tail != tail:
                errors.append("cleanup rebuild changed the original RPKX suffix")
        except Exception as exc:
            final_standard, final_tail = b"", b""
            errors.append(f"cleanup cache is not a complete parseable standard+RPKX image: {exc}")

    row = {
        "name": "cross-process-cache-race",
        "passed": not errors,
        "errors": errors,
        "environment": INFO,
        "native_wave_standard_sha256": sha(native_wave),
        "native_spectrogram_standard_sha256": sha(native_spec),
        "native_source_seconds": 300,
        "initial_sha256": sha(initial),
        "rpkx_tail_sha256": sha(tail),
        "rpkx_tail_bytes": len(tail),
        "after_race_sha256": sha(after_race) if after_race else None,
        "after_race_standard_sha256": sha(race_standard) if race_standard else None,
        "after_race_tail_sha256": sha(race_tail) if race_tail else None,
        "after_race_standard_kind": "waveform" if race_standard == native_wave else "spectrogram" if race_standard == native_spec else "invalid",
        "final_sha256": sha(final) if final else None,
        "final_standard_sha256": sha(final_standard) if final_standard else None,
        "final_tail_sha256": sha(final_tail) if final_tail else None,
        "wave_runner": result_a,
        "spectrogram_runner": result_b,
        "cleanup_runner": result_c,
        "scope": (
            "Two independent normal REAPER 7.79 processes, deterministic filesystem start barrier, shared five-minute PCM16 source/cache, "
            "same-source five-minute native waveform/spectrogram controls, 16 MiB RPKX suffix, simultaneous waveform same-size and "
            "spectrogram growth rebuilds, exact native-standard-oracle validation immediately after the race, exact suffix preservation, "
            "and a third clean rebuild proving no residual redo/WAL corruption."
        ),
    }
    (OUT / "cross-process-race-report.json").write_text(json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = [
        "# Cross-process REAPER cache race",
        f"Commit: {INFO['commit']}",
        "",
        "| Case | Result | Error |",
        "|---|---|---|",
        "| cross-process-cache-race | " + ("PASS" if row["passed"] else "FAIL") + " | " + "; ".join(errors) + " |",
    ]
    (OUT / "CROSS_PROCESS_RACE_SUMMARY.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("\n".join(summary) + "\n")
    print(json.dumps(row, ensure_ascii=False), flush=True)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
