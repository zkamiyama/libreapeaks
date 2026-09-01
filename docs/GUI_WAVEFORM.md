# GUI waveform / spectral data model

This document describes the GUI-facing data structures that are implemented by
`WavePyramid`, the Python `ReaPeaks` class, and the C tile APIs.

## Current format scope

The interactive waveform pyramid currently requires materialized RPKN or RPKL
positive waveform layers. The parser recognizes RPKM files, but the compact
RPKM waveform payload is not currently decoded into `WavePyramid` data.

Spectral GUI access uses parsed `-'s'` layers. `-'r'` loudness is parsed by the
Rust core but is not currently exposed as a Python/C GUI texture API.

## Goals

The GUI path is designed for DAW-style interaction:

- horizontal zoom from sample-scale to hours;
- low CPU cost while scrubbing/scrolling;
- bounded incremental allocations;
- a data representation usable by Qt 6/PySide6, OpenGL/QRhi, WebGL2 and WebGPU;
- no second application-specific persistent waveform pyramid when compatible
  `.reapeaks` waveform data already exists.

The last point does not imply that `.reapeaks` contains individual samples.
When the finest peak bucket becomes visibly wider than a pixel, the reference
players switch to a bounded decoded source window. See
[`SOURCE_PCM_LOD.md`](SOURCE_PCM_LOD.md) for the exact-sample LOD, decoder, and
memory policy.

## Pyramid structure

`WavePyramid` has two kinds of levels:

1. **native** — exact positive-division waveform mipmaps stored in `.reapeaks`;
2. **derived display levels** — geometric levels (ratio 4 by default) represented
   only by metadata.

A derived level is not allocated in full. For a requested range, its max/min
pairs are aggregated from the finest native level on demand. With ratio 4 this
avoids roughly one third of the fine-level memory that an eager display pyramid
would otherwise add.

Level selection uses frames-per-pixel and chooses the stored/derived division
closest to about **1.5 peaks per pixel**. The number of levels is logarithmic,
so the scan is effectively constant-time for media-size purposes.

`.reapeaks` does not store exact source frame count. `WavePyramid::from_reapeaks`
therefore estimates an upper bound from the finest native level. The reference
players prefer an exact duration/frame count from the playback media when that
information is available.

## High-zoom handoff to source PCM

Peak tiles and source PCM are separate LOD domains. Keep using the persistent
RGBA8 `.reapeaks` pyramid until a finest peak spans about 1.5 pixels; then use
`plan_pcm_lod()` / `planPcmLod()` to request a bounded transient source page.
The planner accepts finite fractional UI coordinates, clamps them to the source
timeline, and rounds the source interval outwards to integer frame boundaries.
This preserves a partially visible endpoint sample after cursor-anchored wheel
zoom while keeping every decoder request integer-indexed and division-aligned.

At `division > 1`, the transient source page is reduced to exact on-demand
max/min `RG32F` records. At `division == 1`, it is an interleaved exact-sample
`R32F` texture. `plan_pcm_draw()` / `planPcmDraw()` maps its visible record
range to `x_origin`/`x_step`, identifies the value offset for each channel, and
enables connected lines and (from 3 px/frame by default) circular points. The
plan allocates no segment/point list, so CPU and shader renderers can share the
same geometry contract.

Use the range-access event separately from paint invalidation. A successful
access may be `decoded`, `cache-hit`, or `coalesced`; only `reader_ran=true`
means a real file read/decoder miss occurred. See
[`SOURCE_PCM_LOD.md`](SOURCE_PCM_LOD.md) for the event fields, memory limits,
host playback-cache adapter, and failure behavior.

## Tile identity

Default `tile_peaks = 4096`.

```text
WaveTileKey {
    level_index,
    tile_index
}
```

A tile covers:

```text
first_peak = tile_index * 4096
count      = min(4096, level_peak_count - first_peak)
```

