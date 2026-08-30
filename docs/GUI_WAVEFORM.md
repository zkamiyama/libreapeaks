# GUI waveform / spectral data model

## Goals

The GUI path is designed for DAW-style interaction:

- horizontal zoom from sample-scale to hours;
- low CPU cost while scrubbing/scrolling;
- bounded incremental allocations;
- a data representation usable by Qt 6/PySide6, OpenGL/QRhi, WebGL2 and WebGPU;
- no second application-specific waveform cache when `.reapeaks` already exists.

## Pyramid structure

`WavePyramid` has two kinds of levels:

1. **native** — exact positive-division wave mipmaps stored in `.reapeaks`;
2. **derived display levels** — geometric levels (ratio 4 by default) represented
   only by metadata.

A derived level is not allocated in full. For a requested range, its max/min
pairs are aggregated from the finest native level on demand. With ratio 4 this
avoids roughly one third of the fine-level memory that an eager display pyramid
would otherwise add.

Level selection uses frames-per-pixel and chooses the closest division to about
1.5 peaks/pixel. The number of levels is logarithmic, so the scan is effectively
constant-time for media-size purposes.

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

This key should also be the frontend cache key. A practical desktop LRU can
keep dozens to a few hundred decoded/data-texture tiles while the GPU keeps only
currently visible + neighboring tiles.

## Waveform RGBA8 data texture

The data texture is *not* a pre-rendered waveform image. It is a lossless
packing of the max/min envelope so a shader can draw at arbitrary vertical
scale/color without rebuilding cache data.

Dimensions:

```text
width  = number of peaks in tile (<=4096)
height = source channels
format = RGBA8_UNORM / 4 raw bytes per texel
```

Byte packing:

```text
R = max low byte
G = max high byte
B = min low byte
A = min high byte
```

JavaScript decode:

```js
function i16(lo, hi) {
  const u = lo | (hi << 8);
  return u >= 0x8000 ? u - 0x10000 : u;
}

const maxCode = i16(r, g);
const minCode = i16(b, a);
```

For RPKN, decode amplitude asymmetrically:

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

## Spectral RGBA8 data texture

Each texel is the exact little-endian REAPER spectral u32:

```text
code = R | G<<8 | B<<16 | A<<24
frequency_hz = code & 0x7fff
density      = (code >> 15) & 0x3fff
```

This is compact enough to upload directly and lets a shader choose its own
frequency-to-color/vertical mapping.

## PySide6 / Qt 6

For a CPU-rendered preview:

```python
raw = rp.render_rgba(width, height, start, end)
img = QImage(raw, width, height, width * 4, QImage.Format_RGBA8888)
```

Keep the Python `bytes` object alive as long as a zero-copy `QImage` references
it, or call `.copy()` on the QImage.

For QRhi/OpenGL, use `tile_texture()` and upload the returned bytes as RGBA8.
The tile key is suitable for an LRU keyed by `(level_index, tile_index)`.

## Browser

CPU image path:

```js
const image = new ImageData(new Uint8ClampedArray(bytes), width, height);
ctx.putImageData(image, 0, 0);
```

GPU path: upload tile bytes as a 2D RGBA8 texture and decode i16/spectral fields
in the shader. WebGPU is preferable for a new application, but the packed
format also works in WebGL2 without integer-texture requirements.

## Why not render one giant texture?

A multi-hour source at a few hundred peaks/second produces millions of peaks.
A single texture either exceeds practical GPU dimensions or forces needless
uploads whenever only a small viewport is visible. The 4096-peak tiled layout
keeps uploads bounded and gives natural prefetch granularity for scrolling.
