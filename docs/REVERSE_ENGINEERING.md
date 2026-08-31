# REAPER 7.79 `.ReaPeaks` reverse-engineering notes

Date updated: 2026-09-01  
Primary oracle: REAPER 7.79 x86_64 Linux  
Current scope: waveform, `-'s'` spectral peaks, `-'r'` loudness, division
selection, decoder-path provenance, and central-cache path policy

This document is the technical record behind libreapeaks' REAPER-compatibility
code. It separates public Cockos format facts from behavior measured against a
live REAPER executable and from implementation details recovered by differential
probing/disassembly.

For a shorter statement of what is actually proven today, read
[`COMPATIBILITY.md`](COMPATIBILITY.md).

## Evidence labels

- **Official** — documented by Cockos.
- **Oracle** — directly measured from REAPER 7.79-generated `.reapeaks` files or
  REAPER API results.
- **Runtime trace** — observed function/library calls inside a running REAPER
  process.
- **Disassembly** — recovered from the REAPER 7.79 x86_64 binary and checked
  against differential probes.
- **Validated implementation** — exercised by CI against REAPER-generated
  golden/oracle data.

## Public sources

- Cockos `.ReaPeaks` format: https://www.reaper.fm/sdk/reapeaks.txt
- ReaScript API: https://www.reaper.fm/sdk/reascript/reascripthelp.html
- Cockos WDL: https://github.com/justinfrankel/WDL

# Live oracle method

REAPER 7.79 is run headlessly under Xvfb. A ReaScript creates a `PCM_source` and
drives:

```text
PCM_Source_BuildPeaks(source, 0)  # begin
PCM_Source_BuildPeaks(source, 1)  # run until complete
PCM_Source_BuildPeaks(source, 2)  # finish
```

The common mode-3 configuration is:

```text
peakcachegenmode=3
peakcachegenrs=300
```

but the adversarial oracle also uses `peakcachegenrs=150` and `500`.

The most important harness rule is:

> **one media file = one fresh REAPER process**

Early batch probing showed spectral results that could depend on a preceding
source when several `PCM_Source_BuildPeaks` operations were performed in one
REAPER process. Xvfb can be shared, but golden media are built by fresh REAPER
instances.

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

Known special negative tokens:

```text
-'s' = -115  spectral peaks
-'g' = -103  spectrogram
-'r' = -114  current loudness records
-'l' = -108  legacy loudness
```

Current parser behavior:

- RPKN/RPKL positive waveform layers are materialized;
- RPKM is recognized and sized but its compact waveform payload is not exposed
  through `WavePyramid`;
- `-'s'` layers are parsed into spectral peaks;
- `-'g'` payload size is recognized and skipped, not decoded;
- `-'r'` layers are parsed into momentary/short-term energy records;
- the legacy `-'l'` payload layout is intentionally unsupported.

## Spectral code — Official

One u32 per peak/channel:

```text
frequency_hz = code & 0x7fff
density      = (code >> 15) & 0x3fff
```

Density 16383 is most tonal; 0 is noise-like.

## `-'r'` loudness layout — Oracle

REAPER 7.79 mode-3 files store two little-endian f32 values per time record and
channel:

```text
f32 momentary_energy
f32 short_term_energy
```

The `peak_count` field in the `-'r'` layer header is the number of f32 values
per channel, so it is twice the time-record count. Consequently:

```text
payload_bytes = peak_count * channels * 4
record_count  = peak_count / 2
```

The loudness record count is not required to equal the mirrored waveform bucket
count. EOF scheduling and some peak-rate combinations make such an equality
check invalid.

# REAPER preference-derived divisions

## `peakcachegenrs` is not fixed at 300 — Oracle

A live REAPER matrix measured `peakcachegenrs` values
100/150/200/300/500/1000 at 22,051 / 44,100 / 48,000 Hz. The recovered rule is:

```text
sr  = max(sample_rate, 1)
pps = max(peakcachegenrs, 1)

fine = max(1, floor(sr / pps))
mid  = fine * max(1, ceil(sr / (fine * 20)))
coarse = mid * max(1, ceil(sr / mid))
```

Examples:

```text
44,100 / 300 -> [147, 2205, 44100]
48,000 / 300 -> [160, 2400, 48000]
48,000 / 500 -> [96, 2400, 48000]
22,051 / 300 -> [73, 1168, 22192]
```

