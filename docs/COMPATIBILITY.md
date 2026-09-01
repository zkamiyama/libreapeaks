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

`generate_pcm16_mode3_with_spectrogram` is intentionally separate from
`generate_pcm16_mode3`. The established non-`g` mode-3 byte stream therefore
remains unchanged unless the caller explicitly chooses spectrogram generation.

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

### Spectrogram mode-3 stress matrix

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
```

Generation currently targets PCM16 RPKN mode-3. There is no public f32/C/Python
complete-mode-3 `-'g'` generation entry point yet.

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

Complete Rust mode-3 writers:

```text
generate_pcm16_mode3
generate_f32_mode3
generate_pcm16_mode3_with_spectrogram   # PCM16 + -'g'
```

C and Python generation currently expose waveform plus optional `-'s'` spectral
layers, not complete `-'r'`/`-'g'` mode-3 writing. Parsing/GUI APIs have their
own documented surfaces; use the current source/header as the API source of
truth.

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
- f32/RPKL `-'g'` generation;
- complete `-'g'`/mode-3 writer access through the current C/Python APIs;
- legacy `-'l'` loudness payload parsing/generation;
- RPKM compact waveform materialization through the current pyramid;
- exact REAPER NaN/Inf/subnormal policy for arbitrary float media.

Malformed-input and overflow tests deliberately go beyond REAPER compatibility:
the Rust parser/generator, C ABI, and application helpers are expected to fail
closed rather than panic or cross unsafe boundaries.

## Evidence sources

Key permanent evidence is in:

- `.github/workflows/reaper-spectrogram-byte-identical.yml`
- `.github/workflows/reaper-spectrogram-stress-oracle.yml`
- `.github/workflows/reaper-ffmpeg-byte-identical.yml`
- `.github/workflows/reaper-adversarial-oracle.yml`
- `tests/reaper_spectrogram_exact.rs`
- `tests/reaper_spectrogram_g_only.rs`
- `tests/spectrogram_stress.rs`
- `tests/spectrogram_structure_stress.rs`
- `tests/spectrogram_toggle_stress.rs`
- `tests/spectral_*`
- `tests/adversarial_properties.rs`
- `tests/c_api_adversarial.rs`
- `docs/validation-summary.json`
- `docs/REVERSE_ENGINEERING.md`
