# libreapeaks

**Use REAPER waveform-cache data outside REAPER.**

libreapeaks is a Rust library for reading and generating REAPER `.reapeaks`
files. It lets browsers, editors, review tools, asset managers, and playback
applications reuse REAPER's waveform/spectral cache instead of maintaining a
second analysis database.

A `.reapeaks` file is cache/analysis data, not source audio. libreapeaks never
rewrites the media file.

## What it can do

- parse RPKN and RPKL waveform layers;
- build zoomable waveform pyramids and fixed-size display/GPU tiles;
- parse and generate `-'s'` spectral-peak layers;
- parse, serialize, and generate `-'g'` spectrogram layers;
- parse and generate current `-'r'` loudness layers;
- generate the three cache shapes REAPER 7.79 was observed to emit;
- expose the same generation model from Rust, Python, and C;
- resolve/share REAPER's central cache path at the application layer;
- hand off to bounded source-PCM windows at sample-level zoom.

The core generation API works on decoded PCM. Reference players can use FFmpeg
as an external decoder.

## Compatibility status

The primary oracle is **REAPER 7.79 x86_64 Linux**. Compatibility claims refer
to that pinned executable and the explicitly named matrices. They are not a
claim about every REAPER release, operating system, CPU architecture, codec, or
preference combination.

The strongest current results are:

- PCM16 strict-WDL mode-3 whole-file gates are byte-identical on the permanent
  lossless/adversarial matrices;
- PCM16 spectrogram mode-3: **122 / 122 complete files byte-identical**;
- RPKN PCM24 waveform quantization: **50,000 / 50,000 constant buckets exact**
  against a deterministic 24-bit WAV oracle;
- finite float32/RPKL `-'g'`: **128 / 128 adversarial cases exact**, including
  every decoded 128-bin frame and every packed payload byte;
- RPKL waveform quantization: **exhaustively matched for every finite IEEE-754
  binary32 bit pattern** by recovering all **65,535 decision boundaries**;
- that scalar proof covers **4,278,190,080 finite f32 bit patterns**, including
  signed zero, all subnormals, and all **8,192 representable exact-half ties**;
- a dedicated finite-edge live oracle compares the **complete RPKL file**
  (waveform + `-'s'` + `-'g'` + `-'r'` + headers) and is byte-identical for all
  15 IEEE-754/RPKL edge cases.

The RPKL exact-half rule is sign-asymmetric. If `q` is the transformed positive
magnitude, REAPER behaves as:

```text
positive:  floor(q + 0.5)
negative: -ceil(q - 0.5)
```

So an implementation that rounds `abs(x)` half-up and only then applies the sign
is wrong at representable negative `.5` ties.

The exhaustive statement above is for the **scalar RPKL waveform quantizer**.
The stateful `-'s'`, `-'g'`, and `-'r'` transforms are locked by fresh-process
byte-exact corpus gates; this repository does not claim a formal proof over every
possible arbitrary-length finite-f32 sequence.

Exact source **NaN / +Inf / -Inf** policy is still outside the REAPER-identity
claim. Finite subnormals are no longer an unproven area.

See:

- [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) — compatibility contract;
- [`docs/F32_FINITE_PROOF.md`](docs/F32_FINITE_PROOF.md) — exhaustive finite-f32
  RPKL proof and whole-file finite evidence;
- [`docs/validation-summary.json`](docs/validation-summary.json) —
  machine-readable totals.

## REAPER-native generation modes

A fresh-process sweep of 71 REAPER 7.79 `showpeaks` configurations found three
native cache shapes:

```text
waveform:
  waveform only

spectral:
  waveform + -'s' spectral + -'r' loudness

spectrogram:
  waveform + -'s' spectral + -'g' spectrogram + -'r' loudness
```

No `-'s'`-only, `-'g'`-only, or `-'r'`-only native file was observed. New code
should therefore use a mode rather than treating those layers as arbitrary
independent flags.

Rust:

```text
ReaperPeakMode::{Waveform, Spectral, Spectrogram}
generate_pcm16_reaper(...)
generate_pcm24_reaper(...)
generate_pcm24_i32_reaper(...)
generate_f32_reaper(...)
```

Python:

```text
REAPER_PEAK_MODE_WAVEFORM
REAPER_PEAK_MODE_SPECTRAL
REAPER_PEAK_MODE_SPECTROGRAM
generate_pcm16_reaper(..., mode)
generate_pcm24_reaper(..., mode)
generate_f32_reaper(..., mode)
```

C:

```text
RPK_REAPER_PEAK_MODE_WAVEFORM
RPK_REAPER_PEAK_MODE_SPECTRAL
RPK_REAPER_PEAK_MODE_SPECTROGRAM
rpk_generate_pcm16_reaper(...)
rpk_generate_pcm24_reaper(...)
rpk_generate_pcm24_i32_reaper(...)
rpk_generate_f32_reaper(...)
```

PCM16 and float32 support all three modes. `large_range=true` selects RPKL for
float32 generation.

Packed signed PCM24LE and signed PCM24 values stored right-justified/sign-extended
in `i32` also support all three modes as RPKN generation paths. Each 24-bit sample
is normalized on demand to the exact float32 value `sample / 2^23`; the Rust and
C paths therefore do not materialize a second whole-file float32 PCM buffer.
Left-aligned S24-in-S32 data must be shifted right by eight bits before using the
`i32` entry point.

The direct PCM24 paths are regression-tested byte-for-byte against the existing
float32/RPKN generator for waveform, spectral, and spectrogram native modes. The
separate live-REAPER PCM24 claim remains the 50,000 / 50,000 RPKN waveform
quantizer oracle above; do not infer an additional PCM24 source-container oracle
for every stateful special layer from the adapter regression alone.

Lower-level writers remain available for applications that intentionally need a
non-native layer shape:

```text
generate_pcm16 / generate_f32
generate_pcm16_mode3 / generate_f32_mode3
generate_pcm16_mode3_with_spectrogram
generate_f32_mode3_with_spectrogram
```

## Support table

| Area | Current support |
|---|---|
| RPKN waveform | Parse, generate, tile, render; PCM16, PCM24 and float32 generation inputs |
| RPKL waveform | Parse, generate, tile, render; finite-f32 quantizer exhaustive against REAPER 7.79 |
| RPKM | Header/layout recognition; compact waveform payload not materialized through `WavePyramid` |
| `-'s'` spectral peaks | Parse, generate, tile; strict-WDL live-oracle validated |
| `-'g'` spectrogram | Parse, serialize, PCM16 + PCM24 + float32 generate; permanent live byte-exact gates cover the documented PCM16/float32 matrices |
| `-'r'` loudness | Parse and generate through REAPER-native modes |
| legacy `-'l'` loudness | Token recognized; payload layout not implemented |
| REAPER-style divisions | `default_divisions(sample_rate, peakcachegenrs)` in Rust/Python/C |
| C ABI | Parse/render/native generation plus PCM16/PCM24/f32 writers |
| Python | Parse/render/native generation plus packed PCM24 and existing PCM16/f32 writers |

RPKM materialization and legacy `-'l'` are functional gaps. Exact source
NaN/Inf behavior is a compatibility-proof gap. These are intentionally listed
separately in `docs/COMPATIBILITY.md`.

## `-'g'` spectrogram format

A logical channel/time frame contains 128 unsigned 12-bit codes:

```text
128 bins * 12 bits = 192 bytes = 48 u32 words
```

The `-'g'` layer token is `-103`. Its `peak_count` counts **u32 words per
channel**, not logical time frames. Payload ordering is time-major,
channel-inner.

Public Rust codec items include:

```text
SpectrogramFrame
SPECTROGRAM_BINS
SPECTROGRAM_BYTES_PER_CHANNEL_FRAME
SPECTROGRAM_WORDS_PER_CHANNEL_FRAME
decode_spectrogram_frame(...)
encode_spectrogram_frame(...)
```

See `docs/REVERSE_ENGINEERING.md` for FFT/window/placement/quantizer and mipmap
details.

## Build and test

Clone with the Cockos WDL submodule when using the byte-exact compatibility
backend:

```bash
git clone --recurse-submodules https://github.com/zkamiyama/libreapeaks.git
cd libreapeaks
```

Normal implementation:

```bash
cargo test
```

Strict compatibility implementation:

```bash
cargo test --release --features strict-wdl
```

`strict-wdl` builds Cockos WDL FFT/resampler code from `third_party/WDL` with
`WDL_FFT_REALSIZE=8`.

On Windows with MSVC, define `NOMINMAX` when building `strict-wdl` to avoid
Windows SDK `min`/`max` macro collisions:

```powershell
$env:CXXFLAGS="/DNOMINMAX"
cargo test --release --features strict-wdl
```

## REAPER extension reference implementation

[`examples/reaper_rpkx_extension/`](examples/reaper_rpkx_extension/) contains an
experimental REAPER 7.79 extension showing how an application can compose this
library into a transparent RPKX-preserving host integration. **It is example
code, not part of the libreapeaks public library API.**

