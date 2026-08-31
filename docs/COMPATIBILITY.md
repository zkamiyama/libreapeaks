# Compatibility and validation scope

This document is the short, authoritative answer to: **what does libreapeaks
actually reproduce, and under which conditions has that been proven?**

The implementation contains both general `.reapeaks` parsing/generation code
and REAPER-compatibility code recovered from REAPER 7.79. A passing oracle means
the generated file or payload was compared against a file produced by a real,
pinned REAPER executable. It does not mean that every REAPER release behaves
the same way.

## Current compatibility statement

For the validated corpora, the Rust `strict-wdl` mode-3 generator reproduces
REAPER 7.79 x86_64 Linux `.reapeaks` output **byte-for-byte**, including:

- RPKN waveform layers;
- mirrored `-'s'` spectral layers;
- `-'r'` momentary/short-term loudness layers;
- layer headers and counts;
- source metadata fields when the same source size/mtime values are supplied;
- REAPER 7.79 EOF and coarse-mipmap scheduling behavior exercised by the live
  oracle suite.

The full-file statement currently applies to `generate_pcm16_mode3` with
`strict-wdl` in the tested configurations below. The float32 mode-3 path uses
the same recovered spectral/loudness machinery and has separate waveform and
spectral coverage, but it is not covered by the two whole-file matrices below
in every combination.

## Whole-file live REAPER gates

### FFmpeg-backed ALAC / M4A matrix

The `reaper-ffmpeg-byte-identical` workflow creates deterministic 48 kHz stereo
PCM16, encodes it losslessly to ALAC/M4A with FFmpeg, verifies that an external
FFmpeg decode returns the original PCM16 byte-for-byte, asks REAPER 7.79 to
build a fresh mode-3 cache from the M4A, then compares the complete file with
libreapeaks strict-WDL output.

All 8 cases are whole-file byte-identical:

```text
silence
sine_997
step
impulse
dc_tail
low_level
block_edge_impulse
alternating
```

Configuration:

```text
REAPER:             7.79 x86_64 Linux
peakcachegenmode:   3
peakcachegenrs:     300
source rate:        48,000 Hz
channels:           2
source container:   M4A
source codec:       ALAC
REAPER source type: VIDEO
```

The signal set deliberately includes low-amplitude values, abrupt transients,
DC decay, 25 ms / 400 ms block boundaries, and full-scale alternating samples,
so it is sensitive to tiny filter and accumulation-order differences.

### Adversarial rate / EOF / channel matrix

The `reaper-adversarial-oracle` workflow runs 16 independent whole-file cases.
Each case is converted to lossless ALAC/M4A, each source is processed by a
fresh REAPER process, and the complete REAPER file is compared with
libreapeaks.

The matrix covers:

```text
sample rates:
  22,050
  32,000
  44,100
  48,000
  88,200
  96,000 Hz

peakcachegenrs:
  150
  300
  500

channels:
  mono
  stereo
  6-channel

boundary cases:
  3-second EOF - 1 sample
  exact 3-second EOF
  3-second EOF + 1 sample
  400 ms window - 1 sample
  exact 400 ms window
  400 ms window + 1 sample
```

The generated signals include deterministic steps, impulses placed on 25 ms
block edges and at EOF, and deterministic multichannel noise.

## Waveform validation

RPKN PCM16 quantization was measured exhaustively over all 65,536 signed int16
values and incorporated into a larger waveform corpus:

```text
122,516 / 122,516 waveform buckets exact
```

The normalized RPKN mapping recovered from REAPER 7.79 is asymmetric:

```text
x >= 0:  round_half_up(x * 32767)
x <  0: -round_half_up(-x * 32768)
```

A separate decoded PCM24 corpus confirmed the same normalized rule:

```text
50,000 / 50,000 buckets exact
```

RPKL float waveform encoding was checked over 43,857 values including
large-range probes:

```text
43,857 / 43,857 exact
```

REAPER's RPKL bucket initialization (`max=-1.0`, `min=+1.0`) is reproduced.

## Spectral validation

The strict-WDL spectral path is backed by Cockos WDL and reproduces REAPER's
measured resampling, windowing, FFT, phase refinement, density, and coarse-level
aggregation behavior.

Current fixed corpora recorded in `validation-summary.json`:

```text
fresh-process primary corpus:
  188 cases
  10,112 / 10,112 spectral codes exact

expanded fine corpus:
  169 cases
  6,188 / 6,188 spectral codes exact

independent fine total:
  357 cases
  16,300 / 16,300 spectral codes exact

all-mipmap corpus:
  8 media cases
  24 spectral layers
  96,222 / 96,222 spectral codes exact

coarse aggregation differential corpus:
  3,219 / 3,219 points exact
```

The all-mipmap spectral corpus includes 22,051 / 48k / 96k / 192k sources,
mono/stereo/4-channel layouts, RPKN PCM16, and RPKL float32.

## Loudness validation

The mode-3 `-'r'` writer is no longer an open reverse-engineering item. The
remaining whole-file mismatch found during development was traced to the exact
loudness implementation and fixed.

The validated REAPER 7.79 behavior is reproduced with:

- libebur128-style K-weighting coefficients;
- the two biquad sections convolved into one fourth-order Direct Form II
  filter;
- 25 ms energy blocks using `floor(sample_rate / 40)` frames;
- 16 blocks for momentary energy (nominally 400 ms);
- 120 blocks for short-term energy (nominally 3 s);
- normalization based on `round(sample_rate / 10) * 4` and `* 30` frames;
- ring accumulation in the observable order `sum = (sum + new) - old`;
- no flush of an incomplete final 25 ms energy block;
- records emitted on the second waveform division cadence, with an EOF record
  when needed;
- coarser loudness layers averaged from complete groups of the base loudness
  records.

Tiny differences in the filter topology or addition/subtraction order survive
as different raw f32 bytes after step/DC transients, which is why the exact
operation order is treated as compatibility behavior rather than an algebraic
implementation detail.

## REAPER decoder provenance

`GetMediaSourceType(...)=VIDEO` alone is not considered proof that FFmpeg is
used. A dedicated Linux provenance workflow traces real FFmpeg function calls
inside the REAPER process.

For the tested ALAC/M4A input, REAPER's `reaper_video.so` was observed calling
FFmpeg functions including:

```text
avio_alloc_context
avformat_open_input
av_read_frame
avcodec_send_packet
avcodec_receive_frame
```

The corresponding WAVE-only control process recorded zero calls to those
functions while building its peak cache. In the tested Ubuntu environment the
M4A path used `libavformat.so.60` and `libavcodec.so.60`.

A separate REAPER WAVE-vs-M4A comparison showed equal normalized peak payloads
for the diagnostic signals used to isolate the original loudness mismatch.
This ruled out the decoder path as the cause of that mismatch.

The provenance statement is specific to the tested REAPER 7.79 Linux / ALAC
configuration. It must not be generalized to every codec or platform without a
corresponding trace.

## REAPER preference-derived divisions

`peakcachegenrs` is a preference, not a fixed value of 300. The measured REAPER
7.79 three-level division rule is implemented by `default_divisions` and
exposed from Rust, Python, and C.

For sample rate `sr` and requested fine peak rate `pps`:

```text
fine   = max(1, floor(sr / pps))
mid    = fine * max(1, ceil(sr / (fine * 20)))
coarse = mid  * max(1, ceil(sr / mid))
```

Examples:

```text
44,100 Hz / 300 -> [147, 2205, 44100]
48,000 Hz / 300 -> [160, 2400, 48000]
48,000 Hz / 500 -> [96, 2400, 48000]
22,051 Hz / 300 -> [73, 1168, 22192]
```

The original live division probe also covered `peakcachegenrs`
100/150/200/300/500/1000 at 22,051 / 44,100 / 48,000 Hz.

## Cache-path compatibility is separate from byte compatibility

A byte-identical `.reapeaks` file is useful to REAPER only when it is stored at
the path REAPER expects. Central cache filenames must not be guessed from
`altpeakspath`.

The canonical path policy is queried with REAPER's `GetPeakFileNameEx` and can
be persisted in a versioned cache map. See `REAPER_CENTRAL_CACHE.md`.

## API coverage versus implementation coverage

The Rust core exposes the complete recovered mode-3 writer:

```text
generate_pcm16_mode3
generate_f32_mode3
```

The current public C and Python writer APIs expose waveform plus optional
spectral layers only:

```text
C:      rpk_generate_pcm16 / rpk_generate_f32
Python: generate_pcm16 / generate_f32
```

They do **not** currently expose the Rust mode-3 loudness writer. Existing
C/Python parsing and GUI APIs can open the waveform/spectral portions of REAPER
files; the Rust parser also materializes `-'r'` loudness records.

## Known unsupported or unproven areas

The following are intentionally outside the current byte-exact claim:

- REAPER releases other than 7.79 unless separately tested;
- Windows/macOS behavior unless a dedicated oracle says otherwise;
- architectures other than the validated x86_64 Linux runtime;
- every lossy codec and every possible decoder version/build option;
- `-'g'` spectrogram generation;
- legacy `-'l'` loudness payload parsing/generation;
- RPKM compact waveform payload exposure through the current waveform pyramid;
- REAPER's exact NaN/Inf/subnormal policy for arbitrary float media;
- complete mode-3 writer access through the current C/Python generation APIs.

Malformed-input and overflow tests intentionally go beyond REAPER compatibility:
the Rust parser/generator, C ABI, and Python cache/config helpers are exercised
with truncation, count corruption, invalid pointers/geometry, extreme division
values, non-object JSON, invalid cache-map versions, and other fail-closed
cases.

## Evidence sources in the repository

- `.github/workflows/reaper-ffmpeg-byte-identical.yml`
- `.github/workflows/reaper-adversarial-oracle.yml`
- `.github/workflows/reaper-ffmpeg-function-call-provenance.yml`
- `tests/reaper_ffmpeg_byte_identical.rs`
- `tests/reaper_adversarial_oracle.rs`
- `tests/spectral_*`
- `tests/adversarial_properties.rs`
- `tests/c_api_adversarial.rs`
- `tests/python/test_reaper_config_adversarial.py`
- `docs/validation-summary.json`
- `docs/REVERSE_ENGINEERING.md`
