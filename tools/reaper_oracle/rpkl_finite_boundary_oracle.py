#!/usr/bin/env python3
from pathlib import Path
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time

repo = Path.cwd()
root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/rpkl-finite-boundaries")
reaper = Path(sys.argv[2] if len(sys.argv) > 2 else os.environ["REAPER_BIN"])
media = root / "media"
results = root / "results"
media.mkdir(parents=True, exist_ok=True)
results.mkdir(parents=True, exist_ok=True)
probe = (repo / "tools/reaper_oracle/build_probe.lua").resolve()

SAMPLE_RATE = 48_000
PPS = 48_000
POS_TRANSITIONS = 32_767  # 0->1 through 32766->32767
NEG_TRANSITIONS = 32_768  # 0->1 through 32767->32768
FRAMES = max(POS_TRANSITIONS, NEG_TRANSITIONS)
MAX_FINITE_MAG_BITS = 0x7F7FFFFF
DISPLAY = ":97"


def f32_from_bits(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def write_float_wave(path: Path, positive_bits, negative_bits) -> None:
    payload = bytearray()
    for index in range(FRAMES):
        pos_bits = positive_bits[index] if index < len(positive_bits) else 0
        neg_bits = negative_bits[index] if index < len(negative_bits) else 0
        payload += struct.pack("<I", pos_bits)
        payload += struct.pack("<I", neg_bits | 0x80000000)
    channels = 2
    block_align = channels * 4
    byte_rate = SAMPLE_RATE * block_align
    fmt = struct.pack("<HHIIHH", 3, channels, SAMPLE_RATE, byte_rate, block_align, 32)
    riff_size = 4 + (8 + len(fmt)) + (8 + len(payload))
    wave = bytearray()
    wave += b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
    wave += b"fmt " + struct.pack("<I", len(fmt)) + fmt
    wave += b"data" + struct.pack("<I", len(payload)) + payload
    path.write_bytes(wave)


def parse_fine_codes(path: Path):
    data = path.read_bytes()
    if data[:4] != b"RPKL":
        raise RuntimeError(f"expected RPKL, got {data[:4]!r}")
    channels = data[4]
    layer_count = data[5]
    if channels != 2:
        raise RuntimeError(f"expected 2 channels, got {channels}")
    headers = [struct.unpack_from("<iI", data, 18 + 8 * i) for i in range(layer_count)]
    wave_headers = [(index, division, count) for index, (division, count) in enumerate(headers) if division > 0]
    if not wave_headers:
        raise RuntimeError("no positive waveform layers")
    first_index, first_division, first_count = wave_headers[0]
    if first_index != 0 or first_division != 1:
        raise RuntimeError(f"expected first waveform division=1, got index={first_index} division={first_division}")
    if first_count != FRAMES:
        raise RuntimeError(f"expected {FRAMES} fine peaks, got {first_count}")
    offset = 18 + 8 * layer_count
    fine_bytes = first_count * channels * 4
    payload = data[offset : offset + fine_bytes]
    if len(payload) != fine_bytes:
        raise RuntimeError("truncated fine waveform payload")
    positive = []
    negative_mag = []
    for frame in range(FRAMES):
        base = frame * channels * 4
        pos_max, _pos_min = struct.unpack_from("<hh", payload, base)
        _neg_max, neg_min = struct.unpack_from("<hh", payload, base + 4)
        positive.append(int(pos_max))
        negative_mag.append(32768 if neg_min == -32768 else -int(neg_min))
    return positive, negative_mag, headers


def run_reaper(positive_bits, negative_bits, label: str):
    source = media / "probe.wav"
    write_float_wave(source, positive_bits, negative_bits)
    peak = media / "probe.wav.reapeaks"
    if peak.exists():
        peak.unlink()
    case_dir = Path(tempfile.mkdtemp(prefix=f"rpkl-boundary-{label}-"))
    config = case_dir / "reaper.ini"
    config.write_text(
        "[REAPER]\n"
        "peakcachegenmode=3\n"
        f"peakcachegenrs={PPS}\n"
        "showpeaks=1\n"
        "altpeaks=2\n",
        encoding="utf-8",
    )
    status_path = case_dir / "result.txt"
    env = os.environ.copy()
    env.update(DISPLAY=DISPLAY, REAPEAKS_MEDIA=str(source.resolve()), REAPEAKS_RESULT=str(status_path))
    with (results / f"reaper-{label}.log").open("wb") as log:
        completed = subprocess.run(
            [str(reaper), "-newinst", "-cfgfile", str(config), "-new", "-nosplash", str(probe)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
    status = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
    if completed.returncode != 0 or "OK loops=" not in status:
        raise RuntimeError(f"REAPER failed label={label} rc={completed.returncode}: {status!r}")
    if not peak.exists():
        candidates = list(media.rglob("probe.wav.reapeaks"))
        if len(candidates) != 1:
            raise RuntimeError(f"expected one peak file for {label}, found {candidates}")
        peak = candidates[0]
    evidence = results / f"probe-{label}.reapeaks"
    if label in ("verify-low", "verify-high"):
        shutil.copy2(peak, evidence)
    return parse_fine_codes(peak)


pos_lo = [0] * POS_TRANSITIONS
pos_hi = [MAX_FINITE_MAG_BITS] * POS_TRANSITIONS
neg_lo = [0] * NEG_TRANSITIONS
neg_hi = [MAX_FINITE_MAG_BITS] * NEG_TRANSITIONS

xvfb_log = (results / "xvfb.log").open("wb")
xvfb = subprocess.Popen(
    ["Xvfb", DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
    stdout=xvfb_log,
    stderr=subprocess.STDOUT,
)

try:
    time.sleep(0.4)
    round_index = 0
    while True:
        pos_active = any(hi - lo > 1 for lo, hi in zip(pos_lo, pos_hi))
        neg_active = any(hi - lo > 1 for lo, hi in zip(neg_lo, neg_hi))
        if not pos_active and not neg_active:
            break
        pos_mid = [lo + (hi - lo) // 2 if hi - lo > 1 else hi for lo, hi in zip(pos_lo, pos_hi)]
        neg_mid = [lo + (hi - lo) // 2 if hi - lo > 1 else hi for lo, hi in zip(neg_lo, neg_hi)]
        pos_codes, neg_codes, _headers = run_reaper(pos_mid, neg_mid, f"round-{round_index:02d}")
        for index, mid in enumerate(pos_mid):
            target = index + 1
            if pos_hi[index] - pos_lo[index] <= 1:
                continue
            if pos_codes[index] >= target:
                pos_hi[index] = mid
            else:
                pos_lo[index] = mid
        for index, mid in enumerate(neg_mid):
            target = index + 1
            if neg_hi[index] - neg_lo[index] <= 1:
                continue
            if neg_codes[index] >= target:
                neg_hi[index] = mid
            else:
                neg_lo[index] = mid
        round_index += 1
        remaining = sum(hi - lo > 1 for lo, hi in zip(pos_lo, pos_hi)) + sum(
            hi - lo > 1 for lo, hi in zip(neg_lo, neg_hi)
        )
        print(f"RPKL_BOUNDARY_SEARCH_ROUND={round_index} remaining={remaining}", flush=True)
        if round_index > 32:
            raise RuntimeError("boundary search did not converge")

    pos_boundaries = pos_hi
    neg_boundaries = neg_hi

    pos_low_bits = [boundary - 1 for boundary in pos_boundaries]
    neg_low_bits = [boundary - 1 for boundary in neg_boundaries]
    pos_low_codes, neg_low_codes, headers = run_reaper(pos_low_bits, neg_low_bits, "verify-low")
    pos_high_codes, neg_high_codes, _ = run_reaper(pos_boundaries, neg_boundaries, "verify-high")

    failures = []
    for index, boundary in enumerate(pos_boundaries):
        expected_low = index
        expected_high = index + 1
        if pos_low_codes[index] != expected_low or pos_high_codes[index] != expected_high:
            failures.append(
                f"positive transition {index}->{index + 1} bits=0x{boundary:08x} "
                f"low={pos_low_codes[index]} high={pos_high_codes[index]}"
            )
    for index, boundary in enumerate(neg_boundaries):
        expected_low = index
        expected_high = index + 1
        if neg_low_codes[index] != expected_low or neg_high_codes[index] != expected_high:
            failures.append(
                f"negative transition {index}->{index + 1} bits=0x{boundary:08x} "
                f"low={neg_low_codes[index]} high={neg_high_codes[index]}"
            )

    if any(a >= b for a, b in zip(pos_boundaries, pos_boundaries[1:])):
        failures.append("positive decision boundaries are not strictly increasing")
    if any(a >= b for a, b in zip(neg_boundaries, neg_boundaries[1:])):
        failures.append("negative decision boundaries are not strictly increasing")

    (results / "positive_boundaries.u32le").write_bytes(
        b"".join(struct.pack("<I", value) for value in pos_boundaries)
    )
    (results / "negative_boundaries.u32le").write_bytes(
        b"".join(struct.pack("<I", value) for value in neg_boundaries)
    )

    exact_half_ties = []
    for index, boundary in enumerate(pos_boundaries[:24576]):
        value = f32_from_bits(boundary)
        scaled = value * 24576.0
        if scaled == index + 0.5:
            exact_half_ties.append((index, boundary, pos_high_codes[index]))
    report = {
        "oracle": "REAPER 7.79 x86_64 Linux",
        "source": "WAVE IEEE float32",
        "sample_rate": SAMPLE_RATE,
        "peakcachegenrs": PPS,
        "fine_division": headers[0][0],
        "fresh_reaper_processes": round_index + 2,
        "search_rounds": round_index,
        "positive_transition_count": len(pos_boundaries),
        "negative_transition_count": len(neg_boundaries),
        "positive_first_boundary_bits": pos_boundaries[0],
        "positive_last_boundary_bits": pos_boundaries[-1],
        "negative_first_boundary_bits": neg_boundaries[0],
        "negative_last_boundary_bits": neg_boundaries[-1],
        "linear_exact_half_tie_count": len(exact_half_ties),
        "linear_exact_half_tie_examples": [
            {"lower_code": code, "bits": bits, "reaper_upper_code": upper}
            for code, bits, upper in exact_half_ties[:32]
        ],
        "verification_failures": failures[:100],
    }
    (results / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = (
        f"RPKL_FINITE_BOUNDARIES positive={len(pos_boundaries)} negative={len(neg_boundaries)} "
        f"rounds={round_index} half_ties={len(exact_half_ties)} failures={len(failures)}"
    )
    (results / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    print(summary, flush=True)
    if failures:
        for failure in failures[:20]:
            print("BOUNDARY_FAILURE", failure, flush=True)
        raise SystemExit(1)
finally:
    xvfb.terminate()
    try:
        xvfb.wait(timeout=3)
    except subprocess.TimeoutExpired:
        xvfb.kill()
    xvfb_log.close()