`default_divisions(sample_rate, fine_peaks_per_second)` implements this rule and
is exposed through Rust, Python, and `rpk_default_divisions` in the C ABI.

# Waveform generation

## RPKN PCM16 quantizer — Oracle

A mono probe assigned one fine bucket to every signed 16-bit value. REAPER's
stored extrema give the exact integer map:

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

The recorded waveform corpus contains **122,516 / 122,516 exact buckets**. A
separate decoded PCM24 probe confirmed the same normalized RPKN rule for
**50,000 / 50,000** buckets.

## RPKL encoder — Official + Oracle

For floating amplitude `x`:

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

before scanning samples. This affects buckets whose entire signal is above +1
or below -1. The recorded RPKL corpus is **43,857 / 43,857 exact**.

## Waveform EOF scheduling — Oracle

The adversarial whole-file oracle specifically probes exact and off-by-one EOF
positions. REAPER's positive waveform mipmaps do not all use a naive
`ceil(frames/division)` rule recursively. The implementation preserves the
measured fine-bucket flush and upper-mipmap completion behavior used by REAPER
7.79.

The permanent live oracle includes:

```text
3 seconds - 1 sample
exact 3 seconds
3 seconds + 1 sample
400 ms - 1 sample
exact 400 ms
400 ms + 1 sample
```

and compares the complete resulting mode-3 files.

# Spectral generation

## Analysis domain — Disassembly + Oracle

For source rates above 22,050 Hz, REAPER's spectral path operates in a 22,050 Hz
analysis domain and uses a 1024-point WDL FFT.

For source division `div`:

```text
analysis_hop = div * 22050 / source_rate
```

The analysis aperture is centered around the scheduled peak and has a 512-sample
half-width in the 22.05 kHz domain.

Precision-sensitive window preparation is:

1. analysis-ring sample stored as float32;
2. Hann coefficient stored as float32;
3. multiplication performed in float32;
4. product promoted to double for WDL FFT input.

`strict-wdl` builds Cockos WDL with:

```text
WDL_FFT_REALSIZE=8
```

so FFT storage matches the recovered path.

## Fine spectral count — Oracle

The old approximation `floor((frames-1024)/div)` is not generally correct. For
`source_rate > 22050`, the measured scheduler is equivalent to:

```text
analysis_frames = source_frames * 22050 / source_rate
analysis_hop    = source_division * 22050 / source_rate
fine_count      = round_half_up((analysis_frames - 512) / analysis_hop)
```

The implementation uses rational/integer arithmetic where possible to avoid
introducing additional floating-point ambiguity.

## <= 22,050 Hz REAPER 7.79 quirk — Oracle

For:

```text
source_rate <= 22050
```

REAPER 7.79 still creates spectral layers, but their u32 payload codes are zero.
The fine count follows the source-domain schedule:

```text
fine_count = round_half_up((source_frames - 512) / fine_division)
```

This is reproduced in `strict-wdl` as a compatibility quirk, not recommended as
general spectral-analysis behavior.

## WDL resampler feed granularity — Oracle

Near 22,051 Hz, WDL's IIR prefilter startup depends on how many frames are fed
to the resampler at once. The exact REAPER-compatible feed granularity found by
differential sweep is:

```text
interleaved source buffer = 2048 samples
block_frames = max(1, 2048 / channels)
```

At 22,051 Hz, deterministic tone and integer-noise probes matched all tested
spectral codes only at this granularity. The same setting then passed broader
mono/stereo/4-channel, float32, and high-rate corpora.

## WDL FFT initialization race — Validated implementation

Upstream `WDL_fft_init()` uses a process-global static initialization path that
is not safe for concurrent first use. Parallel tests were able to observe
partially initialized FFT tables.

The `strict-wdl` bridge therefore wraps WDL initialization in C++
`std::call_once`. The C ABI boundary also validates channel/rate/span inputs and
prevents C++ exceptions from crossing the ABI.

## Magnitudes — Disassembly

For a real 1024 FFT:

```text
mag[0]   = abs(DC)
mag[512] = abs(Nyquist)
mag[k]   = sqrt(re[k]^2 + im[k]^2), 1 <= k <= 511
```

Total magnitude is accumulated in double precision. A float32-rounded copy of
each magnitude is used by the density second-moment path.

