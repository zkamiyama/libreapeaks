#!/usr/bin/env python3
"""Second-level completion proof for the independent-REAPER cache race gate."""
from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = ROOT / "host-results"


def main() -> None:
    path = OUT / "cross-process-race-report.json"
    if not path.is_file():
        print("cross-process completion: missing report", file=sys.stderr)
        raise SystemExit(1)
    report = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []

    def require(ok: bool, message: str) -> None:
        if not ok:
            errors.append(message)

    require(report.get("name") == "cross-process-cache-race", "wrong/missing race case identity")
    require(report.get("passed") is True, "race report did not pass")
    environment = report.get("environment") if isinstance(report.get("environment"), dict) else {}
    expected_sha = os.getenv("GITHUB_SHA")
    require(bool(expected_sha), "GITHUB_SHA is not set")
    if expected_sha:
        require(environment.get("commit") == expected_sha, f"report commit {environment.get('commit')} != workflow SHA {expected_sha}")
    require(bool(environment.get("plugin_sha256")), "normal plugin hash is missing")
    require(bool(environment.get("reaper_sha256")), "REAPER binary hash is missing")
    require("reaper779" in str(environment.get("url", "")).lower(), "race did not use pinned REAPER 7.79 archive")

    tail_sha = report.get("rpkx_tail_sha256")
    require(bool(tail_sha), "original RPKX tail checksum is missing")
    require(report.get("rpkx_tail_bytes", 0) >= 16 * 1024 * 1024, "race did not use the required >=16 MiB RPKX suffix")
    require(report.get("after_race_standard_kind") in ("waveform", "spectrogram"), "post-race standard is not one of the exact native controls")
    require(report.get("after_race_tail_sha256") == tail_sha, "post-race RPKX checksum changed")
    require(report.get("final_tail_sha256") == tail_sha, "cleanup rebuild RPKX checksum changed")
    require(bool(report.get("after_race_standard_sha256")), "post-race standard checksum is missing")
    require(bool(report.get("final_standard_sha256")), "cleanup standard checksum is missing")

    for key in ("wave_runner", "spectrogram_runner", "cleanup_runner"):
        runner = report.get(key) if isinstance(report.get(key), dict) else {}
        result = str(runner.get("result", ""))
        trace = str(runner.get("trace", ""))
        require(runner.get("exit") == 0, f"{key}: REAPER exit was not clean")
        require("finished=true" in result, f"{key}: host script did not finish")
        require("barrier_ready=true" in result and "barrier_go=true" in result, f"{key}: filesystem barrier proof missing")
        require("final_status=2" in result, f"{key}: plugin was not ready at completion")
        require("DIAGNOSTIC_BUILD" not in trace, f"{key}: diagnostic build was used")
        require("ERROR\t" not in trace, f"{key}: production plugin trace contains ERROR")
        require(any(line.startswith("DONE\t") and "reuse=0" in line for line in trace.splitlines()), f"{key}: no real DONE reuse=0 commit")

    output = {
        "passed": not errors,
        "commit": environment.get("commit"),
        "case": report.get("name"),
        "errors": errors,
    }
    (OUT / "cross-process-completion.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print("CROSS_PROCESS_COMPLETION", json.dumps(output), flush=True)
    if errors:
        for error in errors:
            print("cross-process completion:", error, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
