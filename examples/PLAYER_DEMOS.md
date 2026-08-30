# Tiled audio-player reference demos

These examples show how the GUI-facing libreapeaks APIs fit into a real audio
player. Both demos deliberately expose tile identities on screen so it is easy
to verify that panning/zooming does **not** build or upload one giant waveform.

## Build the Python module

The project `pyproject.toml` enables the `python` and `strict-wdl` features by
default:

```bash
python -m pip install -U maturin
maturin develop --release
```

If an existing REAPER cache is next to the media, both players reuse it. If no
cache exists and the source is PCM16 or IEEE-float32 WAV, the demos can generate
one with libreapeaks itself. Other media formats can still be played, but need
an existing `.reapeaks` cache because the examples intentionally do not bundle
an audio decoder.

## 1. PySide6 desktop player

```bash
python -m pip install PySide6
python examples/pyside6_player.py /path/to/audio.wav
```

Optional:

```bash
python examples/pyside6_player.py audio.wav --peaks /cache/audio.wav.reapeaks
python examples/pyside6_player.py audio.wav --rebuild-cache
```

The upper overview is the CPU fallback `render_rgba()` path. The large waveform
uses lossless RGBA8 data tiles, decoded by the Qt widget and drawn with
`QPainter`. The lower panel decodes the exact REAPER spectral u32 tile payload
into a logarithmic frequency trace; density controls opacity.

Mouse wheel zooms around the cursor, drag pans, click seeks. `L3 T12` means
waveform level 3/tile 12. `S1 T4` means spectral layer 1/tile 4. The status line
shows the currently selected division, peaks/pixel and LRU hit/miss counters.

## 2. Browser / JavaScript player

The web demo keeps libreapeaks native and exposes a very small HTTP tile API.
The JavaScript frontend uses the browser `<audio>` element for playback and
fetches only the visible binary RGBA8 tiles.

```bash
python examples/web_player/server.py /path/to/audio.wav
# open http://127.0.0.1:8765/
```

The request flow is intentionally visible in the code:

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

The browser maintains its own 96-entry LRU keyed by the exact tile identity.
This is the same cache-key model recommended for a native Qt/QRhi/WebGPU
application.

## libreapeaks APIs exercised

| API | Desktop | Web | Purpose |
|---|---:|---:|---|
| `ReaPeaks.open()` | yes | yes | parse/reuse existing cache |
| `sample_rate`, `channels`, `wave_encoding` | yes | yes | display/decoding |
| `levels()` | yes | yes | native + lazy-derived pyramid metadata |
| `tile_peaks` | yes | yes | frontend LRU/tile addressing |
| `plan_view()` | yes | yes | zoom-dependent level choice |
| `tiles_for_view()` | yes | yes | minimal visible waveform tile set |
| `tile_texture()` | yes | yes | lossless max/min RGBA8 tile |
| `spectral_tile_texture()` | yes | yes | lossless REAPER spectral u32 tile |
| `render_rgba()` | yes | yes | small full-file CPU overview |
| `envelope_texture()` | yes, coarsest level | yes, coarsest level | complete-level texture inspection |
| `default_divisions()` | yes | yes | diagnostics/cache generation |
| `generate_pcm16()` | when needed | when needed | build missing PCM16 WAV cache |
| `generate_f32()` | when needed | when needed | build missing float32 WAV/RPKL cache |

The full-level `envelope_texture()` call is intentionally limited to the
coarsest display level. Long-media interaction uses tiles only.

## Data-texture decoding

Wave tiles are row-major: width is peaks in the tile and height is channels.
Each RGBA8 texel packs the exact max/min codes:

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
Quick/QRhi/WebGL2/WebGPU frontend can upload the same bytes and move the decode
into a shader without changing the cache or tile protocol.