Dominant-bin selection starts with Nyquist and scans 1..511 with strict `>`.
DC contributes to density but is not selected as the dominant frequency bin.

## Frequency refinement — Disassembly

For non-Nyquist dominant bin `k`, current double phase is compared with the
previous spectrum stored as float32 complex values:

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

The recovered expression is:

```text
total  = sum(mag_f64[k], k=0..512)
spread = sum(float32(mag[k]) * (k-best_bin)^2, k=0..512)

density = trunc(
    0.5 + 16383 * (1 - 4*spread/(total*512^2))
)
```

then clamped to `[0,16383]`.

## Coarser spectral mipmaps — Oracle

Coarser spectral levels are aggregated **directly from the fine spectral
level**, not recursively from the immediately finer stored level.

For a group of fine peaks:

```text
density_out = floor(mean(fine_density))
```

Frequency is copied from the fine peak maximizing:

```text
density * (32768 - frequency_hz)
```

The reverse-engineering differential corpus matched **3,219 / 3,219** aggregate
points.

# Loudness generation

## Root cause of the former mode-3 mismatch — Oracle + implementation tracing

Initial mode-3 files matched REAPER in waveform, spectral data, and short-term
loudness but differed by tiny f32 values in the momentary `-'r'` payload after
step/DC transitions. The mismatch was not caused by ALAC/FFmpeg decoding.

The exact REAPER 7.79 result is reproduced by the libebur128-style topology
below.

## K-weighting filter topology

The two K-weighting biquad sections are first convolved into one fourth-order
transfer function. Processing uses a Direct Form II state vector, rather than
running two separately rounded Direct Form I biquads.

For coefficients `b[0..4]`, `a[1..4]` and state `s[1..4]`:

```text
v0 = input
     - a1*s1
     - a2*s2
     - a3*s3
     - a4*s4

out = b0*v0
      + b1*s1
      + b2*s2
      + b3*s3
      + b4*s4

s4 = s3
s3 = s2
s2 = s1
s1 = v0
```

The coefficient-convolution expression tree is preserved because algebraically
equivalent rewrites can change the last bits of the raw loudness f32 payload.

## 25 ms energy blocks

REAPER-compatible block size:

```text
block_frames = max(1, floor(sample_rate / 40))
```

Filtered squared samples are accumulated into that block. An incomplete final
25 ms block is **not** pushed into the rolling energy rings at EOF.

Two rings are maintained per channel:

```text
momentary:   16 blocks
short-term: 120 blocks
```

The observable ring update order is:

```text
old = ring[next]
sum = (sum + new_energy) - old
ring[next] = new_energy
```

Writing the mathematically equivalent `(sum - old) + new_energy` changes tiny
DC-tail values and breaks byte identity.

## Loudness normalization

REAPER 7.79 matches libebur128-style normalization based on rounded 100 ms
sample counts:

```text
samples_100ms = round(sample_rate / 10)
momentary_normalization = samples_100ms * 4
short_term_normalization = samples_100ms * 30
```

In integer form used by the implementation:

```text
samples_100ms = (sample_rate + 5) / 10
```

The ring sums are divided by these frame counts, then stored as f32.

## Record and mipmap cadence

The base loudness layer mirrors the **second** positive waveform division
(`divisions[1]`), not the finest division. A record is emitted whenever that
base division completes and at EOF if an additional base record is required.

For coarser positive divisions, complete groups of base loudness records are
averaged. Partial final groups are not emitted at the coarser loudness level.

This behavior is why a `-'r'` layer's record count is not generally equal to the
waveform bucket count for the mirrored positive division.

## Adversarial whole-file confirmation

The permanent live oracle compares complete REAPER/libreapeaks files over 16
cases covering:

```text
22,050 / 32,000 / 44,100 / 48,000 / 88,200 / 96,000 Hz
peakcachegenrs 150 / 300 / 500
mono / stereo / 6-channel
EOF +/- 1 sample
400 ms window +/- 1 sample
step / impulse / deterministic noise
```

All current cases are byte-identical with the strict-WDL mode-3 PCM16 writer.

# FFmpeg decoder-path investigation

## Why `TYPE=VIDEO` was not accepted as proof

REAPER's source type string is too coarse to prove which backend performed the
decode. A process may also preload FFmpeg libraries even while decoding another
format, so checking `/proc/<pid>/maps` alone is insufficient.

