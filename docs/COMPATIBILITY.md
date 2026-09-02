# Compatibility and validation scope

This document is the concise compatibility contract for libreapeaks: **what is
reproduced, against which REAPER build, and how strongly has it been tested?**

The primary oracle is **REAPER 7.79 x86_64 Linux**. A passing live oracle means
libreapeaks output was compared with output from that pinned executable. It does
not imply identical behavior for every REAPER release, platform, CPU, codec, or
preference combination.

## Current compatibility statement

For the validated PCM16 corpora, the Rust `strict-wdl` mode-3 generators
reproduce REAPER 7.79 `.reapeaks` files **byte-for-byte**, including:

- RPKN waveform layers;
- mirrored `-'s'` spectral-peak layers;
- mirrored `-'g'` spectrogram layers when using
  `generate_pcm16_mode3_with_spectrogram`;
- `-'r'` momentary/short-term loudness layers;
- layer headers/counts and supplied source metadata;
- the tested REAPER 7.79 EOF, scheduler, window-placement, and coarse-mipmap
  behavior.

For the validated IEEE float32/RPKL spectrogram corpus, the Rust `strict-wdl`
float generator reproduces REAPER 7.79 `-'g'` layers **byte-for-byte** in
**128 / 128** adversarial cases. The live gate compares both every decoded
128-bin `SpectrogramFrame` and the complete packed `-'g'` payload bytes.

This float32 claim is intentionally scoped to `-'g'`. It is not a whole-file
claim for every possible RPKL waveform rounding edge, nor a claim about REAPER's
exact NaN/Inf/subnormal policy. Those are separate compatibility surfaces.

`generate_pcm16_mode3_with_spectrogram` is intentionally separate from
`generate_pcm16_mode3`. The established non-`g` mode-3 byte stream therefore
remains unchanged unless the caller explicitly chooses spectrogram generation.

## Native REAPER cache modes

A dedicated live oracle enumerates REAPER 7.79's native `Peaks:` actions and
then rebuilds the same deterministic PCM16 source in **71 fresh REAPER
processes** with the resulting and neighboring `showpeaks` configurations.
Only three distinct special-layer sets were observed:

```text
waveform mode:     waveform only
spectral mode:     waveform + -'s' spectral + -'r' loudness
spectrogram mode:  waveform + -'s' spectral + -'g' spectrogram + -'r' loudness
```

Representative native actions produced:

```text
Peaks: Show normal peaks       -> www
Peaks: Toggle spectral peaks   -> wwwsssrr
Peaks: Toggle spectrogram      -> wwwsssggrr
```

The LUFS display actions also produced the `wwwsssrr` cache shape. Across the
71-case sweep there was **no** `-'s'`-only, `-'g'`-only, or `-'r'`-only cache.
For REAPER-oriented callers, libreapeaks therefore exposes the observed shapes
as `ReaperPeakMode::{Waveform, Spectral, Spectrogram}` instead of arbitrary
independent layer flags.

Public one-call mode APIs are:

```text
Rust:   generate_pcm16_reaper / generate_f32_reaper
Python: generate_pcm16_reaper / generate_f32_reaper
C:      rpk_generate_pcm16_reaper / rpk_generate_f32_reaper
```

PCM16 and float32 both support all three modes. With `large_range=true`, the
float32 path writes RPKL. Its `-'g'` payload is covered by the 128-case
byte-exact live oracle described below.

The older `generate_pcm16` / `generate_f32` and matching Python/C calls remain
available for compatibility. Their optional `spectral` switch means waveform +
optional `-'s'` only and should be treated as a lower-level/legacy writer shape,
not as the canonical REAPER native-mode selector.

## Whole-file live REAPER gates

### FFmpeg-backed ALAC / M4A matrix

`reaper-ffmpeg-byte-identical` creates deterministic 48 kHz stereo PCM16,
encodes it losslessly to ALAC/M4A, verifies an external FFmpeg decode returns the
original PCM16, asks REAPER 7.79 to build a fresh mode-3 cache, and compares the
complete file with libreapeaks strict-WDL output.

Result:

```text
8 / 8 complete files byte-identical
```

