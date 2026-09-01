# libreapeaks

**Use REAPER waveform-cache data outside REAPER.**

libreapeaks is a Rust library for reading and generating REAPER `.reapeaks`
files. A `.reapeaks` file is analysis/cache data used to draw waveforms and
related views quickly; it is not the source audio, and libreapeaks never rewrites
your media.

## Why an artist or DAW developer might care

A sample browser, editor, review tool, asset manager, or playback application can
use libreapeaks to:

- open an existing REAPER cache instead of re-analyzing audio;
- draw deeply zoomable waveforms from REAPER's multiresolution data;
- read `-'s'` spectral peaks and `-'g'` spectrogram bins;
- generate REAPER-compatible cache layers from decoded PCM;
- share the cache path selected by REAPER instead of maintaining a second
  waveform database.

The core works on decoded PCM. The example players can use FFmpeg as an external
decoder.

## Compatibility status

The primary oracle is **REAPER 7.79 x86_64 Linux**. Compatibility claims in this
repository refer to that pinned build and the explicitly tested matrices; they
are not a claim about every REAPER release, operating system, architecture,
codec, or preference combination.

For the continuously validated PCM16 mode-3 paths, `strict-wdl` reproduces
REAPER output byte-for-byte. The permanent gates include:

- 8/8 lossless ALAC/M4A mode-3 files, complete-file byte identical;
- 16/16 adversarial rate/channel/EOF mode-3 files, complete-file byte identical;
- 122/122 adversarial **spectrogram mode-3** cases, complete-file byte identical
  with `generate_pcm16_mode3_with_spectrogram` + `strict-wdl`;
- the same 122-case spectrogram matrix checked against the portable/default FFT
  implementation for exact `-'g'` output;
- a 188-case fresh-process `-'s'` spectral corpus with 10,112/10,112 oracle
  codes exact, plus larger fine/mipmap aggregation corpora;
- ASan/UBSan, ThreadSanitizer, strict-WDL boundary checks, parser corruption
  tests, packing exhaustiveness, scheduler boundaries, and deterministic
  parallel-generation stress.

The 122-case spectrogram stress matrix spans 8 kHz through 192 kHz, multiple
`peakcachegenrs` values including 100/150/300/500/1000 and branch-boundary
values, 1-8 channels, exact scheduler edges, long inputs, silence/DC/Nyquist,
LSB-level and full-scale signals, exact-bin/off-bin tones, chirps, impulses,
noise, and deterministic randomized cases.

See [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) for the exact contract and
[`docs/validation-summary.json`](docs/validation-summary.json) for the
machine-readable validation summary.

## Spectrogram (`-'g'`) support

`-'g'` is now parsed, serialized, and generated natively for PCM16 mode-3
caches. The Rust API exposes:

```text
generate_pcm16_mode3_with_spectrogram(...)
```

This entry point is intentionally separate from `generate_pcm16_mode3`, so
adding spectrogram layers cannot silently change the established byte-exact
legacy mode-3 path. With `GenerateOptions.spectral = true`, the generated REAPER
7.79 layer order is:

```text
waveform layers
mirrored -'s' spectral layers
mirrored -'g' spectrogram layers
-'r' loudness layers
```

The public spectrogram codec surface also includes:

```text
SpectrogramFrame
SPECTROGRAM_BINS                         // 128
SPECTROGRAM_BYTES_PER_CHANNEL_FRAME      // 192
SPECTROGRAM_WORDS_PER_CHANNEL_FRAME      // 48
decode_spectrogram_frame(...)
encode_spectrogram_frame(...)
```

Each logical channel/time frame contains 128 unsigned 12-bit codes. The on-disk
`-'g'` header count is a count of 32-bit words **per channel**, not a logical
frame count.

## What the library can read and generate

| Area | Current support |
|---|---|
| RPKN / RPKL waveform layers | Parse, generate, tile, render |
| RPKM | Header/layout recognition; compact waveform payload is not exposed through the current waveform pyramid |
| `-'s'` spectral peaks | Parse, generate, tile |
| `-'g'` spectrogram | Parse, serialize, PCM16 mode-3 generate |
| `-'r'` loudness | Parse; generate through Rust mode-3 APIs |
| legacy `-'l'` loudness | Token recognized; payload layout not implemented |
| REAPER-style divisions | `default_divisions(sample_rate, peakcachegenrs)` in Rust/Python/C |
| GUI waveform pyramid | Native REAPER levels plus lazy ratio-4 display levels |
| C ABI | Parsing, view planning, waveform/spectral textures/rendering and waveform/`-'s'` generation; no complete mode-3/`-'g'` writer entry point yet |
| Python | Parsing/GUI APIs and waveform/`-'s'` generation; no complete mode-3/`-'g'` writer entry point yet |

## Rust generation entry points

Clone with the Cockos WDL submodule when using `strict-wdl`:

```bash
git clone --recurse-submodules https://github.com/zkamiyama/libreapeaks.git
cd libreapeaks
```

