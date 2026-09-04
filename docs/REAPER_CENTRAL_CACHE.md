# REAPER cache paths, `reaper.ini`, and demo settings

Generating compatible `.reapeaks` bytes and choosing where to publish them are
separate concerns. libreapeaks does not require REAPER to generate the cache
contents, and the normal demo default does not require REAPER path policy either.

## Default: sidecar beside the source

With no saved configuration or explicit CLI override, the reference demos write:

```text
song.wav
song.wav.reapeaks
```

This is intentionally the least surprising standalone behavior. Installing
REAPER must not silently change where the demos write files.

## REAPER-compatible choices are opt-in

The shared demo settings expose four user-facing placement policies:

| Policy | Meaning |
|---|---|
| `sidecar` | Write beside the source. This is the default. |
| `subdir` | Write under the source directory's `peaks/` subdirectory. |
| `reaper-central` | Use REAPER's recovered central-cache filename algorithm in an explicitly selected cache directory. REAPER is not required. |
| `reaper-config` | Read `reaper.ini`, including `peakcachegenrs` and peak-path preferences, and follow the locally reproducible REAPER policy. |

The old libreapeaks SHA-256 namespace remains available internally as
`private-central`, but it is an advanced compatibility mode and is intentionally
not presented as REAPER central storage in the GUI.

## Recovered REAPER central filename

The central-cache probe in `tools/reaper_oracle/probe_central_cache.py` validates
the REAPER 7.79 central naming shape as:

```text
source = absolute source path
hash   = SHA1(lowercase(source) encoded as UTF-8)
path   = ALTPEAKSPATH / hash[0:2] / (hash + ".reapeaks")
```

The demo-layer implementation is
`examples/demo_cache_config.py::reaper_central_peak_path()`.

This lets an application create a REAPER-compatible central cache without a
REAPER executable. `GetPeakFileNameEx` is therefore an oracle/verification path,
not a prerequisite for the common central-cache case.

The recovered algorithm is treated as a versioned compatibility claim rather
than a promise about every future REAPER release. The oracle tools remain useful
for checking new versions and unusual path-policy combinations.

## Following `reaper.ini`

`examples/reaper_config.py` reads these `[REAPER]` values when present:

```ini
peakcachegenrs=300
peakcachegenmode=3
altpeaks=...
altpeakspath=...
altpeaksopathlist=...
```

The demo resolver uses the following peak-rate precedence:

```text
explicit CLI rate
  > saved demo-config peak rate
  > peakcachegenrs from REAPER.ini
  > 300
```

For cache placement, the offline resolver reproduces the common, validated
choices without REAPER:

- normal sidecar placement when no alternate path policy is enabled;
- global alternate/central placement using `altpeakspath`;
- central filename derivation using the recovered SHA-1 rule.

Selective `altpeaksopathlist` matching and unfamiliar `altpeaks` flag
combinations are deliberately **not** guessed from the INI. Those cases fall
back to `GetPeakFileNameEx` (or a previously saved cache map). This preserves the
standalone benefit for the common cases while keeping opaque REAPER policy
semantics fail-safe.

## Optional `GetPeakFileNameEx` verification

When `Verify derived paths with installed REAPER` is enabled, the demo launches
a short-lived REAPER process (or uses a saved map) and queries:

```text
GetPeakFileNameEx(source, ..., forWrite)
```

The query does not call `PCM_Source_BuildPeaks`. libreapeaks still decodes the
media and generates the `.reapeaks` bytes itself.

If the oracle disagrees with the locally derived target, the exact REAPER answer
wins and the path origin is reported as an oracle override. This makes live
REAPER useful as a compatibility verifier without making it a normal runtime
dependency.

The legacy CLI spelling `--cache-mode reaper` remains the force-exact path. It
uses a saved map when supplied or resolves REAPER from the configured executable,
`REAPER_EXE`, or the normal executable search path.

## Persistent settings shared by the demos

Desktop and Web DAW demos use the same JSON configuration. The default locations
are platform user-config directories, for example:

```text
Linux:   ~/.config/libreapeaks/demo-config.json
macOS:   ~/Library/Application Support/libreapeaks/demo-config.json
Windows: %APPDATA%\libreapeaks\demo-config.json
```

`LIBREAPEAKS_DEMO_CONFIG` can override the file location for tests or custom
launchers.

Conceptually the file is:

```json
{
  "version": 1,
  "cache": {
    "policy": "sidecar",
    "cache_directory": "",
    "reaper_ini": "",
    "auto_reaper_ini": false,
    "verify_with_reaper": false,
    "reaper_executable": "",
    "peak_rate": null
  }
}
```

The PySide cache-preparation dialog has a **Cache settings…** editor. The Web DAW
page exposes the same settings through `/api/config` and persists them to the
same file. Saved settings apply to later cache preparations; explicit CLI cache
placement still takes precedence.

## Browser-upload limitation

A browser `File` upload intentionally does not reveal the source's original
absolute filesystem path. That path is part of REAPER's central-cache key and is
also needed to interpret path-specific `reaper.ini` rules.

Therefore the Web DAW demo refuses `reaper-central` / `reaper-config` placement
for temporary browser uploads instead of hashing the server's temporary upload
path. To demonstrate exact REAPER placement in the Web app, start the server with
a real server-side source path, for example:

```bash
python examples/web_player/daw_server.py /media/library/song.flac
```

Browser uploads continue to work with the default sidecar or `peaks/` policies.

## Persisted exact cache maps

REAPER answers can still be saved for offline reuse:

```bash
python tools/reaper_oracle/make_cache_map.py \
  --reaper-executable /path/to/reaper \
  --reaper-ini /path/to/reaper.ini \
  --output ~/.cache/libreapeaks/reaper-cache-map.json \
  --recursive /media/library
```

The current map format is version 2. It stores path-policy answers, not peak
files. Maps are particularly useful for CI, compatibility research, and cases
where REAPER path-policy behavior is intentionally treated as opaque.

## Low-level private central namespace

`examples/player_common.py::central_peak_path()` is deliberately different from
REAPER central naming. It constructs a collision-resistant libreapeaks-private
SHA-256 filename inside an application-supplied directory.

Do not use that function when the goal is for REAPER to discover the same cache.
The user-facing REAPER central resolver lives in `demo_cache_config.py`.

## Cache generation and publication

The current desktop and Web DAW preparation paths use
`ensure_reapeaks_native()` and the native REAPER-shaped Python writer. The full
`spectrogram` shape is:

```text
waveform + -'s' spectral + -'g' spectrogram + -'r' loudness
```

Publication remains defensive: source geometry is validated, decoded output is
bounded, an exclusive cache lock is used, output is written to a temporary file
in the target directory, the source stamp is checked before publication, and the
final file is atomically replaced.

For byte-identity claims and permanent REAPER oracle coverage, see
`COMPATIBILITY.md`.
