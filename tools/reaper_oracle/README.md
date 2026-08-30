# REAPER live-oracle harness

This directory contains the harness used to generate REAPER 7.79 compatibility
fixtures.

## Critical rule

**One media file must be processed by one fresh REAPER process.**

Early reverse-engineering runs processed many media sources in one process and
observed spectral results that depended on the preceding source. Xvfb can be
kept alive across the run, but REAPER itself is restarted for every media file.

## Usage

Extract a Linux REAPER build and place test media directly in one directory:

```bash
python tools/reaper_oracle/run_fresh.py \
  --reaper /path/to/REAPER/reaper \
  --media-dir /path/to/probes \
  --peak-rate 300
```

The runner:

1. starts one Xvfb server;
2. writes an isolated REAPER configuration;
3. removes an existing peak cache for the current media;
4. launches a **new REAPER process** with `build_one.lua`;
5. calls `PCM_Source_BuildPeaks` until completion;
6. waits for REAPER to quit;
7. parses the generated `.reapeaks` file;
8. prints each spectral mipmap's division, count, and FNV-1a hash;
9. repeats with another fresh REAPER process for the next media.

Example output:

```text
# name  magic  sample_rate  channels  divisions        level_counts  level_fnv64
probe   RPKN   48000        2         160,2400,48000   5993,399,19  ...
```

`peakcachegenmode=3` and `peakcachegenrs=300` are used by default to match the
main reverse-engineering corpus. Change the peak rate when testing another
preference configuration.

The parser currently treats REAPER 7.79 `-'r'` loudness payloads as 4 bytes per
channel/sample solely so it can skip them while locating spectral data. The
loudness writer remains outside the strict spectral compatibility claim.
