# REAPER 7.79 `.ReaPeaks` reverse-engineering notes

Date: 2026-08-30  
Primary oracle: REAPER 7.79 x86_64 Linux  
Scope: waveform and spectral-peak cache compatibility

This document separates Cockos' public format facts from behavior measured
against a live REAPER executable. `strict-wdl` is intended to reproduce REAPER
7.79's spectral writer, including implementation quirks that are not useful DSP
rules by themselves.

## Evidence labels

- **Official** — documented by Cockos.
- **Oracle** — directly measured from REAPER 7.79-generated `.reapeaks` files.
- **Disassembly** — recovered from the REAPER 7.79 x86_64 executable and checked
  against differential probes.
- **Validated implementation** — exercised by CI against REAPER-generated
  golden data.

## Public sources

- Cockos `.ReaPeaks` format: https://www.reaper.fm/sdk/reapeaks.txt
- ReaScript API: https://www.reaper.fm/sdk/reascript/reascripthelp.html
- Cockos WDL: https://github.com/justinfrankel/WDL

# Live oracle method

REAPER 7.79 is run under Xvfb. A ReaScript creates a `PCM_source` and drives:

```text
PCM_Source_BuildPeaks(source, 0)  # begin
PCM_Source_BuildPeaks(source, 1)  # run until complete
PCM_Source_BuildPeaks(source, 2)  # finish
```

The oracle preferences used in the main corpus are:

```text
peakcachegenmode=3
peakcachegenrs=300
```

The most important harness rule is:

> **one media file = one fresh REAPER process**

During early batch probing, multiple `PCM_Source_BuildPeaks` operations in one
REAPER process produced spectral results that depended on the preceding source.
That makes a multi-source process unsuitable as a golden oracle. Xvfb may be
reused, but REAPER itself is restarted for every media file.

# File layout

## Header — Official

All multibyte integers are little-endian.

```text
0x00  4  RPKM / RPKN / RPKL
0x04  1  channels
0x05  1  mipmap count
0x06  4  source sample rate
0x0a  4  low32(st_mtime)
0x0e  4  low32(st_size)
0x12  ... 8-byte mipmap headers
...       payloads in header order
```

Mipmap header:

```text
int32  division_or_token
uint32 peak_count
```

Special negative tokens:

```text
-'s' = -115  spectral peaks
-'g' = -103  spectrogram
-'r' = -114  loudness
-'l' = -108  legacy loudness
```

## Spectral code — Official

One u32 per peak/channel:

```text
frequency_hz = code & 0x7fff
density      = (code >> 15) & 0x3fff
```

Density 16383 is most tonal; 0 is noise-like.

# Waveform generation

## RPKN PCM16 quantizer — Oracle

A 44.1 kHz mono WAV was constructed with 65,536 fine buckets, one bucket for
every possible signed 16-bit value. REAPER's stored extrema give the exact map:

```text
if v < 0:
    stored = v
else:
    stored = round_half_up(v * 32767 / 32768)
```

Equivalent normalized form:

```text
x >= 0:  round_half_up(x * 32767)
x <  0: -round_half_up(-x * 32768)
```

Notable values:

```text
-32768 -> -32768
-1     -> -1
0      -> 0
16384  -> 16384
16385  -> 16384
32767  -> 32766
```

Across the waveform validation corpus, **122,516 / 122,516 buckets are
byte-exact**.

A separate 50,000-bucket PCM24 probe confirmed the same normalized RPKN rule for
decoded integer media: **50,000 / 50,000 exact**.

## RPKL encoder — Official + Oracle

For a floating sample `x`:

```text
m = abs(x)

if m <= 1:
    code_mag = round_half_up(m * 24576)
else:
    code_mag = round_half_up(24576 + 1024*log2(m))

positive: clamp code_mag to 32767
negative: clamp code_mag to 32768, then negate
```

REAPER initializes each floating bucket as:

```text
max = -1.0
min = +1.0
```

before scanning samples. This matters for buckets entirely above +1 or below
-1. The measured RPKL corpus is **43,857 / 43,857 exact**.

# Spectral generation

## Analysis domain — Disassembly + Oracle