Run the normal implementation:

```bash
cargo test
```

Run the WDL-backed compatibility implementation used by the strict byte-exact
gates:

```bash
cargo test --release --features strict-wdl
```

Generation entry points:

```text
generate_pcm16 / generate_f32
    waveform + optional -'s' spectral layers

generate_pcm16_mode3 / generate_f32_mode3
    waveform + -'s' spectral + -'r' loudness

generate_pcm16_mode3_with_spectrogram
    waveform + -'s' spectral + -'g' spectrogram + -'r' loudness
```

`GenerateOptions.spectral` controls the existing `-'s'` spectral path and must
be `true` for the mode-3 entry points. Spectrogram generation is selected by the
separate `generate_pcm16_mode3_with_spectrogram` function.

## Python

The distribution name is `libreapeaks`; the import module is `reapeaks`.

```bash
maturin develop --release
python - <<'PY'
import reapeaks
print(reapeaks.default_divisions(48_000, 300))
PY
```

For 48 kHz / 300 peaks per second the result is:

```text
[160, 2400, 48000]
```

`300` is a preference, not a file-format constant. Applications following a
user's REAPER setup should use that setup's `peakcachegenrs` or reuse the
positive divisions in an existing cache.

## Reference players

Build the Python extension first. The Python package enables `strict-wdl` by
default.

```bash
python -m pip install -U maturin
maturin develop --release
```

Desktop Qt player:

```bash
python -m pip install PySide6
python examples/pyside6_player.py /path/to/audio.wav
```

Browser player:

```bash
python examples/web_player/server.py /path/to/audio.wav
# open http://127.0.0.1:8765/
```

For compressed media, the desktop example can explicitly use FFmpeg for cache
generation:

```bash
python examples/pyside6_player.py song.flac --cache-decoder ffmpeg
```

The demos use fixed-size waveform/spectral tiles and frontend LRUs. Their public
Python writer path still exposes waveform plus optional `-'s'` spectral layers;
the complete mode-3 loudness/`-'g'` writer is currently a Rust API. See
[`examples/PLAYER_DEMOS.md`](examples/PLAYER_DEMOS.md).

## C / C++

Build the library and include `include/reapeaks.h`:

```bash
cargo build --release --features strict-wdl
```

The stable C ABI exposes parsing, zoom planning, tiled waveform/spectral
textures, CPU RGBA rendering, REAPER-style divisions, and PCM16/f32
waveform/`-'s'` generation. Returned `RpkBuffer` objects must be released with
`rpk_buffer_free`. See [`docs/C_ABI.md`](docs/C_ABI.md).

## Sharing REAPER's cache path

Generating correct bytes is only half of interoperability. REAPER also chooses
the filename and directory according to its configuration. Do not reverse-guess
a central-cache filename from `altpeakspath`.

The canonical path oracle is REAPER's `GetPeakFileNameEx` API. libreapeaks ships
application-layer helpers that can read peak-related `reaper.ini` values, ask a
short-lived REAPER process for the exact read/write path, and persist those
answers in a cache map. See
[`docs/REAPER_CENTRAL_CACHE.md`](docs/REAPER_CENTRAL_CACHE.md).

## GUI model

`WavePyramid` keeps REAPER's native waveform levels and adds metadata-only
ratio-4 display levels. Derived extrema are aggregated only for the visible
range or requested tile. Waveform tiles pack max/min codes losslessly into
RGBA8; existing `-'s'` spectral codes can likewise be packed into RGBA8 for
GPU-friendly display. See [`docs/GUI_WAVEFORM.md`](docs/GUI_WAVEFORM.md).

## Technical background

Byte identity depends on details that are invisible at the UI level: REAPER
waveform quantization, WDL FFT/resampling behavior, `-'s'` phase refinement,
`-'g'` Blackman-Harris window placement and 12-bit quantization, coarse-layer
aggregation, libebur128-style loudness filtering, exact floating-point update
order, and EOF/mipmap scheduling.

The documentation starts at [`docs/README.md`](docs/README.md):

- [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) — proven compatibility scope;
- [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md) — recovered
  algorithms and oracle methodology, including `-'g'`;
- [`docs/validation-summary.json`](docs/validation-summary.json) — validation
  totals in machine-readable form;
- [`docs/REAPER_CENTRAL_CACHE.md`](docs/REAPER_CENTRAL_CACHE.md) — cache path
  policy;
- [`docs/GUI_WAVEFORM.md`](docs/GUI_WAVEFORM.md) — GUI data model;
- [`docs/C_ABI.md`](docs/C_ABI.md) — C ABI overview.

## Third-party code

`strict-wdl` builds Cockos WDL FFT/resampler code from the `third_party/WDL`
submodule with `WDL_FFT_REALSIZE=8`. WDL retains its own permissive license
notices; see `THIRD_PARTY_NOTICES.md`.

## License

MIT for libreapeaks original code. Third-party components retain their own
licenses.