Signals: silence, 997 Hz sine, step, impulse, DC tail, low-level signal,
block-edge impulse, and full-scale alternating samples.

### Adversarial mode-3 matrix

`reaper-adversarial-oracle` runs 16 independent whole-file cases through fresh
REAPER processes.

Result:

```text
16 / 16 complete files byte-identical
```

Coverage includes 22.05/32/44.1/48/88.2/96 kHz, `peakcachegenrs`
150/300/500, mono/stereo/6-channel audio, EOF ±1-sample boundaries, 400 ms
window ±1-sample boundaries, steps, impulses, and deterministic multichannel
noise.

### PCM16 spectrogram mode-3 stress matrix

`reaper-spectrogram-stress-oracle` pins the same REAPER 7.79 Linux x86_64 build,
uses one fresh REAPER process per source, and currently generates **122**
adversarial PCM16 WAVE cases.

For every case, the strict-WDL test compares:

1. the complete RPKN header;
2. the complete layer-header table;
3. every non-`g` layer payload;
4. decoded `-'g'` bins;
5. packed `-'g'` payload bytes;
6. finally the **entire file**.

Result:

```text
strict-wdl spectrogram mode-3: 122 / 122 complete files byte-identical
portable/default FFT:          122 / 122 exact -'g' comparisons
```

The stress corpus spans:

- sample rates from 8,000 through 192,000 Hz;
- standard and awkward rates around the recovered 256-sample placement branch
  (76,799 / 76,800 / 76,801 Hz);
- `peakcachegenrs` 100/150/300/500/1000 plus many neighboring values that force
  fine divisions around 255/256/257;
- 1 through 8 channels, including sparse and independent-lane inputs;
- exact scheduler boundaries at ±1 sample;
- long inputs that produce many coarse frames;
- silence, positive/negative full-scale DC, Nyquist alternation, 1-LSB
  alternation, steps, ramps, chirps, exact-bin/off-bin tones, impulses, noise,
  and deterministic randomized cases.

The portable/default FFT gate is a `-'g'` compatibility check rather than a
claim that every auxiliary floating-point path is identical to strict-WDL.

### Float32/RPKL `-'g'` stress matrix

`reaper-spectrogram-f32-byte-identical` uses the same pinned executable and a
fresh REAPER process for every IEEE float32 WAVE source. REAPER writes RPKL
caches, and the permanent strict-WDL test compares the float-generated `-'g'`
layers independently of unrelated waveform/loudness compatibility surfaces.

For every case it checks:

1. `-'g'` layer count and mirrored divisions;
2. every decoded 128-bin channel/time frame;
3. every `-'g'` header word count;
4. every packed `-'g'` payload byte.

Result:

```text
strict-wdl float32/RPKL -'g': 128 / 128 cases exact
packed-payload failures:       0
```

The 128-case corpus spans 8,000 through 192,000 Hz, 1 through 8 channels,
`peakcachegenrs` 100/150/300/500/1000 plus many neighboring values, the
76,799/76,800/76,801 placement branch, fine divisions around 255/256/257,
scheduler boundaries, long multichannel inputs, silence/DC, values above
±1.0, very small finite values, exact-bin and off-bin tones, chirps, ramps,
steps, impulses, deterministic noise, sparse lanes, and deterministic randomized
cases.

The corpus deliberately uses finite float audio values for the compatibility
claim. NaN/Inf exact REAPER behavior remains outside scope; the libreapeaks
safety policy for such values is described below.

## `-'g'` format and API coverage

The Rust parser materializes `-'g'` layers as `SpectrogramFrame` records and the
codec exposes exact frame encode/decode helpers.

Recovered REAPER 7.79 layout:

```text
token:                           -103
bins per channel/time frame:      128
bits per bin:                     12
bytes per channel/time frame:     192
u32 words per channel/time frame: 48
payload ordering:                 time-major, channel-inner
```

The `peak_count` in a `-'g'` layer header counts **u32 words per channel**; it is
not the number of logical time frames.

Two 12-bit codes are packed into three bytes as:

```text
[code1 >> 4,
 ((code1 & 0x0f) << 4) | (code2 & 0x0f),
 code2 >> 4]
```

Public Rust items include:

```text
SpectrogramFrame
decode_spectrogram_frame
encode_spectrogram_frame
SPECTROGRAM_BINS
SPECTROGRAM_BYTES_PER_CHANNEL_FRAME
SPECTROGRAM_WORDS_PER_CHANNEL_FRAME
generate_pcm16_mode3_with_spectrogram
generate_f32_mode3_with_spectrogram
generate_pcm16_reaper(..., ReaperPeakMode::Spectrogram)
generate_f32_reaper(..., ReaperPeakMode::Spectrogram)
```

Exact `-'g'` generation is live-oracle validated for PCM16/RPKN and for the
128-case finite float32/RPKL matrix. Python and C expose the same REAPER-mode
spectrogram paths.

## Spectrogram implementation stress beyond the live oracle

Normal and strict-WDL CI exercise the spectrogram implementation from several
independent angles:

- exhaustive round-trip coverage of all 12-bit code values and packed-pair edge
  combinations;
- arbitrary 192-byte frame decode/encode bijection;
- rejection of every nearby wrong frame length;
- 255-channel acceptance and 256-channel rejection;
- custom fine divisions 254/255/256/257/258;
- empty and tiny inputs across scheduler boundaries;
- deterministic header corruption, every truncated prefix of a valid cache, and
  thousands of deterministic parser bit flips without panic;
- channel permutation, sign inversion, mono-lane independence, source-metadata
  independence, and completed-prefix/zero-tail invariants;
- repeated parallel generation and mixed-configuration first-use races;
- many nested mipmap levels and randomized scheduler matrices;
- monotonic exact-bin response over PCM amplitude;
- verification that enabling `-'g'` does not change pre-existing mode-3 layers.

ASan/UBSan, ThreadSanitizer, strict-WDL boundary checks, and the ordinary Rust
feature matrix are also permanent gates.

## `-'s'` spectral validation

`-'s'` is a different REAPER layer from the new `-'g'` spectrogram. Its
strict-WDL implementation remains validated by the established corpora:

```text
fresh-process primary corpus:
  188 cases
  10,112 / 10,112 codes exact

expanded fine corpus:
  169 cases
  6,188 / 6,188 codes exact

independent fine total:
  357 cases
  16,300 / 16,300 codes exact

all-mipmap corpus:
  8 media cases
  24 layers
  96,222 / 96,222 codes exact

coarse aggregation differential corpus:
  3,219 / 3,219 points exact
```

The all-mipmap matrix includes 22,051 / 48k / 96k / 192k sources,
mono/stereo/4-channel layouts, RPKN PCM16, and RPKL float32.

## Waveform validation

RPKN PCM16 quantization was measured exhaustively over all 65,536 signed int16
values and incorporated into a larger corpus:

```text
122,516 / 122,516 waveform buckets exact
```

The normalized REAPER 7.79 mapping is asymmetric:

```text
x >= 0:  round_half_up(x * 32767)
x <  0: -round_half_up(-x * 32768)
```

Separate corpora confirm:

```text
RPKN decoded PCM24: 50,000 / 50,000 buckets exact
RPKL float:          43,857 / 43,857 values exact
```

The float32 `-'g'` oracle is intentionally independent of the RPKL waveform
quantizer. A diagnostic whole-file float matrix found a finite half-tie waveform
rounding edge that does not affect `-'g'`; whole-file RPKL identity for every
finite float bit pattern is therefore not implied by the 128-case `-'g'` claim.

## Loudness validation

The `-'r'` writer reproduces the tested REAPER 7.79 mode-3 behavior using a
libebur128-style K-weighting implementation with the recovered operation order:

- convolved fourth-order Direct Form II filter;
- `floor(sample_rate / 40)` 25 ms blocks;
- 16-block momentary and 120-block short-term rings;
- normalization from `round(sample_rate / 10) * 4` and `* 30`;
- observable ring update order `sum = (sum + new) - old`;
- no incomplete final 25 ms block flush;
- base records on the second waveform division cadence;
- complete-group averaging for coarser loudness levels.

Tiny algebraic changes can alter raw f32 bytes, so the operation order is part
of the tested compatibility behavior.

## REAPER preference-derived divisions

