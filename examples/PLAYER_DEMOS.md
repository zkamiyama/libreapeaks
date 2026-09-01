# Audio-player reference demos

The repository contains desktop and browser reference players that exercise the
GUI-facing libreapeaks APIs with REAPER-style interaction. Both are still demo
applications rather than production DAWs, but they deliberately expose the
important cache, level-selection, upload, and residency behavior instead of
hiding it behind a single pre-rendered waveform image.

The preferred full-analysis cache shape is the REAPER 7.79 native combination:

```text
waveform + -'s' spectral peaks + -'g' spectrogram + -'r' loudness
```

libreapeaks also supports the two other cache-generation shapes observed from
REAPER 7.79:

```text
waveform only
waveform + -'s' + -'r'
```

It intentionally does not present arbitrary `s-only`, `g-only`, or `r-only`
cache-generation modes as REAPER-native choices.

## Build the Python module

`pyproject.toml` enables the Python bindings and strict-WDL compatibility path
for maturin builds:

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

The desktop player exposes the REAPER-native generation modes in its cache
creation dialog. Cache generation runs off the UI thread and reports progress.
The large analysis canvas can use the packed OpenGL/GLSL path when the cache
contains the corresponding layers; the QPainter/tiled path remains available
as a fallback/reference implementation.

The packed path keeps display-domain work on the GPU:

```text
waveform  -> RGBA8UI
-'s'      -> RGBA8UI
-'g'      -> R8UI, exact packed 12-bit bytes
-'r'      -> RG32F
```

The `-'g'` layer is not expanded to u16 or recolored on the CPU. The shader
unpacks the 12-bit codes, applies spectrogram gain and the selected heatmap,
and composites waveform / spectral / loudness overlays. Gain and palette
changes therefore do not rebuild an RGBA spectrogram texture.

Interaction is REAPER-oriented:

- mouse wheel: horizontal time zoom anchored under the pointer;
- `Ctrl` + mouse wheel: vertical waveform full-scale zoom;
- drag: horizontal pan;
- click: move the playhead; the next Play starts at that time.

The GPU debug strip shows which waveform / `-'s'` / `-'g'` / `-'r'` windows are
resident and which areas are currently unloaded.

## 2. Browser / JavaScript player

Start the web demo with a full REAPER-native analysis cache:

```bash
python examples/web_player/server.py /path/to/audio.wav \
  --generation-mode spectrogram
# open http://127.0.0.1:8765/
```

Compressed source example:

```bash
python examples/web_player/server.py song.opus \
  --cache-decoder ffmpeg \
  --generation-mode spectrogram
```

`--generation-mode` accepts:

```text
waveform     -> waveform only
spectral     -> waveform + -'s' + -'r'
spectrogram  -> waveform + -'s' + -'g' + -'r'
```

The browser offers two renderers from the same cache:

1. **WebGL2 packed** — preferred when WebGL2 is available;
2. **Canvas2D tiled** — fallback and comparison path.

### Packed WebGL2 path

The WebGL2 path deliberately bypasses the CPU display-texture conversion APIs.
The HTTP server owns a `reapeaks.GpuCacheView`, indexes the `.reapeaks` file
once, and serves exact on-disk record windows:

```text
GET /api/gpu-records?kind=waveform&layer=L&first=N&count=C
GET /api/gpu-records?kind=spectral&layer=L&first=N&count=C
GET /api/gpu-records?kind=spectrogram&layer=L&first=N&count=C
GET /api/gpu-records?kind=loudness&layer=L&first=N&count=C
```

The browser uploads those response bodies directly:

```text
.reapeaks bytes
  -> HTTP ArrayBuffer
  -> WebGL2 integer/float texture
  -> GLSL ES 3.00 decode + composite
```

For `-'g'`, the HTTP body remains the exact 192 bytes per channel/time record.
The shader performs the 12-bit unpack:

```text
[a >> 4,
 ((a & 0x0f) << 4) | (b & 0x0f),
 b >> 4]
```

No browser-side u16 expansion is required. The WebGL2 texture shape is
`width = 192 * channels`, `height = records`, which preserves REAPER's
record-major/channel-inner byte order without repacking and avoids multiplying
texture height by channel count.

Raw windows are page-aligned and byte-budgeted. Wide-channel spectrogram
requests use smaller record pages so one viewport change does not upload an
unbounded packed texture. Every texture dimension is also checked against the
runtime `MAX_TEXTURE_SIZE` before upload.

The following operations are uniform-only redraws once the current raw windows
are resident:

- spectrogram gain;
- heatmap/grayscale selection;
- playhead movement;
- vertical waveform full-scale changes;
- GPU residency-strip visibility.

They do not fetch or upload `-'g'` again. Horizontal pan/zoom may select a new
mipmap/window and therefore fetch only the raw record pages needed for the new
viewport.

When `EXT_disjoint_timer_query_webgl2` is available, the diagnostics panel also
shows a GPU timer result. The CI browser benchmark uses Chrome headless with
ANGLE/SwiftShader, so its GPU milliseconds are useful for correctness and
regression detection but **must not be interpreted as representative hardware
GPU performance**. Hardware-GPU decisions should be based on measurements on
the target machines/browsers.

