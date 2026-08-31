# REAPER central peak-cache integration

## What “central” means

REAPER has two separate responsibilities around `.reapeaks` files:

1. **Path policy** — decide the read and write pathname for a given media
   source, according to the active REAPER resource/configuration.
2. **Peak generation** — decode the media and write the cache bytes.

libreapeaks only needs REAPER for the first responsibility. The application can
ask REAPER's `GetPeakFileNameEx` API for the canonical path and then decode and
generate the cache entirely outside REAPER.

This distinction is important: a `reaper-cache-map` is not a collection of peak
files and it is not proof that REAPER generated them. It is a persisted set of
path-policy answers.

## Cache modes in the reference players

| Mode | Meaning |
|---|---|
| `sidecar` | `audio.ext.reapeaks` next to the media |
| `subdir` | `peaks/audio.ext.reapeaks` below the media directory |
| `central` | Exact REAPER central path; fails closed without a canonical resolver |
| `reaper` | Exact path selected by REAPER, whether sidecar, subdirectory, or central |
| `private-central` | libreapeaks-only SHA-256 namespace; collision-safe but not claimed to match REAPER |
| `auto` | Reuse an existing cache; use a canonical resolver when supplied; otherwise sidecar fallback |

`central` deliberately does **not** fall back to the old private SHA filename.
Writing a syntactically valid cache under the wrong name would make REAPER
ignore it and rebuild another file.

## Loading `reaper.ini`

Both demos accept:

```text
--reaper-ini PATH
--auto-reaper-ini
--fine-peaks-per-second N
```

The application reads these `[REAPER]` keys when present:

```ini
peakcachegenrs=300
peakcachegenmode=3
altpeaks=...
altpeakspath=...
altpeaksopathlist=...
```

The precedence is:

```text
explicit --divisions
  > explicit --fine-peaks-per-second
  > peakcachegenrs from reaper.ini
  > 300
```

Unknown `altpeaks` bits and path-list matching rules are not guessed. Exact path
selection is delegated to `GetPeakFileNameEx`, which applies the active REAPER
version's own rules.

## Live path query

Pass the executable and, for portable or non-default installations, the INI:

```bash
python examples/pyside6_player.py song.flac \
  --cache-mode central \
  --reaper-executable /path/to/reaper \
  --reaper-ini /path/to/reaper.ini \
  --cache-decoder ffmpeg
```

The player starts a short-lived REAPER process, creates a `PCM_source`, calls
`GetPeakFileNameEx` for read and write paths, destroys the source, and exits. It
does **not** call `PCM_Source_BuildPeaks`.

libreapeaks then generates and atomically publishes the cache at the returned
write path. Consequently the audio decode, spectral analysis, loudness
generation, and cache bytes remain external to REAPER.

## Persisted `reaper-cache-map`

A map avoids launching REAPER on later runs:

```bash
python tools/reaper_oracle/make_cache_map.py \
  --reaper-executable /path/to/reaper \
  --reaper-ini /path/to/reaper.ini \
  --output ~/.cache/libreapeaks/reaper-cache-map.json \
  --recursive /media/library
```

Then:

```bash
python examples/pyside6_player.py /media/library/song.flac \
  --cache-mode central \
  --reaper-ini /path/to/reaper.ini \
  --reaper-cache-map ~/.cache/libreapeaks/reaper-cache-map.json \
  --cache-decoder ffmpeg
```

The map stores one absolute media key with canonical `read`, `write`, and
source-type fields. Existing v1 string maps and single-record query results
remain readable.

A map is optional. The choices are:

* live official query;
* saved query result;
* `private-central` when REAPER interoperability is not required.

A pure offline clone of REAPER's undocumented filename algorithm is enabled
only after it is proven against real REAPER matrices. Until then, strict
`central` mode fails rather than guessing.

## C API division helper

C callers can either pass any explicit division array to
`rpk_generate_pcm16` / `rpk_generate_f32`, or ask the library to derive the
three REAPER-style divisions for a configured peak rate:

```c
uint32_t divisions[3];
if (rpk_default_divisions(48000, 500, divisions) != 0) {
  /* invalid zero argument or null output */
}
```

For this example, the result is `96, 2400, 48000`. The second argument
corresponds to REAPER's `peakcachegenrs`; it is not fixed at 300.