The example includes its C++ `PCM_source` wrapper, Rust preserving store/bridge,
crash-safety logic, raw PCM16 fast path, real-REAPER acceptance harness, and
native-vs-reference benchmarks in one directory. Start with its
[`README.md`](examples/reaper_rpkx_extension/README.md), then see
[`DESIGN.md`](examples/reaper_rpkx_extension/DESIGN.md) and
[`TESTING.md`](examples/reaper_rpkx_extension/TESTING.md).

## Python

The distribution name is `libreapeaks`; the import module is `reapeaks`.

```bash
python -m pip install -U maturin
maturin develop --release
```

Example:

```python
import reapeaks

divisions = reapeaks.default_divisions(48_000, 300)
cache = reapeaks.generate_pcm16_reaper(
    pcm16le,
    48_000,
    2,
    divisions,
    reapeaks.REAPER_PEAK_MODE_SPECTROGRAM,
)
```

Packed PCM24LE can be passed without expanding the whole source to float32:

```python
cache = reapeaks.generate_pcm24_reaper(
    pcm24le,
    48_000,
    2,
    divisions,
    reapeaks.REAPER_PEAK_MODE_SPECTROGRAM,
)
```

For 48 kHz / 300 peaks per second:

```text
[160, 2400, 48000]
```

`300` is a REAPER preference value, not a file-format constant. Applications
following an existing REAPER setup should use its `peakcachegenrs` or reuse the
positive divisions already present in a cache.

## C / C++

Build the library and include `include/reapeaks.h`:

```bash
cargo build --release --features strict-wdl
```

Returned `RpkBuffer` objects must be released with `rpk_buffer_free`. See
[`docs/C_ABI.md`](docs/C_ABI.md).

## Reference players and exact sample zoom

Desktop and WebGL2 examples use fixed-size waveform/spectral tiles and bounded
front-end LRUs. At extreme zoom they switch from `.reapeaks` extrema to a
bounded source-PCM window so they can draw true sample points without retaining
the entire decoded source.

```bash
python -m pip install PySide6
python examples/pyside6_player.py /path/to/audio.wav

python examples/pyside6_daw_player.py /path/to/audio.wav \
  --generation-mode spectrogram --wave-encoding rpkl

python examples/web_player/server.py /path/to/audio.wav
```

See:

- [`examples/PLAYER_DEMOS.md`](examples/PLAYER_DEMOS.md);
- [`docs/GUI_WAVEFORM.md`](docs/GUI_WAVEFORM.md);
- [`docs/SOURCE_PCM_LOD.md`](docs/SOURCE_PCM_LOD.md).

## Sharing REAPER's cache path

Generating correct bytes is only half of interoperability. REAPER also chooses
the filename and directory according to its configuration. Do not reverse-guess
a central-cache filename from `altpeakspath`.

The canonical application-layer path oracle is REAPER's `GetPeakFileNameEx`.
See [`docs/REAPER_CENTRAL_CACHE.md`](docs/REAPER_CENTRAL_CACHE.md).

## Documentation map

Start at [`docs/README.md`](docs/README.md).

- [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) — tested compatibility
  contract and known gaps;
- [`docs/F32_FINITE_PROOF.md`](docs/F32_FINITE_PROOF.md) — exhaustive finite-f32
  RPKL quantizer proof and finite whole-file evidence;
- [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md) — recovered
  algorithms and oracle methodology;
- [`docs/validation-summary.json`](docs/validation-summary.json) — validation
  totals in machine-readable form;
- [`docs/REAPER_CENTRAL_CACHE.md`](docs/REAPER_CENTRAL_CACHE.md) — cache-path
  policy;
- [`docs/GUI_WAVEFORM.md`](docs/GUI_WAVEFORM.md) — waveform/spectral GUI model;
- [`docs/SOURCE_PCM_LOD.md`](docs/SOURCE_PCM_LOD.md) — exact-sample LOD and
  bounded PCM access;
- [`docs/C_ABI.md`](docs/C_ABI.md) — C ABI overview;
- [`examples/reaper_rpkx_extension/README.md`](examples/reaper_rpkx_extension/README.md)
  — REAPER RPKX-preserving reference integration (example, not library API).

## Third-party code and license

WDL retains its own permissive license notices; see `THIRD_PARTY_NOTICES.md`.
libreapeaks original code is MIT licensed. Third-party components retain their
own licenses.
