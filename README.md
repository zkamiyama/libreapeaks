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

At extreme zoom the WebGL2 and PySide6 reference players automatically switch
from `.reapeaks` min/max records to a bounded source-PCM window, allowing them
to draw real sample points without retaining the full decoded file. See
[`docs/SOURCE_PCM_LOD.md`](docs/SOURCE_PCM_LOD.md).
The shared source helper also exposes structured range-decode events and a
draw plan for exact connected sample lines and optional point markers.

### Exact high-zoom memory model

Playback only guarantees that *some* short, sequential PCM neighborhood is
normally buffered near the playhead. It does not guarantee that an arbitrary
stopped/scrolled waveform viewport is still resident, source-rate, or pre-DSP.
The default source LOD therefore uses independent bounded random access; a DAW
that exposes its own source-rate block cache can share it through
`CallbackPcmWindowReader` and avoid double-caching.

The reference defaults are a 1 MiB target page, a 16 MiB hard limit for one
raw window, a 64 MiB byte LRU, at most 1024 retained entries, 64 distinct
pending keys, two concurrent reader calls, and 4096 display records. The LRU
limit is not a whole-process RAM claim: active decoder output, the small
display/HTTP buffer, GPU texture, decoder process, and OS page cache are
additional bounded/implementation memory. WebGL2 emits
`libreapeaks:pcm-range`; PySide6 emits `rangeAccess` and `rangeDecoded`; and
`plan_pcm_draw()` / `planPcmDraw()` expose line/point placement without
allocating a GUI-specific point array.

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

## REAPER-native generation modes

REAPER 7.79 does **not** generate arbitrary independent `-'s'`, `-'g'`, and
`-'r'` caches. A live fresh-process sweep of 71 `showpeaks` configurations,
including REAPER's native normal/spectral/spectrogram/LUFS actions, produced
exactly three cache shapes:

```text
waveform:
  waveform only

spectral:
  waveform + -'s' spectral + -'r' loudness

spectrogram:
  waveform + -'s' spectral + -'g' spectrogram + -'r' loudness
```

No `-'s'`-only, `-'g'`-only, or `-'r'`-only file was observed. For new callers,
libreapeaks therefore exposes these as a **mode**, rather than as independent
layer flags.

Rust:

```text
ReaperPeakMode::{Waveform, Spectral, Spectrogram}
generate_pcm16_reaper(...)
generate_f32_reaper(...)
```

Python:

```text
REAPER_PEAK_MODE_WAVEFORM      = "waveform"
REAPER_PEAK_MODE_SPECTRAL      = "spectral"
REAPER_PEAK_MODE_SPECTROGRAM   = "spectrogram"
generate_pcm16_reaper(..., mode)
generate_f32_reaper(..., mode)
```

C:

```text
RPK_REAPER_PEAK_MODE_WAVEFORM
RPK_REAPER_PEAK_MODE_SPECTRAL
RPK_REAPER_PEAK_MODE_SPECTROGRAM
rpk_generate_pcm16_reaper(...)
rpk_generate_f32_reaper(...)
```

PCM16 supports all three modes. Float32 currently supports waveform and spectral
modes; float32 spectrogram mode fails closed until exact float32 `-'g'`
generation is implemented.

## Spectrogram (`-'g'`) support

`-'g'` is parsed, serialized, and generated natively for PCM16 mode-3 caches.
The low-level Rust API exposes:

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
| `-'r'` loudness | Parse and generate through REAPER-native mode APIs |
| legacy `-'l'` loudness | Token recognized; payload layout not implemented |
| REAPER-style divisions | `default_divisions(sample_rate, peakcachegenrs)` in Rust/Python/C |
| GUI waveform pyramid | Native REAPER levels plus lazy ratio-4 display levels |
| C ABI | Native waveform / spectral (`s+r`) / PCM16 spectrogram (`s+g+r`) generation plus legacy APIs |
| Python | Native waveform / spectral (`s+r`) / PCM16 spectrogram (`s+g+r`) generation plus legacy APIs |

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

Preferred REAPER-shaped entry points:

```text
generate_pcm16_reaper(..., ReaperPeakMode::Waveform)
    waveform only

generate_pcm16_reaper(..., ReaperPeakMode::Spectral)
    waveform + -'s' spectral + -'r' loudness

generate_pcm16_reaper(..., ReaperPeakMode::Spectrogram)
    waveform + -'s' spectral + -'g' spectrogram + -'r' loudness
```

The lower-level/legacy entry points remain available:

```text
generate_pcm16 / generate_f32
    waveform + optional -'s' spectral layers

generate_pcm16_mode3 / generate_f32_mode3
    waveform + -'s' spectral + -'r' loudness

generate_pcm16_mode3_with_spectrogram
    waveform + -'s' spectral + -'g' spectrogram + -'r' loudness
```

## Python

The distribution name is `libreapeaks`; the import module is `reapeaks`.

```bash
maturin develop --release
python - <<'PY'
import reapeaks

divisions = reapeaks.default_divisions(48_000, 300)
print(divisions)

# pcm16le is interleaved little-endian PCM16 bytes.
cache = reapeaks.generate_pcm16_reaper(
    pcm16le,
    48_000,
    2,
    divisions,
    reapeaks.REAPER_PEAK_MODE_SPECTROGRAM,
)
PY
```

For 48 kHz / 300 peaks per second the divisions are:

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

The demos use fixed-size waveform/spectral tiles and frontend LRUs. See
[`examples/PLAYER_DEMOS.md`](examples/PLAYER_DEMOS.md).

## C / C++

Build the library and include `include/reapeaks.h`:

```bash
cargo build --release --features strict-wdl
```

The stable C ABI exposes parsing, zoom planning, tiled waveform/spectral
textures, CPU RGBA rendering, REAPER-style divisions, legacy writers, and the
three observed native generation modes. Returned `RpkBuffer` objects must be
released with `rpk_buffer_free`. See [`docs/C_ABI.md`](docs/C_ABI.md).

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
- [`docs/SOURCE_PCM_LOD.md`](docs/SOURCE_PCM_LOD.md) — exact-sample LOD,
  bounded decode/cache policy, debug events, and draw-plan API;
- [`docs/C_ABI.md`](docs/C_ABI.md) — C ABI overview.

## Third-party code

`strict-wdl` builds Cockos WDL FFT/resampler code from the `third_party/WDL`
submodule with `WDL_FFT_REALSIZE=8`. WDL retains its own permissive license
notices; see `THIRD_PARTY_NOTICES.md`.

## License

MIT for libreapeaks original code. Third-party components retain their own
licenses.
