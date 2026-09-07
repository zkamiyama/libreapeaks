#!/usr/bin/env python3
"""Adversarial real-REAPER acceptance for the RPKX reference extension.

These cases deliberately use awkward paths, non-page-aligned payload sizes,
misleading magic bytes inside opaque payloads, and malformed/stale RPKX metadata.
Positive cases must remain byte-identical to same-platform native REAPER 7.79.
Refusal cases must leave the entire pre-existing cache byte-for-byte unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import struct
import sys

import host_acceptance as base
import host_extended as ext

OUT = base.OUT
INFO = base.INFO


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_standard(case_name: str) -> bytes:
    summary = json.loads((OUT / case_name / "summary.json").read_text(encoding="utf-8"))
    data = pathlib.Path(summary["cache_path"]).read_bytes()
    return data[: base.standard_end(data)]


def make_tail(
    standard: bytes,
    payload: bytes,
    *,
    stamp: bytes | None = None,
    entry_length: int | None = None,
) -> bytes:
    """Build the same single-chunk RPKX shape as the host fixture with custom bytes."""
    source_stamp = standard[10:18] if stamp is None else stamp
    if len(source_stamp) != 8:
        raise ValueError("RPKX SourceStamp must be 8 bytes")
    length = len(payload) if entry_length is None else entry_length
    total = 80 + len(payload)
    header = b"RPKX" + struct.pack("<HHIIQ", 1, 32, 0, 1, total) + source_stamp
    entry = b"TEST" + struct.pack("<IIIQQ", 1, 0, 0, 80, length)
    return header + b"\x71" * 16 + entry + payload


def hostile_payload() -> bytes:
    # Deliberately crosses 4 KiB and 1 MiB boundaries and contains convincing
    # standard/RPKX magic sequences at awkward offsets. The payload is opaque;
    # a preserving implementation must never scan/reinterpret these bytes.
    size = 4 * 1024 * 1024 + 4097
    block = bytes(range(256))
    data = bytearray((block * ((size + len(block) - 1) // len(block)))[:size])
    markers = (
        (0, b"RPKX\x00RPKN\x00RPKL"),
        (4094, b"RPKNRPKLRPKX"),
        (1024 * 1024 - 3, b"RPKXTESTRPKN"),
        (2 * 1024 * 1024 + 4093, b"RPKLRPKXRPKN"),
        (size - 19, b"RPKX\xff\x00RPKN\x7fRPKL"),
    )
    for offset, marker in markers:
        data[offset : offset + len(marker)] = marker
    return bytes(data)


def with_base_tail(name: str, standard: bytes, tail: bytes, *, action: str = "manual"):
    original = base.rpkx_tail
    base.rpkx_tail = lambda _std, _mib: tail
    try:
        return base.run_case(name, seed=standard, tail_mib=1, action=action)
    finally:
        base.rpkx_tail = original


def with_ext_tail(name: str, standard: bytes, tail: bytes, **kwargs):
    original = ext.rpkx_tail
    ext.rpkx_tail = lambda _std, _mib: tail
    try:
        return ext.run_ext(name, seed=standard, tail_mib=1, **kwargs)
    finally:
        ext.rpkx_tail = original


def finalize_positive(row: dict, data: bytes | None, expected_standard: bytes, tail: bytes, *, path_probe: str | None = None) -> dict:
    errors = list(row.get("errors", []))
    if data is None:
        errors.append("cache missing after positive adversarial case")
    else:
        try:
            end = base.standard_end(data)
            if data[:end] != expected_standard:
                errors.append("standard bytes differ from same-platform native control")
            if data[end:] != tail:
                errors.append("opaque adversarial RPKX bytes changed")
            row["whole_cache_sha256"] = sha(data)
            row["adversarial_tail_sha256"] = sha(tail)
        except Exception as exc:
            errors.append(f"could not parse resulting standard region: {exc}")
    if path_probe and path_probe not in str(row.get("trace", "")):
        errors.append("wrapped source trace lost the adversarial Unicode/path spelling")
    row["errors"] = errors
    row["passed"] = not errors
    return row


def expected_refusal(name: str, raw_row: dict, after: bytes | None, initial: bytes) -> dict:
    result = str(raw_row.get("result", ""))
    trace = str(raw_row.get("trace", ""))
    errors: list[str] = []

    def require(ok: bool, message: str) -> None:
        if not ok:
            errors.append(message)

    require(raw_row.get("exit") == 0, "REAPER process did not exit cleanly during safe refusal")
    require("finished=true" in result, "host action script did not finish")
    require("plugin=true" in result, "reference extension API was not loaded")
    require("final_status=-2" not in result, "source bypassed the wrapper during refusal test")
    require("final_status=-1" in result or "failure_after_action=true" in result, "unsafe input was not surfaced as a plugin failure")
    require("DIAGNOSTIC_BUILD" not in trace, "safe-refusal test accidentally used diagnostic binary")
    require("ERROR\t" in trace, "safe refusal did not produce a plugin error record")
    require(after == initial, "safe refusal changed at least one byte of the pre-existing cache")
    real_done = base.real_done_fields(trace)
    require(not real_done, "safe refusal unexpectedly reached a successful real DONE commit")

    return {
        "name": name,
        "passed": not errors,
        "errors": errors,
        "expected_refusal": True,
        "before_sha256": sha(initial),
        "after_sha256": sha(after) if after is not None else None,
        "whole_cache_unchanged": after == initial,
        "result": result,
        "trace": trace,
        "underlying_positive_harness_errors": raw_row.get("errors", []),
    }


def main() -> None:
    rows: list[dict] = []
    native = read_standard("native-wave")
    native_spec = read_standard("native-spectrogram")

    # 1. Minimum practical opaque payload + path characters that exercise UTF-8
    # and shell/filesystem boundaries on all three CI operating systems.
    tiny_tail = make_tail(native, b"\xa5")
    unicode_name = "adversarial-unicode-Δ_日本語 space # percent %"
    row, data = with_base_tail(unicode_name, native, tiny_tail, action="manual")
    rows.append(finalize_positive(row, data, native, tiny_tail, path_probe="Δ_日本語"))

    # 2-4. A non-page-aligned 4 MiB+4097 payload with fake RPKN/RPKL/RPKX magic
    # must survive same-size overwrite, growth to spectrogram, and shrink back to
    # waveform with exactly the native standard bytes each time.
    payload = hostile_payload()
    hostile_wave_tail = make_tail(native, payload)
    row, data = with_base_tail("adversarial-hostile-tail-same-size", native, hostile_wave_tail, action="manual")
    rows.append(finalize_positive(row, data, native, hostile_wave_tail))

    row, data = with_base_tail("adversarial-hostile-tail-grow", native, hostile_wave_tail, action="spectrogram")
    rows.append(finalize_positive(row, data, native_spec, hostile_wave_tail))

    hostile_spec_tail = make_tail(native_spec, payload)
    row, data, _ = with_ext_tail(
        "adversarial-hostile-tail-shrink",
        native_spec,
        hostile_spec_tail,
        action="normal",
        show=1345,
        expected_standard=native,
        expect_tail_move="positive",
    )
    rows.append(finalize_positive(row, data, native, hostile_spec_tail))

    # 5. A valid container bound to a different SourceStamp must be rejected by
    # the normal distributable plugin, with no fallback/native rewrite.
    wrong_stamp = bytearray(native[10:18])
    wrong_stamp[0] ^= 0x80
    mismatch_tail = make_tail(native, b"stamp-mismatch-adversarial", stamp=bytes(wrong_stamp))
    mismatch_initial = native + mismatch_tail
    raw, after = with_base_tail("adversarial-source-stamp-mismatch", native, mismatch_tail, action="manual")
    rows.append(expected_refusal("adversarial-source-stamp-mismatch", raw, after, mismatch_initial))

    # 6. A chunk table that claims bytes beyond the RPKX container EOF must be
    # rejected without trying to reinterpret, truncate, relocate, or rewrite it.
    malformed_payload = b"malformed-entry" * 37
    malformed_tail = make_tail(native, malformed_payload, entry_length=len(malformed_payload) + 8192)
    malformed_initial = native + malformed_tail
    raw, after = with_base_tail("adversarial-truncated-rpkx-entry", native, malformed_tail, action="manual")
    rows.append(expected_refusal("adversarial-truncated-rpkx-entry", raw, after, malformed_initial))

    passed = all(row.get("passed") is True for row in rows)
    report = {
        "environment": INFO,
        "passed": passed,
        "cases": rows,
        "hostile_payload_bytes": len(payload),
        "hostile_payload_sha256": sha(payload),
        "scope": (
            "New adversarial real-REAPER 7.79 cases: one-byte RPKX payload, Unicode/space/symbol path, "
            "4MiB+4097 opaque payload with deceptive cache/container magic across page boundaries, exact "
            "same-size/grow/shrink preservation, SourceStamp mismatch safe refusal, and truncated RPKX chunk "
            "safe refusal with whole-cache no-write proof."
        ),
    }
    (OUT / "adversarial-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Adversarial REAPER RPKX host acceptance",
        f"Commit: {INFO['commit']}",
        "",
        "| Case | Result | Error |",
        "|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| " + str(row["name"]) + " | " + ("PASS" if row.get("passed") else "FAIL") + " | " + "; ".join(map(str, row.get("errors", []))) + " |"
        )
    (OUT / "ADVERSARIAL_SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
