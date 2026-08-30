# libreapeaks

A Rust core for REAPER `.ReaPeaks` files with a stable C ABI, PyO3 bindings,
and GUI-oriented multiresolution waveform/spectral textures.

The primary goal is **cache sharing**: if REAPER and another playback/editing
application use the same media, they should be able to reuse the same peak
cache instead of building two waveform/spectral caches.

> Status: waveform generation and `-'s'` spectral-peak generation are now
> byte-exact across the current REAPER 7.79 Linux validation corpora when
> `strict-wdl` is enabled. This is strong tested compatibility, not a claim that
> every REAPER version, CPU architecture, preference set, or possible input has
> been exhaustively proven.

## What is implemented

- RPKM/RPKN/RPKL header parsing.
- RPKN/RPKL max/min waveform parsing.
- spectral-peak (`-'s'`) parsing: frequency + density/tonality.
- RPKN generation from decoded PCM16.
- RPKN and RPKL generation from decoded float32.
- REAPER-oriented spectral generation.
- Cockos WDL FFT/resampler backend (`strict-wdl`) for byte-level compatibility.
- stable C ABI (`include/reapeaks.h`).
- PyO3 module (`reapeaks`).
- multiresolution GUI waveform index with lazy derived levels.
- fixed 4096-peak tiles suitable for CPU/GPU LRU caches.
- lossless RGBA8 waveform data textures.
- lossless RGBA8 spectral-code textures.
- CPU RGBA8 waveform rendering for Qt/PySide6 or browser `ImageData`.

## REAPER 7.79 validation

The oracle is REAPER 7.79 x86_64 Linux running headlessly via ReaScript
`PCM_Source_BuildPeaks`.

**Oracle rule:** every media file is processed by a fresh REAPER process.
Batching several source builds in one REAPER process was observed to leak
spectral state between sources, so it is deliberately excluded from golden
fixture production.

### Waveform

For RPKN PCM16, the quantizer was exhaustively measured using one REAPER fine
bucket for every possible int16 value (`-32768..32767`). Together with the
larger probe corpus, **122,516 / 122,516 waveform buckets are byte-exact**.

The effective normalized RPKN mapping is asymmetric:

```text
x >= 0:  round_half_up(x * 32767)
x <  0: -round_half_up(-x * 32768)
```

A 50,000-value PCM24 probe independently confirmed the same normalized rule:
**50,000 / 50,000 exact**.

For RPKL, 43,857 float values plus high-range probes through +/-512 confirmed
the official transform with round-half-up and REAPER's bucket initialization
`max=-1.0`, `min=+1.0`: **43,857 / 43,857 exact**.

### Spectral peaks

The recovered REAPER 7.79 path uses:

- a 22,050 Hz analysis domain for source rates above 22,050 Hz;
- a 1024-point WDL FFT with double FFT storage;
- float32 sample/window multiplication before promotion to double;
- phase-vocoder-style frequency refinement using the previous float32 complex
  spectrum;
- the recovered second-moment density expression;
- coarser spectral levels aggregated directly from the fine level;
- a strict WDL resampler feed buffer of 2048 interleaved samples
  (`max(1, 2048/channels)` frames), which is required for exact near-unity
  resampling behavior around 22,051 Hz;
- thread-safe WDL FFT initialization via `std::call_once`.

REAPER 7.79 also has an observable low-rate compatibility quirk: for
`source_rate <= 22050`, spectral layers are created but their payload codes are
zero. `strict-wdl` reproduces this; it is not treated as a general DSP rule.

Current byte-exact strict-WDL gates:

```text
Fresh-process primary fine corpus:
  188 cases
  10,112 / 10,112 u32 codes exact

Expanded fine corpus:
  169 cases
  6,188 / 6,188 u32 codes exact
  also exact after generate -> serialize -> parse

Independent fine total:
  357 cases
  16,300 / 16,300 u32 codes exact

Independent all-mipmap corpus:
  8 fresh-process media cases
  24 spectral levels
  96,222 / 96,222 u32 codes exact
```

The all-mipmap oracle covers 22,051 / 48k / 96k / 192k, mono / stereo /
4-channel, PCM16 RPKN and float32 RPKL.

Earlier, before substituting Cockos WDL and matching REAPER's resampler feed
behavior, the handwritten numerical model was only 99.745% exact for frequency
and 93.32% exact for density. Those residuals are no longer used as the strict
compatibility result.