The same key should be used by the frontend cache. A practical desktop LRU can
keep dozens to a few hundred decoded/data-texture tiles while the GPU keeps only
currently visible and neighboring tiles.

## Waveform RGBA8 data texture

The data texture is **not** a pre-rendered waveform image. It is a lossless
packing of max/min envelope codes so a shader or CPU renderer can choose its own
vertical scale, colors, fills, and antialiasing without rebuilding the cache.

Dimensions:

```text
width  = number of peaks in tile (<=4096)
height = source channels
storage = 4 bytes per texel
```

Byte packing:

```text
R = max low byte
G = max high byte
B = min low byte
A = min high byte
```

JavaScript byte decode:

```js
function i16(lo, hi) {
  const u = lo | (hi << 8);
  return u >= 0x8000 ? u - 0x10000 : u;
}

const maxCode = i16(r, g);
const minCode = i16(b, a);
```

For RPKN, decode normalized amplitude asymmetrically:

```js
const amp = code < 0 ? code / 32768.0 : code / 32767.0;
```

For RPKL:

```js
function rpkl(code) {
  const neg = code < 0;
  const m = Math.abs(code);
  const a = m <= 24576
    ? m / 24576.0
    : Math.pow(2.0, (m - 24576) / 1024.0);
  return neg ? -a : a;
}
```

When a GPU API exposes the texture through a normalized RGBA8 sampler rather
than integer/byte loads, reconstruct the byte values consistently before
combining them into i16/u32 fields. The CPU/Python APIs return the original raw
bytes.

## Spectral RGBA8 data texture

Each texel is the exact little-endian REAPER spectral u32:

```text
code = R | G<<8 | B<<16 | A<<24
frequency_hz = code & 0x7fff
density      = (code >> 15) & 0x3fff
```

This is compact enough to upload directly and lets a shader choose its own
frequency-to-color or frequency-to-height mapping.

## Python / PySide6

The Python object exposes:

```text
ReaPeaks.open()
levels()
plan_view()
tiles_for_view()
tile_texture()
envelope_texture()
spectral_tile_texture()
render_rgba()
```

For a CPU-rendered preview:

```python
raw = rp.render_rgba(width, height, start, end)
img = QImage(raw, width, height, width * 4, QImage.Format_RGBA8888)
```

Keep the Python `bytes` object alive while a zero-copy `QImage` references it,
or call `.copy()` on the QImage as the reference desktop player does.

For QRhi/OpenGL, use `tile_texture()` and upload the returned bytes as a small
RGBA8 data texture. The tile key is suitable for an LRU keyed by
`(level_index, tile_index)`.

## C ABI equivalents

The main C entry points are:

```text
rpk_plan_view
rpk_tile_peaks
rpk_tile_count
rpk_tile_texture_rgba8
rpk_level_texture_rgba8
rpk_spectral_layer_count
rpk_spectral_tile_texture_rgba8
rpk_render_rgba8
rpk_render_rgba8_scaled
```

See [`C_ABI.md`](C_ABI.md) for ownership and generation details.

## Browser

CPU image path:

```js
const image = new ImageData(new Uint8ClampedArray(bytes), width, height);
ctx.putImageData(image, 0, 0);
```

GPU path: upload tile bytes as a 2D RGBA8 texture and decode the packed
waveform/spectral fields in the shader. WebGPU is a natural fit for a new
application, but the packed representation also works in WebGL2.

## Why not render one giant texture?

A multi-hour source at a few hundred peaks per second produces millions of
peaks. A single texture either exceeds practical GPU dimensions or forces
needless uploads whenever only a small viewport is visible. The 4096-peak tiled
layout keeps uploads bounded and gives a natural prefetch granularity for
scrolling.

The bundled PySide6 and browser demos display tile IDs and LRU statistics so
this behavior can be inspected directly. See
[`../examples/PLAYER_DEMOS.md`](../examples/PLAYER_DEMOS.md).