### Canvas2D fallback

The fallback intentionally exercises the higher-level tiled display APIs:

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

The browser keeps a 96-entry Promise-aware LRU keyed by exact tile identity.
Concurrent requests for the same tile share the same in-flight Promise. Stale
`/api/plan` work is aborted and wheel/drag render requests are coalesced with
`requestAnimationFrame`, reducing request storms on precision trackpads.

Both renderers share the same interaction model:

- wheel: cursor-anchored horizontal zoom;
- `Ctrl` + wheel: vertical waveform zoom, full-scale range `0.1..32`;
- drag: pan;
- click: seek.

The wheel math is regression-tested directly from `app.js`, including
fractional high-resolution trackpad deltas and cursor-anchor preservation.

## Browser WebGL2 CI benchmark

`.github/workflows/webgl2-web-player-benchmark.yml` creates a deterministic
12-second 48 kHz stereo PCM16 source, generates a full `wave+s+g+r` cache,
starts the real demo server, and drives Chrome with Selenium.

The gate verifies all of the following in an actual WebGL2 context:

- GLSL ES 3.00 shader compile/link;
- `RGBA8UI`, packed `R8UI`, and `RG32F` texture uploads;
- all four raw layer groups are present;
- initial raw HTTP request count equals the number of initial texture uploads,
  preventing duplicate startup fetches;
- repeated spectrogram-gain and `Ctrl`-wheel changes do not add raw HTTP
  requests or texture uploads;
- horizontal zoom can select and upload new raw pages;
- no browser console errors are produced by the player path.

The workflow records renderer/vendor/version, `MAX_TEXTURE_SIZE`, timer-query
support, CPU submit time, last fetch/upload time, upload counts/bytes, and raw
resource timing data as an artifact.

## Cache-path options

The runnable demos accept:

```text
--cache-mode auto|sidecar|subdir|central|reaper
--cache-dir PATH
--reaper-cache-map PATH
```

Their low-level semantics are:

- `sidecar` — cache beside the media;
- `subdir` — cache in a `peaks/` directory;
- `central` — the older **libreapeaks private SHA-256 namespace** under
  `--cache-dir`; this is not REAPER's canonical central naming;
- `reaper` — exact read/write path from `--reaper-cache-map`;
- `auto` — reuse available mapped/local candidates and otherwise fall back to a
  sidecar target.

The repository also contains the higher-level
`player_reaper_integration.py` policy, which gives `central` its intended
REAPER-canonical meaning and supports `reaper.ini`/live `GetPeakFileNameEx`
resolution.

For actual REAPER path sharing with the current runnable demos, build a cache
map with `tools/reaper_oracle/make_cache_map.py` and use:

```bash
python examples/pyside6_player.py song.flac \
  --cache-mode reaper \
  --reaper-cache-map /path/to/reaper-cache-map.json \
  --cache-decoder ffmpeg
```

See [`../docs/REAPER_CENTRAL_CACHE.md`](../docs/REAPER_CENTRAL_CACHE.md).

## libreapeaks APIs exercised

| API | Desktop | Web | Purpose |
|---|---:|---:|---|
| `ReaPeaks.open()` | yes | Canvas fallback | parsed display/cache inspection |
| `GpuCacheView.open()` | yes | WebGL2 | index exact raw layer windows without full display decode |
| `GpuCacheView.levels()` | yes | WebGL2 | raw waveform / `s` / `g` / `r` level metadata |
| `GpuCacheView.records()` | yes | WebGL2 | exact on-disk record-window bytes |
| `sample_rate`, `channels`, `wave_encoding` | yes | yes | display/decoding metadata |
| `levels()` | yes | Canvas fallback | native + lazy-derived pyramid metadata |
| `tile_peaks` | yes | Canvas fallback | frontend LRU/tile addressing |
| `plan_view()` | yes | Canvas fallback | zoom-dependent level choice |
| `tiles_for_view()` | yes | Canvas fallback | minimal visible waveform tile set |
| `tile_texture()` | yes | Canvas fallback | lossless max/min RGBA8 tile |
| `spectral_tile_texture()` | yes | Canvas fallback | lossless REAPER spectral u32 tile |
| `render_rgba()` | overview | overview | small full-file CPU overview |
| `default_divisions()` | yes | yes | diagnostics/cache generation |
| native REAPER-mode Python writer | yes | when needed | build one of the three REAPER-observed cache shapes |

## Data-texture decoding summary

Waveform records contain two signed i16 codes per channel:

```text
max i16 little-endian
min i16 little-endian
```

`-'s'` records contain the exact little-endian REAPER u32 code:

```text
frequency_hz = code & 0x7fff
density      = (code >> 15) & 0x3fff
```

`-'g'` records contain 128 12-bit values per channel, packed into 192 bytes.
The packed WebGL/OpenGL paths preserve those bytes and decode them in shader
code rather than expanding/recoloring on the CPU.

`-'r'` records expose two f32 values per channel/record and are uploaded as a
two-component float texture for the shader overlay.
