# C ABI overview

The public declarations live in [`include/reapeaks.h`](../include/reapeaks.h).
The header is the source of truth for the ABI.

## Ownership model

Parsed files are represented by opaque `RpkHandle*` objects:

```c
RpkHandle *handle = NULL;
int32_t rc = rpk_open(path, &handle);
...
rpk_close(handle);
```

Byte-returning functions use:

```c
typedef struct RpkBuffer {
  uint8_t *data;
  size_t len;
  size_t capacity;
} RpkBuffer;
```

Every successful returned `RpkBuffer` must be released with
`rpk_buffer_free()`. The free function clears the structure and is safe to call
again on the cleared buffer.

The C boundary validates null pointers, channel/sample-rate geometry, size
multiplication overflow, division counts, and malformed paths. Invalid C inputs
return an error code instead of allowing a C++ exception to cross the ABI.

## File and waveform metadata

```text
rpk_open
rpk_close
rpk_wave_encoding
rpk_level_count
rpk_get_level_info
```

`rpk_wave_encoding()` returns one of:

```text
RPK_WAVE_ENCODING_UNKNOWN
RPK_WAVE_ENCODING_RPKN
RPK_WAVE_ENCODING_RPKL
```

The GUI waveform APIs require materialized RPKN/RPKL waveform layers. Although
the parser recognizes RPKM files, the current compact RPKM waveform payload is
not exposed as a `WavePyramid`.

## View planning and tiled waveform access

```text
rpk_plan_view
rpk_tile_peaks
rpk_tile_count
rpk_tile_texture_rgba8
rpk_level_texture_rgba8
```

`rpk_tile_texture_rgba8()` is the preferred long-media interface. The default
tile size is 4096 peaks. `rpk_level_texture_rgba8()` materializes a complete
level and is better suited to small/coarse overview levels.

Waveform RGBA8 packing is lossless:

```text
R,G = max i16 little-endian
B,A = min i16 little-endian
```

## Spectral data

```text
rpk_spectral_layer_count
rpk_spectral_tile_texture_rgba8
```

Each RGBA8 texel is the existing little-endian REAPER spectral u32 code, not a
pre-rendered color.

## CPU waveform rendering

```text
rpk_render_rgba8
rpk_render_rgba8_scaled
```

Colors are supplied as byte-ordered RGBA packed into a little-endian `uint32_t`.
The scaled form accepts an explicit vertical full-scale value.

## REAPER-style division calculation

```c
int32_t rpk_default_divisions(
    uint32_t sample_rate,
    uint32_t fine_peaks_per_second,
    uint32_t out_divisions[3]);
```

The second argument corresponds to REAPER's `peakcachegenrs` preference. It is
not fixed at 300.

Example:

```c
uint32_t divisions[3];
if (rpk_default_divisions(48000, 500, divisions) == 0) {
  /* divisions = {96, 2400, 48000} */
}
```

Zero sample/peak rates and a null output pointer are rejected.

## Generation

```text
rpk_generate_pcm16
rpk_generate_f32
```

`rpk_generate_pcm16()` writes RPKN from decoded interleaved PCM16.

`rpk_generate_f32(..., large_range=0)` writes RPKN from decoded float samples,
which is useful when integer media such as PCM24/PCM32/FLAC has been decoded to
float.

`rpk_generate_f32(..., large_range=1)` writes RPKL, the large-range waveform
encoding observed for float media and several compressed formats in the tested
REAPER 7.79 build.

The `spectral` argument controls whether mirrored `-'s'` spectral layers are
also generated.

### Important mode-3 limitation

The current C writer entry points call the legacy Rust generation surface:

```text
generate_pcm16
generate_f32
```

They generate waveform plus optional spectral layers. They do **not** currently
expose the Rust complete mode-3 functions:

```text
generate_pcm16_mode3
generate_f32_mode3
```

Therefore the C ABI does not currently generate `-'r'` loudness layers, even
though the Rust core can generate them and the Rust parser can read them.

When byte-exact spectral behavior matters, build the library with
`--features strict-wdl`. The pure Rust fallback spectral implementation is not
the backend covered by the current byte-exact REAPER 7.79 spectral claim.

## Build

```bash
git clone --recurse-submodules https://github.com/zkamiyama/libreapeaks.git
cd libreapeaks
cargo build --release --features strict-wdl
```

Link the resulting static/shared library as appropriate for the target platform
and include `include/reapeaks.h`.

See [`COMPATIBILITY.md`](COMPATIBILITY.md) for the validated REAPER scope and
[`GUI_WAVEFORM.md`](GUI_WAVEFORM.md) for the texture/tile data model.