`peakcachegenrs` is a preference, not a constant 300. The measured REAPER 7.79
three-level rule implemented by `default_divisions` is:

```text
fine   = max(1, floor(sr / pps))
mid    = fine * max(1, ceil(sr / (fine * 20)))
coarse = mid  * max(1, ceil(sr / mid))
```

Examples:

```text
44,100 / 300 -> [147, 2205, 44100]
48,000 / 300 -> [160, 2400, 48000]
48,000 / 500 -> [96, 2400, 48000]
22,051 / 300 -> [73, 1168, 22192]
```

## Decoder provenance

For the tested ALAC/M4A input, REAPER's `reaper_video.so` was observed calling
FFmpeg functions including `avformat_open_input`, `av_read_frame`,
`avcodec_send_packet`, and `avcodec_receive_frame`. A WAVE-only control process
recorded zero calls to the monitored functions. This is specific to the tested
REAPER 7.79 Linux configuration.

## API coverage versus implementation coverage

Preferred REAPER-native writers:

```text
Rust:
  generate_pcm16_reaper
  generate_f32_reaper

Python:
  generate_pcm16_reaper
  generate_f32_reaper

C:
  rpk_generate_pcm16_reaper
  rpk_generate_f32_reaper
```

Lower-level Rust writers remain available:

```text
generate_pcm16_mode3
generate_f32_mode3
generate_pcm16_mode3_with_spectrogram
generate_f32_mode3_with_spectrogram
```

Legacy waveform + optional `-'s'` writers also remain available in all three
language surfaces for backward compatibility.

## Cache-path compatibility is separate

Byte-identical content is useful to REAPER only when stored at the path REAPER
expects. Central-cache filenames must not be guessed from `altpeakspath`; the
canonical application-layer path oracle is REAPER's `GetPeakFileNameEx`. See
`REAPER_CENTRAL_CACHE.md`.

## Known unsupported or unproven areas

Outside the current compatibility claim:

- REAPER versions other than 7.79 unless separately tested;
- Windows/macOS or non-x86_64 behavior without a dedicated oracle;
- every lossy codec and decoder build;
- legacy `-'l'` loudness payload parsing/generation;
- RPKM compact waveform materialization through the current pyramid;
- exact REAPER NaN/Inf/subnormal policy for arbitrary float media;
- whole-file RPKL byte identity for arbitrary finite float waveform rounding
  edges beyond the validated waveform corpora.

Malformed-input and overflow tests deliberately go beyond REAPER compatibility:
the Rust parser/generator, C ABI, and application helpers are expected to fail
closed rather than panic or cross unsafe boundaries. For float32 `-'g'`
generation specifically, non-finite source samples are sanitized to zero so
hostile input cannot poison FFT output or panic generation; that safety policy
is not presented as REAPER's exact exceptional-value behavior.

## Evidence sources

Key permanent evidence is in:

- `.github/workflows/reaper-individual-layer-oracle.yml`
- `.github/workflows/reaper-spectrogram-byte-identical.yml`
- `.github/workflows/reaper-spectrogram-stress-oracle.yml`
- `.github/workflows/reaper-spectrogram-f32-byte-identical.yml`
- `.github/workflows/reaper-ffmpeg-byte-identical.yml`
- `.github/workflows/reaper-adversarial-oracle.yml`
- `tests/reaper_peak_modes.rs`
- `tests/reaper_peak_modes_ffi.rs`
- `tests/python/test_reaper_generation_modes.py`
- `tests/reaper_spectrogram_exact.rs`
- `tests/reaper_spectrogram_g_only.rs`
- `tests/reaper_spectrogram_f32_g_only.rs`
- `tools/reaper_oracle/spectrogram_f32_stress_cases.py`
- `tools/reaper_oracle/spectrogram_f32_g_stress_run.py`
- `tests/spectrogram_stress.rs`
- `tests/spectrogram_structure_stress.rs`
- `tests/spectrogram_toggle_stress.rs`
- `tests/spectral_*`
- `tests/adversarial_properties.rs`
- `tests/c_api_adversarial.rs`
- `docs/validation-summary.json`
- `docs/REVERSE_ENGINEERING.md`