For source rates above 22,050 Hz, REAPER's spectral path operates in a 22,050 Hz
analysis domain and uses a 1024-point FFT.

For a source division `div`:

```text
analysis_hop = div * 22050 / source_rate
```

The analysis aperture is centered around the scheduled peak and has a 512-sample
half-width in the 22.05 kHz domain.

Window preparation has a precision-sensitive order:

1. analysis-ring sample is float32;
2. Hann coefficient is float32;
3. multiplication is performed in float32;
4. the product is promoted to double for the FFT input.

`strict-wdl` builds Cockos WDL with:

```text
WDL_FFT_REALSIZE=8
```

so the FFT buffer matches the recovered REAPER path.

## Fine spectral count — Oracle

The old source-domain approximation `floor((frames-1024)/div)` is not generally
correct; it happened to look correct at common rates such as 44.1 kHz.

For `source_rate > 22050`, the measured scheduler is reproduced by doing the
count in the analysis domain:

```text
analysis_frames = source_frames * 22050 / source_rate
analysis_hop    = source_division * 22050 / source_rate

fine_count = round_half_up((analysis_frames - 512) / analysis_hop)
```

The implementation uses the equivalent rational/integer form to avoid adding
new floating rounding ambiguity. This count formula matched every case in the
expanded rate/length corpus, including 22,051, 48k, 96k and 192k.

## <= 22,050 Hz compatibility quirk — Oracle

REAPER 7.79 has a special observable path for low-rate media:

```text
source_rate <= 22050
```

Spectral layers are still present, but their u32 payload codes are all zero.
The fine count follows the source domain:

```text
fine_count = round_half_up((source_frames - 512) / fine_division)
```

This is treated as a **strict REAPER-compatibility quirk**, not as a general DSP
rule. A non-strict application is free to analyze low-rate media normally.

## WDL resampler feed granularity — Oracle

The last remaining mismatch around the 22,051 Hz boundary was not the FFT or
the recovered spectral formula. WDL's IIR prefilter has a startup fade whose
slope depends on the number of frames passed to the resampler.

A fresh-process differential sweep found one exact feed size:

```text
interleaved source buffer = 2048 samples
block_frames = max(1, 2048 / channels)
```

At 22,051 Hz, both a deterministic tone and deterministic integer-noise probe
matched **61 / 61 spectral codes exactly** only at this feed granularity. The
same production setting subsequently passed mono, stereo, four-channel,
float32 and high-rate corpora.

## WDL FFT initialization race — Validated implementation

Upstream `WDL_fft_init()` uses an unsynchronized static flag and sets that flag
before all shared twiddle/permutation tables are populated. Parallel Rust tests
were able to observe a partially initialized table: one tone test could produce
a nonsensical first spectral code while another test passed.

`strict-wdl` therefore wraps WDL initialization in C++ `std::call_once` and only
runs transforms after initialization has completed. This does not change WDL's
math; it makes the shared initialization deterministic and thread-safe.

## Magnitudes — Disassembly

For a real 1024 FFT:

```text
mag[0]   = abs(DC)
mag[512] = abs(Nyquist)
mag[k]   = sqrt(re[k]^2 + im[k]^2), 1 <= k <= 511
```

The total magnitude is accumulated in double precision. A second copy of each
magnitude is rounded to float32 and is used by the density second-moment path.

Dominant-bin selection starts with Nyquist as the candidate and scans 1..511
using strict `>` comparison. DC contributes to density but is not a dominant
frequency candidate.

## Frequency refinement — Disassembly

For a non-Nyquist dominant bin `k`, the current double phase is compared with
the previous spectrum stored as float32 complex values:

```text
phase_cur  = atan2(cur_im_f64, cur_re_f64)
phase_prev = atan2(prev_im_f32, prev_re_f32)

residual = fmod(
    (phase_cur - phase_prev)/pi - 2*(elapsed/1024)*k,
    2
)

if residual <= -1: residual += 2
if residual >   1: residual -= 2

best_bin = k + (512/elapsed) * residual
frequency_hz = trunc(0.5 + best_bin * 22050 / 1024)
```

The result is clamped to the 15-bit frequency field.

## Density — Disassembly