See [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md) and
[`docs/validation-summary.json`](docs/validation-summary.json).

### Media-format probes

With the same 48 kHz stereo decoded signal, REAPER 7.79 produced identical
wave/spectral/loudness payloads for WAV16, WAV24, WAV32, FLAC16 and FLAC24.
A float WAV produced the same spectral/loudness payload but RPKL waveform
encoding. On this REAPER build, MP3, Vorbis and Opus also select RPKL.

This is why `generate_f32(..., large_range=...)` makes the output wave encoding
explicit rather than guessing from the Rust/Python sample type.

## Build

```bash
git clone --recurse-submodules https://github.com/zkamiyama/libreapeaks.git
cd libreapeaks

# Pure Rust fallback spectral math
cargo test

# Byte-exact REAPER-oriented backend using Cockos WDL
cargo test --release --features strict-wdl
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

REAPER's native waveform mipmaps are excellent for persistent storage but sparse
for smooth arbitrary zoom levels. `WavePyramid` keeps native levels and adds a
geometric ratio-4 **metadata-only display pyramid**. Derived peaks are computed
only for the visible range/tile, so the application does not create another
persistent waveform cache.

A default tile contains 4096 peaks. `WaveTileKey { level_index, tile_index }` is
stable and intended as the key of a frontend LRU/GPU texture cache.

For a view:

1. `plan_view(start_frame, end_frame, pixel_width)` selects the nearest level.
2. `tiles_for_view(...)` returns the minimal tile set.
3. `tile_texture(...)` returns a lossless RGBA8 data texture.
4. cache the texture by `(level_index, tile_index)`.

Waveform texture layout, one row per channel:

```text
R,G = max i16 little-endian
B,A = min i16 little-endian
```

Spectral textures store the existing REAPER 32-bit spectral code directly as
little-endian RGBA8. This is suitable for Qt6/PySide6 CPU/GPU upload and browser
WebGL/WebGPU/`ImageData` paths.

See [`docs/GUI_WAVEFORM.md`](docs/GUI_WAVEFORM.md).

## Reference audio-player demos

Two runnable reference players show the same tile model in desktop Qt and a
browser. Both display tile boundaries/IDs and frontend LRU statistics so it is
obvious when panning/zooming reuses cached 4096-peak tiles.

```bash
# Build the PyO3 module first.
maturin develop --release

# PySide6/QMediaPlayer desktop demo.
python -m pip install PySide6
python examples/pyside6_player.py /path/to/audio.wav

# JavaScript/<audio> browser demo backed by a thin libreapeaks tile server.
python examples/web_player/server.py /path/to/audio.wav
# open http://127.0.0.1:8765/
```

The demos exercise `open`, `levels`, `plan_view`, `tiles_for_view`,
`tile_texture`, `spectral_tile_texture`, `render_rgba`, `envelope_texture`,
`default_divisions`, and—when a PCM16/float32 WAV cache must be built—
`generate_pcm16` / `generate_f32`.

See [`examples/PLAYER_DEMOS.md`](examples/PLAYER_DEMOS.md) for architecture,
controls, cache-generation behavior, and the complete API-usage matrix.

## Important compatibility note

REAPER's peak rate is a preference. The main oracle configuration uses
`peakcachegenrs=300`, giving divisions 147/2205/44100 at 44.1 kHz and
160/2400/48000 at 48 kHz. At 22,051 Hz REAPER selected 73/1168/22192 in the
fresh-process oracle. Do not assume fixed divisions: mirror the user's REAPER
configuration or reuse an existing `.reapeaks` file's positive division factors.

## Documentation

- [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md)
- [`docs/validation-summary.json`](docs/validation-summary.json)
- [`docs/GUI_WAVEFORM.md`](docs/GUI_WAVEFORM.md)
- [`docs/C_ABI.md`](docs/C_ABI.md)
- [`examples/PLAYER_DEMOS.md`](examples/PLAYER_DEMOS.md)

## Third-party code

`strict-wdl` builds the Cockos WDL FFT/resampler from the `third_party/WDL` Git
submodule with `WDL_FFT_REALSIZE=8`. WDL retains its own permissive license
notices; see `THIRD_PARTY_NOTICES.md`.

## License

MIT for libreapeaks original code. Third-party components retain their own
licenses.
