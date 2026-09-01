#!/usr/bin/env python3
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

repo = Path.cwd()
root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/spectrogram-stress")
reaper = Path(sys.argv[2] if len(sys.argv) > 2 else os.environ["REAPER_BIN"])
media = root / "media"
results = root / "results"
results.mkdir(parents=True, exist_ok=True)
cases = json.loads((media / "cases.json").read_text(encoding="utf-8"))
probe = (repo / "tools/reaper_oracle/build_probe.lua").resolve()
display = ":95"
xvfb_log = (results / "xvfb.log").open("wb")
xvfb = subprocess.Popen(
    ["Xvfb", display, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
    stdout=xvfb_log,
    stderr=subprocess.STDOUT,
)

try:
    time.sleep(0.4)
    for index, case in enumerate(cases):
        name = case["name"]
        source = media / f"{name}.wav"
        peak_name = source.name + ".reapeaks"
        for old in media.rglob(peak_name):
            old.unlink()
        case_dir = Path(tempfile.mkdtemp(prefix=f"spectrogram-stress-{index:03d}-"))
        config = case_dir / "reaper.ini"
        config.write_text(
            "[REAPER]\n"
            "peakcachegenmode=3\n"
            f"peakcachegenrs={case['pps']}\n"
            "showpeaks=1345\n",
            encoding="utf-8",
        )
        status_path = case_dir / "result.txt"
        env = os.environ.copy()
        env.update(
            DISPLAY=display,
            REAPEAKS_MEDIA=str(source.resolve()),
            REAPEAKS_RESULT=str(status_path),
        )
        with (results / f"{name}.reaper.log").open("wb") as log:
            completed = subprocess.run(
                [str(reaper), "-newinst", "-cfgfile", str(config), "-new", "-nosplash", str(probe)],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=120,
                check=False,
            )
        status = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
        (results / f"{name}.status.txt").write_text(status, encoding="utf-8")
        source_type = next((line[5:] for line in status.splitlines() if line.startswith("TYPE=")), "")
        if completed.returncode != 0 or "OK loops=" not in status:
            raise SystemExit(f"{name}: REAPER failed rc={completed.returncode}: {status!r}")
        if source_type != "WAVE":
            raise SystemExit(f"{name}: expected WAVE source, got {source_type!r}")
        peaks = list(media.rglob(peak_name))
        if len(peaks) != 1:
            raise SystemExit(f"{name}: expected one peak file, found {peaks}")
        shutil.copy2(peaks[0], results / f"{name}.reaper.reapeaks")
        if (index + 1) % 20 == 0 or index + 1 == len(cases):
            print(f"REAPER_STRESS_PROGRESS={index + 1}/{len(cases)}", flush=True)
finally:
    xvfb.terminate()
    try:
        xvfb.wait(timeout=3)
    except subprocess.TimeoutExpired:
        xvfb.kill()
    xvfb_log.close()

failures = 0
summary = []
interesting = (
    "SPECTROGRAM_EXACT_STATS",
    "spectrogram mismatch",
    "layer header table differs",
    "non-spectrogram layer",
    "RPKN header differs",
    "whole RPKN file differs",
)
for index, case in enumerate(cases):
    name = case["name"]
    env = os.environ.copy()
    env.update(
        REAPEAKS_PCM16=str((media / f"{name}.s16le").resolve()),
        REAPEAKS_ORACLE=str((results / f"{name}.reaper.reapeaks").resolve()),
        LIBREAPEAKS_OUTPUT=str((results / f"{name}.libreapeaks.reapeaks").resolve()),
    )
    completed = subprocess.run(
        [
            "cargo", "test", "--release", "--features", "strict-wdl",
            "--test", "reaper_spectrogram_exact",
            "reaper779_pcm16_spectrogram_is_byte_identical",
            "--", "--ignored", "--exact", "--nocapture",
        ],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
        check=False,
    )
    (results / f"{name}.test.txt").write_text(completed.stdout, encoding="utf-8")
    if completed.returncode == 0:
        detail = next((line for line in completed.stdout.splitlines() if "SPECTROGRAM_BYTE_IDENTICAL" in line), "")
        summary.append(f"PASS {name}\n{detail}" if detail else f"PASS {name}")
    else:
        failures += 1
        lines = [line for line in completed.stdout.splitlines() if any(key in line for key in interesting)]
        summary.append(f"FAIL {name} rc={completed.returncode}\n" + "\n".join(lines[-30:]))
        print(summary[-1], flush=True)
    if (index + 1) % 20 == 0 or index + 1 == len(cases):
        print(f"STRESS_COMPARE_PROGRESS={index + 1}/{len(cases)} failures={failures}", flush=True)

summary.append(f"SPECTROGRAM_STRESS_TOTAL={len(cases)} FAILURES={failures}")
(results / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
print(summary[-1], flush=True)
raise SystemExit(min(failures, 125))
