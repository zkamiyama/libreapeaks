# Tiled audio-player reference demos

These examples show how the GUI-facing libreapeaks APIs fit into a small audio
player. Both demos deliberately expose tile identities and LRU statistics so it
is easy to see that panning/zooming reuses bounded 4096-peak tiles instead of
building or uploading one giant waveform.

They are reference applications, not production DAWs.

## Build the Python module

`pyproject.toml` enables both `python` and `strict-wdl` for maturin builds:

```bash
python -m pip install -U maturin
maturin develop --release
```

The distribution is named `libreapeaks`; the imported extension module is
`reapeaks`.

## Media decoding

The Rust core accepts decoded PCM; it does not contain a general media decoder.
The demos provide two cache-decode paths:

- a defensive built-in WAVE reader for supported PCM/float WAV files;
- an external FFmpeg/ffprobe path for compressed or otherwise unsupported
  sources.

`--cache-decoder auto` tries the WAVE reader first and falls back to FFmpeg when
the source is not a supported WAV representation. Use `--cache-decoder ffmpeg`
when you want the external decode path explicitly.

FFmpeg cache decoding is deterministic-oriented (`threads=1`, explicit audio
stream, no metadata) and is protected by a timeout and maximum decoded-byte
limit.

Playback decoding is separate from cache decoding. `--playback-decoder native`
lets the platform/browser backend open the source directly;
`--playback-decoder ffmpeg` creates a temporary float WAV for playback.

## 1. PySide6 desktop player

```bash
python -m pip install PySide6
python examples/pyside6_player.py /path/to/audio.wav
```

Compressed source example:

```bash
python examples/pyside6_player.py song.flac --cache-decoder ffmpeg
```

Useful cache options:

```bash
python examples/pyside6_player.py audio.wav --peaks /cache/audio.wav.reapeaks
python examples/pyside6_player.py audio.wav --rebuild-cache
python examples/pyside6_player.py audio.wav --divisions 160,2400,48000
python examples/pyside6_player.py audio.wav --fine-peaks-per-second 500
```

The upper overview uses the CPU `render_rgba()` path. The large waveform uses
lossless RGBA8 max/min data tiles, decoded by the Qt widget and drawn with
`QPainter`. The spectral overlay decodes the exact REAPER spectral u32 tile
payload into a logarithmic frequency trace; density controls opacity.

Mouse wheel zooms around the cursor, drag pans, click seeks. `L3 T12` means
waveform level 3/tile 12. `S1 T4` means spectral layer 1/tile 4. The status line
shows the selected division, peaks/pixel, and LRU hit/miss counters.

## 2. Browser / JavaScript player

The web demo keeps libreapeaks native and exposes a small HTTP tile API. The
JavaScript frontend uses the browser `<audio>` element (or the optional FFmpeg
playback conversion) and fetches only visible binary RGBA8 tiles.

```bash
python examples/web_player/server.py /path/to/audio.wav
# open http://127.0.0.1:8765/
```

Compressed source example:

```bash
python examples/web_player/server.py song.opus --cache-decoder ffmpeg
```

The request flow is intentionally visible:

```text
viewport
  -> GET /api/plan
       -> ReaPeaks.plan_view()
       -> ReaPeaks.tiles_for_view()
  -> GET /api/wave-tile?level=L&tile=T
       -> ReaPeaks.tile_texture()
  -> GET /api/spectral-tile?layer=S&tile=T
       -> ReaPeaks.spectral_tile_texture()
```

The browser maintains its own 96-entry LRU keyed by exact tile identity. This is
the same cache-key model recommended for a native Qt/QRhi/WebGPU application.

## Current cache-path options in the runnable demos

The two runnable CLIs currently accept:

```text
--cache-mode auto|sidecar|subdir|central|reaper
--cache-dir PATH
--reaper-cache-map PATH
```

Their current low-level semantics are:

- `sidecar` — cache beside the media;
- `subdir` — cache in a `peaks/` directory;
- `central` — the older **libreapeaks private SHA-256 namespace** under
  `--cache-dir`; this is not REAPER's canonical central naming;
- `reaper` — exact read/write path from `--reaper-cache-map`;
- `auto` — reuse available mapped/local candidates and otherwise fall back to a
  sidecar target.

The repository also contains the newer higher-level
`player_reaper_integration.py` policy, which gives `central` its intended
REAPER-canonical meaning and supports `reaper.ini`/live `GetPeakFileNameEx`
resolution. That policy is not yet wired into these two CLI parsers.

For actual REAPER path sharing with the current runnable demos, build a cache
map with `tools/reaper_oracle/make_cache_map.py` and use:

```bash
python examples/pyside6_player.py song.flac \
  --cache-mode reaper \
  --reaper-cache-map /path/to/reaper-cache-map.json \
  --cache-decoder ffmpeg
```

See [`../docs/REAPER_CENTRAL_CACHE.md`](../docs/REAPER_CENTRAL_CACHE.md).

## Current generation scope of the demos

The demos call the public Python writers:

```text
reapeaks.generate_pcm16()
reapeaks.generate_f32()
```

These generate waveform plus optional `-'s'` spectral layers. They do **not**
currently expose the Rust-only complete mode-3 loudness writer. Therefore a
cache generated by the demo is not the same API surface as the whole-file
mode-3 oracle described in `docs/COMPATIBILITY.md`.

The complete mode-3 writer currently exists in Rust as:

```text
generate_pcm16_mode3
generate_f32_mode3
```

## libreapeaks APIs exercised

| API | Desktop | Web | Purpose |
|---|---:|---:|---|
| `ReaPeaks.open()` | yes | yes | parse/reuse an existing cache |
| `sample_rate`, `channels`, `wave_encoding` | yes | yes | display/decoding metadata |
| `levels()` | yes | yes | native + lazy-derived pyramid metadata |
| `tile_peaks` | yes | yes | frontend LRU/tile addressing |
| `plan_view()` | yes | yes | zoom-dependent level choice |
| `tiles_for_view()` | yes | yes | minimal visible waveform tile set |
| `tile_texture()` | yes | yes | lossless max/min RGBA8 tile |
| `spectral_tile_texture()` | yes | yes | lossless REAPER spectral u32 tile |
| `render_rgba()` | yes | yes | small full-file CPU overview |
| `envelope_texture()` | yes, coarsest level | yes, coarsest level | complete-level texture inspection |
| `default_divisions()` | yes | yes | diagnostics/cache generation |
| `generate_pcm16()` | when needed | when needed | build RPKN waveform/spectral cache from PCM16 |
| `generate_f32()` | when needed | when needed | build RPKN/RPKL waveform/spectral cache from f32 |

The complete-level `envelope_texture()` call is intentionally limited to the
coarsest display level. Long-media interaction uses tiles.

## Data-texture decoding

Wave tiles are row-major: width is peaks in the tile and height is channels.
Each RGBA8 texel packs max/min codes:

```text
R,G = max i16 little-endian
B,A = min i16 little-endian
```

Spectral tiles use the exact little-endian REAPER code:

```text
code         = R | G<<8 | B<<16 | A<<24
frequency_hz = code & 0x7fff
density      = (code >> 15) & 0x3fff
```

The demos decode these textures on the CPU for readability. A production Qt
Quick/QRhi/WebGL2/WebGPU frontend can upload the same bytes and move decoding
into a shader without changing cache or tile identity.
