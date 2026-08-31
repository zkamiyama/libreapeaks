# REAPER cache paths and `reaper.ini`

Generating correct `.reapeaks` bytes and placing them at the path REAPER expects
are two different compatibility problems. This document describes the current
application-layer helpers **and** the current state of the runnable demo CLIs.

## The rule that matters

**Do not invent a REAPER central-cache filename from `altpeakspath`.**

For a given media source, the canonical read/write path is selected by REAPER's
own path policy. libreapeaks delegates that decision to the public REAPER API:

```text
GetPeakFileNameEx(source, ..., forWrite)
```

A short-lived REAPER process can answer that question without calling
`PCM_Source_BuildPeaks`. Once the exact path is known, another application can
decode the media and generate/publish the `.reapeaks` bytes itself.

## Three separate concepts

### 1. Sidecar or application-chosen paths

These are paths an application can construct without REAPER, for example:

```text
song.wav.reapeaks
peaks/song.wav.reapeaks
```

They are useful application caches, but they are only REAPER-interoperable when
REAPER itself would choose the same location.

### 2. REAPER canonical path

This is the exact read/write path returned by `GetPeakFileNameEx` under the
active REAPER configuration. It may be a sidecar, subdirectory, or central path.

This is the path to use when the goal is actual REAPER cache sharing.

### 3. libreapeaks private central namespace

`examples/player_common.py::central_peak_path()` constructs a collision-resistant
SHA-256-based filename inside an application-supplied directory. It is useful
for a private shared cache, but **it is not claimed to reproduce REAPER's
central filename algorithm**.

The higher-level policy module calls this mode `private-central`.

## Reading `reaper.ini`

`examples/reaper_config.py` is the current reusable configuration/path helper.
It reads the following `[REAPER]` values when present:

```ini
peakcachegenrs=300
peakcachegenmode=3
altpeaks=...
altpeakspath=...
altpeaksopathlist=...
```

The peak-rate precedence implemented by the higher-level policy is:

```text
explicit peak rate
  > peakcachegenrs from explicit/auto-discovered reaper.ini
  > 300
```

`peakcachegenrs` is not fixed at 300. For example:

```text
48,000 Hz / 300 -> [160, 2400, 48000]
48,000 Hz / 500 -> [96, 2400, 48000]
```

Unknown `altpeaks` matching semantics are not reimplemented. Path selection is
delegated to REAPER.

## Live canonical path query

`query_reaper_peak_paths()`:

1. resolves the media and REAPER executable;
2. optionally launches REAPER with an explicit `reaper.ini`;
3. creates a `PCM_source` from the media;
4. calls `GetPeakFileNameEx` for read and write paths;
5. records the media source type;
6. destroys the source and exits.

It does **not** call `PCM_Source_BuildPeaks`.

Malformed results fail closed. The current implementation rejects non-object
JSON, non-string paths/source types, missing write paths, query failures, and
timeouts instead of leaking an `AttributeError` or coercing unexpected values.

## Persisting canonical answers in a cache map

Use the helper tool to query REAPER once and save the answers:

```bash
python tools/reaper_oracle/make_cache_map.py \
  --reaper-executable /path/to/reaper \
  --reaper-ini /path/to/reaper.ini \
  --output ~/.cache/libreapeaks/reaper-cache-map.json \
  --recursive /media/library
```

The current JSON format is version 2 and contains path-policy data, not peak
files. Conceptually:

```json
{
  "version": 2,
  "source": "REAPER GetPeakFileNameEx",
  "reaper_ini": "/path/to/reaper.ini",
  "entries": {
    "/absolute/media/song.flac": {
      "media": "/absolute/media/song.flac",
      "read": "/path/chosen/by/REAPER",
      "write": "/path/chosen/by/REAPER",
      "source_type": "VIDEO",
      "origin": "GetPeakFileNameEx"
    }
  }
}
```

Legacy v1/single-record forms remain readable. An explicitly declared unknown
or malformed version is rejected rather than interpreted as the current format.

A cache map is safe to use offline only as long as it still represents the
REAPER configuration/path policy you intend to follow.

# Higher-level policy helper

`examples/player_reaper_integration.py` contains the application-level policy
that was designed for full REAPER integration. Its modes are:

| Mode | Higher-level meaning |
|---|---|
| `sidecar` | Application sidecar path |
| `subdir` | Application `peaks/` subdirectory |
| `central` | Exact REAPER central path; requires a canonical resolver and validation against `reaper.ini` or `--cache-dir` |
| `reaper` | Exact path returned by REAPER, regardless of whether it is central or sidecar |
| `private-central` | libreapeaks SHA-256 namespace; not claimed to match REAPER |
| `auto` | Reuse an existing cache; if REAPER configuration requests a non-default path, fail closed unless an exact resolver is available |

This module can combine:

- explicit peak paths;
- explicit peak rate;
- `reaper.ini` discovery/loading;
- live `GetPeakFileNameEx` queries;
- persisted cache maps;
- strict central-directory validation.

# Important: current runnable demo CLI wiring

The current `examples/pyside6_player.py` and
`examples/web_player/server.py` do **not yet call**
`resolve_player_peak_policy()`. They call the lower-level
`player_common.ensure_reapeaks()` directly.

That means their command-line mode names currently have older semantics:

| Demo CLI mode | Current runnable behavior |
|---|---|
| `auto` | Reuse mapped/sidecar/subdir/private-directory candidates, otherwise write sidecar |
| `sidecar` | Write beside the media |
| `subdir` | Write under `peaks/` |
| `central` | **Private SHA-256 cache under `--cache-dir`**; this is not canonical REAPER central naming |
| `reaper` | Use the exact path from `--reaper-cache-map` |

The demo CLIs currently expose `--reaper-cache-map`, but not
`--reaper-executable`, `--reaper-ini`, `--auto-reaper-ini`, or the
`private-central` name used by the higher-level policy helper.

This mismatch is application wiring debt, not a property of the Rust core.
Documentation must not describe the current demo's `--cache-mode central` as a
REAPER-canonical mode.

## Correct REAPER-path workflow with the current demos

For the runnable demos today, use a persisted map and `--cache-mode reaper`:

```bash
python tools/reaper_oracle/make_cache_map.py \
  --reaper-executable /path/to/reaper \
  --reaper-ini /path/to/reaper.ini \
  --output ~/.cache/libreapeaks/reaper-cache-map.json \
  /media/library/song.flac
```

Then:

```bash
python examples/pyside6_player.py /media/library/song.flac \
  --cache-mode reaper \
  --reaper-cache-map ~/.cache/libreapeaks/reaper-cache-map.json \
  --cache-decoder ffmpeg \
  --fine-peaks-per-second 300
```

Use the actual `peakcachegenrs` value from the REAPER configuration instead of
`300` when it differs. The current demo CLI does not automatically copy that
value out of the cache map or `reaper.ini`.

The browser server accepts the same cache-generation/path options.

# Cache generation scope in the current demos

The runnable Python demos call the public Python functions:

```text
reapeaks.generate_pcm16
reapeaks.generate_f32
```

Those functions currently generate waveform plus optional `-'s'` spectral
layers. They do **not** call the Rust-only complete mode-3 writer, so the demos
do not currently generate `-'r'` loudness layers.

The Rust core's complete mode-3 entry points are:

```text
generate_pcm16_mode3
generate_f32_mode3
```

This distinction matters when using the live whole-file byte-identity results
as a compatibility claim: those strongest mode-3 oracle results apply to the
Rust mode-3 writer, not automatically to the current Python demo writer surface.

# Publishing and cache safety

The lower-level player helper performs defensive publication:

- validates source geometry and division values;
- limits decoded output size and decode time;
- uses an exclusive cache lock;
- writes a temporary file in the target directory;
- parses and checks source size/mtime metadata before publication;
- atomically replaces the final target;
- avoids publishing if the source changes during decode/generation.

The config/cache-map code also fails closed on malformed map versions and
unexpected path-query result types.

# C API division helper

C callers may supply any valid explicit division array to the writer or derive
REAPER-style divisions from a configured peak rate:

```c
uint32_t divisions[3];
if (rpk_default_divisions(48000, 500, divisions) != 0) {
  /* sample rate / peak rate was zero, or output was null */
}
```

The result is:

```text
[96, 2400, 48000]
```

# Validation notes

The repository includes real-REAPER workflows for central path research and
integration, but the central-cache integration workflow is not currently one of
the always-on `main`/pull-request gates. Treat the current source code and unit
tests as authoritative for API behavior, and the permanent whole-file oracle
workflows in `COMPATIBILITY.md` as the authoritative continuous byte-identity
claim.
