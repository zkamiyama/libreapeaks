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

The legacy generation entry points remain available:

```text
rpk_generate_pcm16
rpk_generate_f32
```

They write waveform plus an optional `-'s'` spectral layer set. They are kept
for compatibility with existing callers, but that optional-`s` shape is not one
of the three complete cache shapes observed from REAPER 7.79's native peak
modes.

For new REAPER-oriented code, use:

```text
rpk_generate_pcm16_reaper
rpk_generate_f32_reaper
```

and one of the stable mode constants:

```text
RPK_REAPER_PEAK_MODE_WAVEFORM
RPK_REAPER_PEAK_MODE_SPECTRAL
RPK_REAPER_PEAK_MODE_SPECTROGRAM
```

A fresh-process sweep of 71 `showpeaks` configurations in pinned REAPER 7.79
x86_64 Linux, including the native actions `Peaks: Show normal peaks`, `Peaks:
Toggle spectral peaks`, `Peaks: Toggle spectrogram`, and the LUFS display
actions, produced only these three on-disk layer shapes:

```text
WAVEFORM:
  waveform

SPECTRAL:
  waveform + -'s' spectral + -'r' loudness

SPECTROGRAM:
  waveform + -'s' spectral + -'g' spectrogram + -'r' loudness
```

No `-'s'`-only, `-'g'`-only, or `-'r'`-only file was observed, so the native
mode API intentionally exposes a mode enum instead of independent layer bits.

Both `rpk_generate_pcm16_reaper()` and `rpk_generate_f32_reaper()` support all
three modes. The float32 entry point selects RPKN/RPKL via `large_range`; with
`large_range=1`, SPECTROGRAM writes RPKL plus `-'s'`, `-'g'`, and `-'r'`.

The float `-'g'` path is direct rather than a PCM16 approximation. The permanent
REAPER 7.79 Linux x86_64 live oracle covers 128 adversarial IEEE float32/RPKL
sources and reports **128 / 128 exact** for both decoded 128-bin `-'g'` frames
and packed `-'g'` payload bytes. This is a `-'g'` compatibility claim; exact
NaN/Inf/subnormal behavior and unrelated whole-file RPKL waveform rounding
remain separate.

When byte-exact spectral/spectrogram behavior matters, build with
`--features strict-wdl`. PCM16 complete-file gates and the float32/RPKL `-'g'`
gate are described in [`COMPATIBILITY.md`](COMPATIBILITY.md).

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
