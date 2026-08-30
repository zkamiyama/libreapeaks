# libreapeaks

A Rust core for REAPER `.ReaPeaks` files with a stable C ABI, PyO3 bindings,
and GUI-oriented multiresolution waveform/spectral textures.

The primary goal is **cache sharing**: if REAPER and another playback/editing
application use the same media, they should be able to reuse the same peak
cache instead of building two waveform/spectral caches.

> Status: early compatibility work. The waveform writer is strongly validated
> against REAPER 7.79 Linux. Spectral generation is reverse-engineered and the
> `strict-wdl` backend is continuously compared against REAPER-generated golden
> fixtures.

## What is implemented

- RPKM/RPKN/RPKL header parsing.
- RPKN/RPKL max/min waveform parsing.
- spectral-peak (`-'s'`) parsing: frequency + density/tonality.
- RPKN generation from decoded PCM16.
- RPKN and RPKL generation from decoded float32.
- optional REAPER-oriented spectral generation.
- Cockos WDL FFT/resampler backend (`strict-wdl`) for compatibility work.
- stable C ABI (`include/reapeaks.h`).
- PyO3 module (`reapeaks`).
- multiresolution GUI waveform index with lazy derived levels.
- fixed 4096-peak tiles suitable for CPU/GPU LRU caches.
- lossless RGBA8 waveform data textures.
- lossless RGBA8 spectral-code textures.
- CPU RGBA8 waveform rendering for Qt/PySide6 or browser `ImageData`.

## REAPER 7.79 validation so far

The test oracle is REAPER 7.79 x86_64 Linux running headlessly via ReaScript
`PCM_Source_BuildPeaks`.

### Waveform

For RPKN PCM16, the quantizer has been exhaustively measured using one REAPER
fine bucket for every possible int16 value (`-32768..32767`). Together with the
larger probe corpus, **122,516 compared waveform buckets are byte-exact**.

The effective normalized RPKN mapping is asymmetric:

```text
x >= 0:  round_half_up(x * 32767)
x <  0: -round_half_up(-x * 32768)
```

A 50,000-value 24-bit WAV probe independently confirmed the same normalized
float rule for RPKN: **50,000 / 50,000 constant buckets matched**.

For RPKL, 43,857 float values plus high-range probes through +/-512 confirmed
the official transform with round-half-up. REAPER also initializes a bucket's
floating extrema as `max=-1.0`, `min=+1.0`, which matters for buckets wholly
above +1 or below -1.

### Spectral peaks

Binary inspection plus differential probes recover the main REAPER 7.79 path:

- internal analysis rate: about 22,050 Hz;
- 1024-point analysis FFT;
- float32 Hann/sample product promoted into a double FFT input;
- phase-vocoder-style dominant-frequency refinement using the previous f32
  complex spectrum;
- exact density expression based on the second moment around the refined bin;
- coarser spectral levels aggregated directly from the fine spectral level.

Before swapping the numerical core to Cockos WDL, the reconstructed math model
matched 34,040 / 34,127 fine frequency values (99.745%) and 31,848 / 34,127
density values (93.32%). Remaining errors are concentrated around numerical
boundaries, impulses, and very-low-energy cases; `strict-wdl` exists to remove
FFT/resampler implementation differences.

### Media-format probes

With the same 48 kHz stereo source signal, REAPER 7.79 produced identical
wave/spectral/loudness payloads for WAV16, WAV24, WAV32, FLAC16 and FLAC24.
A float WAV produced the same spectral/loudness payload but RPKL waveform
encoding. On this REAPER build, MP3, Vorbis and Opus also select RPKL.

This is why `generate_f32(..., large_range=...)` makes the output peak encoding
explicit rather than guessing from the Rust/Python sample type.

## Build

```bash
git clone --recurse-submodules https://github.com/zkamiyama/libreapeaks.git
cd libreapeaks

# Pure Rust fallback spectral math
cargo test

# REAPER compatibility backend using Cockos WDL
cargo test --features strict-wdl
```

### Python

The Python package enables `strict-wdl` by default through maturin:

```bash
python -m pip install maturin
maturin develop --release
python -c "import reapeaks; print(reapeaks.default_divisions(48000))"
```

### C / C++

Build a shared/static library and include `include/reapeaks.h`:

```bash
cargo build --release --features strict-wdl
```

The ABI exposes parsing, zoom planning, waveform/spectral tiles, CPU RGBA
rendering, and PCM16/f32 generation.

## GUI data model

REAPER's three native waveform mipmaps are excellent for storage but sparse for
smooth zoom transitions. `WavePyramid` therefore keeps native levels and adds a
geometric ratio-4 **metadata-only display pyramid**. Derived peaks are generated
only for the visible range/tile.

A default tile contains 4096 peaks. `WaveTileKey { level_index, tile_index }` is
stable and intended to be the key of a frontend LRU/GPU texture cache.

For a view:

1. `plan_view(start_frame, end_frame, pixel_width)` selects the nearest level.
2. `tiles_for_view(...)` returns the minimal tile set.
3. `tile_texture(...)` returns a lossless RGBA8 data texture.
4. cache the texture by `(level_index, tile_index)`.

The waveform texture layout is one row per channel:

```text
R,G = max i16 little-endian
B,A = min i16 little-endian
```

The spectral texture stores the existing REAPER 32-bit spectral code directly
as little-endian RGBA8.

See [`docs/GUI_WAVEFORM.md`](docs/GUI_WAVEFORM.md).

## Important compatibility note

REAPER's peak rate is a preference. The oracle configuration used here has
`peakcachegenrs=300`, giving divisions 147/2205/44100 at 44.1 kHz and
160/2400/48000 at 48 kHz. Cockos' public format document notes that current
v7.x defaults can be around 400 peaks/s depending on preferences. Do not assume
a fixed fine division: either mirror the user's REAPER configuration or reuse
an existing `.reapeaks` file's positive division factors.

## Documentation

- [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md)
- [`docs/GUI_WAVEFORM.md`](docs/GUI_WAVEFORM.md)
- [`docs/C_ABI.md`](docs/C_ABI.md)

## Third-party code

`strict-wdl` builds the Cockos WDL FFT/resampler from the `third_party/WDL` Git
submodule with `WDL_FFT_REALSIZE=8`. WDL retains its own permissive license
notices; see `THIRD_PARTY_NOTICES.md`.

## License

MIT for libreapeaks original code. Third-party components retain their own
licenses.