Constants visible in the target routine include `16383`, `4`, `262144=512^2`
and `1/1024`. The recovered expression is:

```text
total  = sum(mag_f64[k], k=0..512)
spread = sum(float32(mag[k]) * (k-best_bin)^2, k=0..512)

density = trunc(
    0.5 + 16383 * (1 - 4*spread/(total*512^2))
)
```

then clamped to `[0,16383]`.

## Coarser spectral mipmaps — Oracle

Coarser spectral levels are aggregated **directly from the fine spectral level**,
not recursively from the previous level.

For a group of fine peaks:

```text
density_out = floor(mean(fine_density))
```

Frequency is copied from the fine peak maximizing:

```text
density * (32768 - frequency_hz)
```

The coarse count is derived from the fine count using the positive-division
ratio:

```text
coarse_count = floor(fine_count / (coarse_division / fine_division))
```

The aggregation rule matched **3,219 / 3,219** REAPER aggregate points during
reverse engineering.

# Byte-exact strict-WDL validation

The current CI gates are REAPER 7.79-generated data, not comparisons against a
second implementation of the same guessed algorithm.

## Fine spectral corpora

Fresh-process primary corpus:

```text
188 cases
10,112 u32 spectral codes
10,112 / 10,112 exact
```

Expanded corpus (boundary rates, high rates, mono/stereo/4ch and float32):

```text
169 cases
6,188 u32 spectral codes
6,188 / 6,188 exact
```

The expanded corpus is tested both through the fine spectral API and through:

```text
generate -> serialize .reapeaks -> parse -> spectral layer
```

Total independent fine-level coverage currently gated:

```text
357 cases
16,300 u32 codes
16,300 / 16,300 exact
```

## Independent all-mipmap corpus

A second live REAPER corpus uses 20-second deterministic LCG-noise media and a
fresh REAPER process for every file. It covers:

```text
22,051 Hz
48,000 Hz
96,000 Hz
192,000 Hz
mono
stereo
4-channel
RPKN PCM16
RPKL float32
```

For each file, all three spectral levels are hashed independently from REAPER's
actual `.reapeaks` payload and compared after libreapeaks full-file generation.

Current gate:

```text
8 media cases
24 spectral mipmap layers
96,222 u32 spectral codes
96,222 / 96,222 exact
```

Therefore, **strict-wdl is byte-exact for every spectral payload in the current
validated corpus**. This is strong compatibility evidence, but it is not a
mathematical claim that every possible input, REAPER version, architecture or
preference combination has been proven.

# Format-dependent behavior

For the same decoded 48 kHz stereo signal, REAPER 7.79 produced identical
wave/spectral/loudness payloads for:

```text
WAV PCM16
WAV PCM24
WAV PCM32
FLAC16
FLAC24
```

Float WAV produced the same spectral/loudness payload but RPKL waveform
encoding. On the tested REAPER build, MP3, Vorbis and Opus also select RPKL.

This is why libreapeaks treats decoded samples and output wave encoding as
separate concerns.

# GUI implications

REAPER's native positive-wave mipmaps form a useful persistent storage pyramid,
but they are sparse for arbitrary zoom levels. `WavePyramid` therefore adds
metadata-only geometric display levels and derives only visible ranges/tiles
from the fine native layer.

A default tile contains 4096 peaks and is keyed by:

```text
WaveTileKey { level_index, tile_index }
```

The API can expose lossless RGBA8 envelope and spectral-code textures to Qt6 /
PySide6, WebGL/WebGPU, browser `ImageData`, or other GPU-backed frontends without
creating a second persistent waveform cache.

See `docs/GUI_WAVEFORM.md`.

# Remaining work

The waveform and `-'s'` spectral-peak paths are now strongly validated. The main
remaining reverse-engineering work is outside that scope:

1. spectrogram (`-'g'`) generation;
2. loudness (`-'r'`) writer details — REAPER 7.79 fixtures occupy 4 bytes per
   channel/sample despite wording in the public text that suggests two floats;
3. NaN/Inf/subnormal RPKL policy if applications need those inputs;
4. mmap-backed parsing for extremely long peak files as a performance feature.

For all future REAPER oracle additions, keep the **fresh-process-per-media** rule.
