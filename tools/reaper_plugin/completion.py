#!/usr/bin/env python3
"""Final per-OS gate for the real-REAPER acceptance artifact.

This intentionally duplicates high-value invariants from the individual suites.
A green completion result proves that all required reports are present, belong
to this exact commit/build, contain the expected cases, and did not become green
merely because a case, assertion, or benchmark group was removed or skipped.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "host-results"

BASE_CASES = {
    "native-wave", "native-stale", "native-float32", "native-spectrogram",
    "plugin-auto", "plugin-float32", "negative-auto", "plugin-manual",
    "plugin-selected", "plugin-project-stale", "plugin-import-stale",
    "plugin-spectrogram", "plugin-reverse", "plugin-online",
    "plugin-genmode-0", "plugin-genmode-1", "plugin-genmode-2",
    "plugin-genmode-3", "negative-manual",
}
EXTENDED_CASES = {
    "spectral-native", "loudness-native", "spectral", "loudness",
    "normal-shrink", "online-regenerate", "extended-negative-reverse",
    "long-native", "long-import", "long-rebuild-rpkx",
    "glue-create", "glue-rpkx-rebuild", "render-create",
    "render-rpkx-rebuild",
}
ENV_KEYS = (
    "commit", "plugin_sha256", "diagnostic_plugin_sha256", "reaper_sha256",
    "archive_sha256",
)


def load(name: str) -> dict[str, Any]:
    path = OUT / name
    if not path.is_file():
        raise ValueError(f"missing required report: {name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{name} is not a JSON object")
    return value


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def index_cases(report: dict[str, Any], expected: set[str], label: str, errors: list[str]) -> dict[str, dict[str, Any]]:
    cases = report.get("cases")
    if not isinstance(cases, list):
        errors.append(f"{label}: cases is missing/not a list")
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("name"), str):
            errors.append(f"{label}: malformed case entry")
            continue
        name = case["name"]
        if name in indexed:
            errors.append(f"{label}: duplicate case {name}")
        indexed[name] = case
    missing = sorted(expected - indexed.keys())
    if missing:
        errors.append(f"{label}: missing required cases: {', '.join(missing)}")
    for name in sorted(expected & indexed.keys()):
        case = indexed[name]
        if case.get("passed") is not True:
            detail = "; ".join(map(str, case.get("errors", [])))
            errors.append(f"{label}: {name} did not pass{': ' + detail if detail else ''}")
    return indexed


def require_same_hash(cases: dict[str, dict[str, Any]], a: str, b: str, errors: list[str]) -> None:
    av = cases.get(a, {}).get("standard_sha256")
    bv = cases.get(b, {}).get("standard_sha256")
    if not av or not bv or av != bv:
        errors.append(f"standard-byte proof mismatch: {a} != {b}")


def real_done(case: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for line in str(case.get("trace", "")).splitlines():
        if not line.startswith("DONE\t"):
            continue
        fields = dict(part.split("=", 1) for part in line.split("\t")[1:] if "=" in part)
        if fields.get("reuse") == "0":
            out.append(fields)
    return out


def require_tail(case: dict[str, Any], name: str, move: str | None, errors: list[str]) -> None:
    if not case.get("tail_sha256"):
        errors.append(f"{name}: RPKX tail checksum proof is missing")
    if move is None:
        return
    values: list[int] = []
    for fields in real_done(case):
        try:
            values.append(int(fields.get("tail_moved", "-1")))
        except ValueError:
            pass
    if not values:
        errors.append(f"{name}: no real DONE record for RPKX relocation proof")
    elif move == "zero" and any(value != 0 for value in values):
        errors.append(f"{name}: same-size rebuild moved RPKX payload")
    elif move == "positive" and not any(value > 0 for value in values):
        errors.append(f"{name}: size-changing rebuild did not relocate RPKX payload")


def require_unwrapped_api(case: dict[str, Any], name: str, errors: list[str]) -> None:
    result = str(case.get("result", ""))
    if "unwrapped_status=-2" not in result:
        errors.append(f"{name}: unwrapped native source status safety proof is missing")
    if "unwrapped_force=0" not in result:
        errors.append(f"{name}: unwrapped native source ForceBuild safety proof is missing")


def require_no_write(case: dict[str, Any], name: str, errors: list[str]) -> None:
    if case.get("failure_no_write") is not True:
        errors.append(f"{name}: injected post-generation failure no-write proof is missing")
        return
    before_present = case.get("before_present") is True
    after_present = case.get("after_present") is True
    before_hash = case.get("failure_before_sha256")
    after_hash = case.get("failure_after_sha256")
    if before_present != after_present:
        errors.append(f"{name}: failure changed cache existence")
    if before_present and (not before_hash or before_hash != after_hash):
        errors.append(f"{name}: failure changed pre-existing cache bytes")
    if not before_present and (before_hash is not None or after_hash is not None):
        errors.append(f"{name}: absent-cache failure unexpectedly produced a cache hash")


def main() -> None:
    errors: list[str] = []
    try:
        base = load("report.json")
        extended = load("extended-report.json")
        benchmark = load("benchmark.json")
    except Exception as exc:
        print(f"completion: {exc}", file=sys.stderr)
        raise SystemExit(1)

    reports = {"base": base, "extended": extended, "benchmark": benchmark}
    for label, report in reports.items():
        if report.get("passed") is not True:
            errors.append(f"{label}: report-level passed flag is not true")

    expected_sha = os.getenv("GITHUB_SHA")
    if not expected_sha:
        errors.append("GITHUB_SHA is not set")

    reference_env = base.get("environment") if isinstance(base.get("environment"), dict) else {}
    for label, report in reports.items():
        env = report.get("environment") if isinstance(report.get("environment"), dict) else {}
        for key in ENV_KEYS:
            if not env.get(key):
                errors.append(f"{label}: environment.{key} is missing")
            elif reference_env.get(key) != env.get(key):
                errors.append(f"{label}: environment.{key} differs from base report")
        if expected_sha and env.get("commit") != expected_sha:
            errors.append(f"{label}: report commit {env.get('commit')} != workflow SHA {expected_sha}")

    for path_key, hash_key in (("plugin", "plugin_sha256"), ("diagnostic_plugin", "diagnostic_plugin_sha256"), ("reaper", "reaper_sha256")):
        raw = reference_env.get(path_key)
        expected = reference_env.get(hash_key)
        if not raw or not expected:
            errors.append(f"base: environment.{path_key}/{hash_key} is missing")
            continue
        path = pathlib.Path(raw)
        if not path.is_file():
            errors.append(f"binary missing at completion time: {path_key}")
        elif sha256(path) != expected:
            errors.append(f"binary hash changed after setup: {path_key}")

    if "reaper779" not in str(reference_env.get("url", "")).lower():
        errors.append("REAPER archive is not the pinned 7.79 package")

    base_cases = index_cases(base, BASE_CASES, "base", errors)
    ext_cases = index_cases(extended, EXTENDED_CASES, "extended", errors)

    record_policy = str(extended.get("record_policy", ""))
    if "no pre-existing RPKX to preserve" not in record_policy or "not a plugin-correctness gate" not in record_policy:
        errors.append("extended: explicit Record preservation-scope policy is missing")

    for name, case in sorted(base_cases.items()):
        if case.get("plugin") is True:
            require_unwrapped_api(case, name, errors)

    for a, b in (
        ("native-wave", "plugin-auto"),
        ("native-float32", "plugin-float32"),
        ("native-spectrogram", "plugin-spectrogram"),
        ("native-stale", "plugin-project-stale"),
        ("native-stale", "plugin-import-stale"),
        ("native-wave", "plugin-manual"),
        ("native-wave", "plugin-selected"),
        ("native-wave", "plugin-reverse"),
        ("native-wave", "plugin-online"),
        ("native-wave", "plugin-genmode-0"),
        ("native-wave", "plugin-genmode-1"),
        ("native-wave", "plugin-genmode-2"),
        ("native-wave", "plugin-genmode-3"),
    ):
        require_same_hash(base_cases, a, b, errors)
    for a, b in (
        ("spectral-native", "spectral"),
        ("loudness-native", "loudness"),
        ("long-native", "long-import"),
        ("long-native", "long-rebuild-rpkx"),
        ("glue-create", "glue-rpkx-rebuild"),
        ("render-create", "render-rpkx-rebuild"),
    ):
        require_same_hash(ext_cases, a, b, errors)
    if base_cases.get("native-wave", {}).get("standard_sha256") != ext_cases.get("normal-shrink", {}).get("standard_sha256"):
        errors.append("normal-shrink does not return exactly to native waveform bytes")
    if base_cases.get("native-wave", {}).get("standard_sha256") != ext_cases.get("online-regenerate", {}).get("standard_sha256"):
        errors.append("online regeneration does not equal native waveform bytes")

    for name in (
        "plugin-manual", "plugin-selected", "plugin-project-stale",
        "plugin-import-stale", "plugin-reverse", "plugin-genmode-0",
        "plugin-genmode-1", "plugin-genmode-2", "plugin-genmode-3",
    ):
        require_tail(base_cases.get(name, {}), name, "zero", errors)
    require_tail(base_cases.get("plugin-online", {}), "plugin-online", None, errors)
    require_tail(base_cases.get("plugin-spectrogram", {}), "plugin-spectrogram", "positive", errors)
    for name in ("spectral", "loudness", "normal-shrink"):
        require_tail(ext_cases.get(name, {}), name, "positive", errors)
    for name in (
        "online-regenerate", "long-rebuild-rpkx", "glue-rpkx-rebuild",
        "render-rpkx-rebuild",
    ):
        require_tail(ext_cases.get(name, {}), name, "zero", errors)

    require_no_write(base_cases.get("negative-auto", {}), "negative-auto", errors)
    require_no_write(base_cases.get("negative-manual", {}), "negative-manual", errors)
    require_no_write(ext_cases.get("extended-negative-reverse", {}), "extended-negative-reverse", errors)

    for name in ("long-import", "long-rebuild-rpkx"):
        case = ext_cases.get(name, {})
        if case.get("real_generation_count", 0) < 1 or "\tstream=1" not in str(case.get("trace", "")):
            errors.append(f"{name}: no proven real streaming generation")

    if benchmark.get("correctness_passed") is not True:
        errors.append("benchmark correctness gate did not pass")
    if benchmark.get("performance_errors") not in ([], None):
        errors.append("benchmark performance gate reported failures")
    summaries = benchmark.get("summaries")
    summary_index: dict[tuple[str, bool, Any], dict[str, Any]] = {}
    if not isinstance(summaries, list):
        errors.append("benchmark summaries are missing")
    else:
        for summary in summaries:
            if isinstance(summary, dict):
                summary_index[(str(summary.get("profile")), bool(summary.get("plugin")), summary.get("rpkx_mib"))] = summary
    for profile in ("waveform", "spectrogram"):
        for plugin, mib in ((False, None), (True, 0), (True, 16), (True, 64)):
            summary = summary_index.get((profile, plugin, mib))
            if not summary or summary.get("valid") is not True or summary.get("n") != 3:
                errors.append(f"benchmark: missing/invalid 3-run median group {profile} plugin={plugin} rpkx_mib={mib}")
            elif plugin and summary.get("within_native_budget") is not True:
                errors.append(f"benchmark: native regression budget not proven for {profile} {mib}MiB")
        large = summary_index.get((profile, True, 64), {})
        if large.get("within_rpkx_size_budget") is not True:
            errors.append(f"benchmark: 64MiB size-regression budget not proven for {profile}")

    manifest = {
        "passed": not errors,
        "commit": expected_sha,
        "platform": reference_env.get("platform"),
        "machine": reference_env.get("machine"),
        "plugin_sha256": reference_env.get("plugin_sha256"),
        "diagnostic_plugin_sha256": reference_env.get("diagnostic_plugin_sha256"),
        "reaper_sha256": reference_env.get("reaper_sha256"),
        "required_base_cases": sorted(BASE_CASES),
        "required_extended_cases": sorted(EXTENDED_CASES),
        "record_policy": record_policy,
        "benchmark_groups": [
            {"profile": profile, "plugin": plugin, "rpkx_mib": mib, "repeats": 3}
            for profile in ("waveform", "spectrogram")
            for plugin, mib in ((False, None), (True, 0), (True, 16), (True, 64))
        ],
        "errors": errors,
    }
    (OUT / "completion.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# REAPER host completion gate",
        "",
        f"Commit: `{expected_sha}`",
        f"Platform: `{reference_env.get('platform')}` / `{reference_env.get('machine')}`",
        f"Plugin SHA-256: `{reference_env.get('plugin_sha256')}`",
        "",
        "Result: **PASS**" if not errors else "Result: **FAIL**",
    ]
    if errors:
        lines += ["", "Failures:"] + [f"- {error}" for error in errors]
    else:
        lines += [
            "",
            "Verified in this artifact: required case inventory, same-commit/build hashes, exact native standard bytes,",
            "RPKX preservation/relocation, unwrapped-source public API safety, post-generation failure atomicity,",
            "25-minute streaming, Glue/Render created-media rebuilds, and median performance budgets.",
            "Record creation is intentionally not a preservation gate because a newly recorded cache has no pre-existing RPKX;",
            "subsequent PCM cache regeneration is covered by the same ordinary PCM16/float32 rebuild-preservation path.",
        ]
    (OUT / "COMPLETION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    if errors:
        for error in errors:
            print("COMPLETION_ERROR", error, file=sys.stderr)
        raise SystemExit(1)
    print("COMPLETION PASS", json.dumps({k: manifest[k] for k in ("commit", "platform", "machine", "plugin_sha256")}))


if __name__ == "__main__":
    main()