## Direct FFmpeg function-call provenance — Runtime trace

A dedicated Linux workflow runs WAVE and ALAC/M4A inputs in separate REAPER
processes under GDB and traces calls to:

```text
avio_alloc_context
avformat_open_input
av_read_frame
avcodec_send_packet
avcodec_receive_frame
```

For the ALAC/M4A test, calls were observed from REAPER's `reaper_video.so` into
`libavformat.so.60` / `libavcodec.so.60`. The WAVE control process recorded zero
calls to those monitored FFmpeg functions while building the cache.

This establishes an actual FFmpeg demux/decode path for the tested REAPER 7.79
Linux ALAC source. It does not prove that every REAPER `VIDEO` source on every
platform uses the same backend.

## Decoder-path equivalence isolation

The same deterministic PCM was supplied to REAPER as WAVE and as lossless ALAC
M4A. After excluding source metadata differences, their peak payloads matched in
the diagnostic cases used to isolate the loudness mismatch.

Separately, the permanent 8-case FFmpeg gate:

1. generates deterministic PCM16;
2. encodes it to ALAC with FFmpeg;
3. decodes the ALAC with external FFmpeg and requires exact PCM16 round-trip;
4. asks REAPER to build mode-3 peaks from the ALAC/VIDEO source;
5. generates the same mode-3 cache with libreapeaks from the decoded PCM;
6. compares the **entire files** byte-for-byte.

The current signal set is:

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

# Central-cache path policy

Byte compatibility and path compatibility are separate problems.

REAPER's active configuration can place peak caches beside the source, in a
subdirectory, or in an alternate/central location. The exact central filename
is not safely derivable from `altpeakspath` alone.

The canonical application-layer resolver therefore delegates path policy to
REAPER's public:

```text
GetPeakFileNameEx(source, ..., forWrite)
```

The repository contains helpers to:

- read `peakcachegenrs`, `peakcachegenmode`, `altpeaks`, `altpeakspath`, and
  `altpeaksopathlist` from `reaper.ini`;
- launch a short-lived REAPER process solely to query read/write paths;
- save those path-policy answers in a versioned JSON cache map;
- reject unknown map versions and malformed path-query JSON rather than
  guessing.

See [`REAPER_CENTRAL_CACHE.md`](REAPER_CENTRAL_CACHE.md) for the important
distinction between the reusable policy helper and the current runnable demo
CLI wiring.

# Validation totals

The deeper spectral/waveform totals are kept in
[`validation-summary.json`](validation-summary.json). The two strongest current
continuous whole-file gates are:

```text
FFmpeg/ALAC mode-3:       8 / 8 whole files byte-identical
adversarial mode-3:      16 / 16 whole files byte-identical
```

The spectral corpora additionally contain:

```text
357 fine cases / 16,300 spectral codes exact
8 all-mipmap cases / 24 layers / 96,222 codes exact
3,219 / 3,219 aggregate points exact
```

# Format-dependent observations

For the same decoded 48 kHz stereo signal, historical REAPER 7.79 probes found
identical wave/spectral/loudness payloads for integer lossless sources including
WAV PCM16/24/32 and FLAC16/24. A float WAV used RPKL waveform encoding while
retaining the corresponding spectral/loudness behavior. On the tested build,
MP3, Vorbis, and Opus were also observed using RPKL waveform encoding.

These are observations from a specific REAPER build, not a rule for selecting
wave encoding solely from a file extension. libreapeaks therefore keeps decoded
sample representation and chosen RPKN/RPKL output encoding separate.

# Remaining work / intentionally unsupported areas

The major waveform, `-'s'` spectral, and `-'r'` mode-3 loudness algorithms are
now represented in the implementation and current oracle suite. Remaining work
is primarily outside that proven surface:

1. `-'g'` spectrogram generation;
2. legacy `-'l'` payload layout;
3. RPKM compact waveform payload materialization;
4. exact REAPER policy for arbitrary NaN/Inf/subnormal float media;
5. broader OS/architecture/REAPER-version/codec oracle matrices;
6. complete mode-3 generation entry points in the public C and Python writer
   APIs;
7. mmap-backed parsing for extremely long files as a performance feature.

Future golden-oracle additions should continue to use a fresh REAPER process per
media source and should distinguish **observed REAPER behavior** from general
DSP rules.
