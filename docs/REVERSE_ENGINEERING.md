# REAPER 7.79 `.reapeaks` reverse-engineering notes

Date updated: 2026-09-02  
Primary oracle: **REAPER 7.79 x86_64 Linux**

This is the technical record behind libreapeaks' compatibility implementation.
For the concise compatibility contract, read [`COMPATIBILITY.md`](COMPATIBILITY.md).
For the finite-f32 proof boundary, read
[`F32_FINITE_PROOF.md`](F32_FINITE_PROOF.md).

## Evidence labels

- **Official** — documented by Cockos.
- **Oracle** — measured from the pinned REAPER executable or a public REAPER API.
- **Runtime trace** — observed in a running REAPER process.
- **Disassembly** — recovered from REAPER 7.79 x86_64 and checked against probes.
- **Validated implementation** — continuously exercised by repository tests or
  live-oracle workflows.

A key rule for permanent live evidence is:

> **one media file = one fresh REAPER process**

Batching multiple builds in one process exposed state-sensitive spectral
behavior, so golden generation does not depend on a warmed REAPER instance.

# File layout

All multibyte integers are little-endian.

```text
0x00  4  RPKM / RPKN / RPKL
0x04  1  channels
0x05  1  mipmap count
0x06  4  source sample rate
0x0a  4  low32(st_mtime)
0x0e  4  low32(st_size)
0x12  ... 8-byte layer headers
...       payloads in header order
```

Each layer header is:

```text
int32  division_or_token
uint32 peak_count
```

Known negative tokens:

```text
-'s' = -115  spectral peaks
-'g' = -103  spectrogram
-'r' = -114  current loudness
-'l' = -108  legacy loudness
```

Current parser coverage:

- RPKN/RPKL positive waveform layers are materialized;
- RPKM is recognized and sized, but its compact waveform payload is not exposed
  through `WavePyramid`;
- `-'s'` is parsed into spectral-peak records;
- `-'g'` is parsed into 128-bin `SpectrogramFrame` records;
- `-'r'` is parsed into momentary/short-term energy records;
- legacy `-'l'` payload layout remains unsupported.

# Native REAPER cache shapes

A fresh-process sweep of 71 `showpeaks` configurations found only these special
layer sets:

```text
waveform:     waveform only
spectral:     waveform + -'s' + -'r'
spectrogram:  waveform + -'s' + -'g' + -'r'
```

No native `-'s'`-only, `-'g'`-only, or `-'r'`-only cache was observed. This is
why the preferred public API exposes `ReaperPeakMode::{Waveform, Spectral,
Spectrogram}`.

# Preference-derived positive divisions

`peakcachegenrs` is a preference, not a fixed 300. REAPER 7.79 follows:

```text
sr  = max(sample_rate, 1)
pps = max(peakcachegenrs, 1)

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

# Waveform generation

## RPKN PCM16 quantizer — exhaustive integer oracle

The recovered normalized mapping is asymmetric:

```text
x >= 0:  round_half_up(x * 32767)
x <  0: -round_half_up(-x * 32768)
```

All 65,536 signed-int16 input values were covered. The larger recorded corpus is
**122,516 / 122,516** exact waveform buckets. A decoded PCM24 corpus adds
**50,000 / 50,000** exact buckets.

## RPKL finite-f32 quantizer — exhaustive decision-boundary oracle

For finite `x`, let `a = abs(x)` and define the transformed magnitude:

```text
if a <= 1:
    q = a * 24576
else:
    q = 24576 + 1024 * log2(a)
```

REAPER 7.79 does **not** simply round `abs(x)` half-up and apply the sign later.
Its exact finite-f32 rule is equivalent to rounding a signed transformed value
with `floor(y + 0.5)`. In magnitude form:

```text
x > 0:
    code = floor(q + 0.5), clamped to +32767

x < 0:
    code = -ceil(q - 0.5), magnitude clamped to 32768

+0.0 / -0.0:
    code = 0
```

Therefore exact `.5` ties are sign-asymmetric. Example:

```text
 0.63934326171875 -> q=15712.5 ->  15713
-0.63934326171875 -> q=15712.5 -> -15712
```

The permanent live oracle binary-searches REAPER's classifier in finite-f32 bit
order and recovers every code transition:

```text
positive transitions: 32,767
negative transitions: 32,768
total transitions:    65,535
exact-half ties:        8,192
```

The transition partition covers **4,278,190,080 finite f32 bit patterns**,
including signed zero and all subnormals. The scalar RPKL waveform quantizer is
therefore exhaustive for the pinned REAPER oracle, not merely sampled by the
older 43,857-value corpus.

Permanent evidence:

- `tools/reaper_oracle/rpkl_finite_boundary_oracle.py`
- `tests/reaper_rpkl_finite_boundaries.rs`
- `.github/workflows/reaper-rpkl-finite-boundaries.yml`

## Waveform EOF scheduling

Positive mipmaps do not reduce to one recursive `ceil(frames/division)` rule.
The implementation preserves the observed fine-bucket flush and upper-level
completion behavior. Permanent whole-file cases cover exact and ±1-sample EOF
boundaries.

# `-'s'` spectral generation

`-'s'` is a per-peak frequency/density layer, distinct from `-'g'`.

A spectral code decodes as:

```text
frequency_hz = code & 0x7fff
density      = (code >> 15) & 0x3fff
```

For source rates above 22,050 Hz, REAPER uses a 22,050 Hz analysis domain and a
1024-point WDL real FFT. Compatibility depends on WDL resampling, exact feed
size, f32 Hann-window multiplication, FFT precision, phase history, density
operation order, aggregation, and EOF scheduling.

At `source_rate <= 22050`, REAPER 7.79 still emits `-'s'` layers but their codes
are zero. strict-WDL reproduces this as a version-specific quirk.

## WDL feed granularity

Differential sweeps recovered:

```text
interleaved source buffer = 2048 doubles
block_frames = max(1, 2048 / channels)
```

Upstream `WDL_fft_init()` has process-global first-use state, so the strict
bridge wraps initialization in `std::call_once`.

## Even-window center and EOF tail

A broad finite-f32 whole-file oracle exposed a scheduler boundary that a simple
integer 512-analysis-sample margin misses. The 1024-point even window is centered
at **511.5 analysis samples**.

The high-rate fine-count boundary can be represented without floating-point
rounding by doubling all terms:

```text
source_span_twice = frames * 22050 * 2
margin_twice      = 1023 * source_rate
hop_twice         = division * 22050 * 2

count = round_half_up((source_span_twice - margin_twice) / hop_twice)
```

Observed boundary examples:

```text
50,550 frames @ 76,800 Hz, division 256 -> 191 records
192,131 frames @ 192,000 Hz, division 192 -> 977 records
```

The first case also showed that WDL's last valid resampled output alone stops one
analysis sample before REAPER's final spectral scheduler event. REAPER advances
the analysis-domain ring once at EOF. strict-WDL mirrors that with one zero
analysis-frame tail; the oracle-derived expected count still caps emission, so
cases that already reached their target do not consume the tail.

## Invalid spectral totals from finite source data

Finite source samples can create non-finite intermediates. At extreme magnitudes,
the scalar f32 Hann multiply may overflow and a zero window coefficient can then
produce:

```text
Inf * 0 -> NaN
```

REAPER proceeds only when total spectral magnitude is **ordered-greater than
zero**. NaN therefore produces a zero `SpectralPeak`. `total <= 0` is not an
equivalent test because comparisons with NaN are false.

This behavior is relevant to the finite-input compatibility claim even though
source NaN/Inf themselves remain outside the claim.

## Phase refinement and coarse aggregation

For a non-Nyquist dominant bin, current phase is f64 while the previous complex
spectrum is retained as f32. Coarser `-'s'` levels aggregate from the fine level:

- density = floor(mean density);
- frequency is selected from the fine record maximizing
  `density * (32768 - frequency_hz)`.

Recorded strict validation includes:

```text
independent fine codes: 16,300 / 16,300 exact
all-mipmap codes:       96,222 / 96,222 exact
aggregation points:      3,219 / 3,219 exact
```

# `-'g'` spectrogram generation

## On-disk layout

One channel/time frame contains:

```text
128 bins * 12 bits = 1536 bits = 192 bytes = 48 u32 words
```

The layer token is `-103`. Header `peak_count` is the number of **u32 words per
channel**, not logical time frames:

```text
time_frames   = peak_count / 48
payload_bytes = peak_count * channels * 4
```

Payload ordering is time-major, channel-inner.

Two 12-bit codes `a` and `b` are packed into three bytes:

```text
[a >> 4,
 ((a & 0x0f) << 4) | (b & 0x0f),
 b >> 4]
```

## FFT bins and window

The FFT size is **256**. Stored bins are real-FFT bins **1..128**: DC is omitted,
Nyquist retained.

The four-term Blackman-Harris coefficients are:

```text
A0 = 0.35875
A1 = 0.48829
A2 = 0.14128
A3 = 0.01168
```

REAPER uses a mixed f64/f32 construction path: phase/cosine and the raw sum are
f64, raw coefficients are stored as f32, inverse-sum is converted to f32, and
normalization is f32 multiplication. Phase advances by repeated addition.

PCM16 samples are first scaled by f32 `1/32768`. Float32 media use the source
f32 sample directly. In both cases the sample/window multiplication is f32 and
the product is then promoted to f64 for the FFT.

## Placement

For positive fine division `d`:

```text
if d >= 256:
    shift = d - 256
else:
    shift = (d - 256) / 2   # signed truncation toward zero
```

At the leading edge, unavailable negative-time samples are not modeled as zeros
under a fixed 256-point window. REAPER rebuilds a symmetric Blackman-Harris
window for the number of real samples available, then zero-pads the FFT input.

## Power quantization

REAPER quantizes squared magnitude directly:

```text
power = re*re + im*im
```

No square root is taken. Recovered constants:

```text
POWER_LOG_SCALE = 88.92179516969081
CODE_BIAS       = 4095.5
CODE_MAX        = 4095
```

For finite `0 < power < 1`:

```text
raw  = ln(power) * POWER_LOG_SCALE + CODE_BIAS
code = trunc(raw), clamped to 0..4095
```

Non-positive/invalid power maps to zero; power >= 1 maps to 4095.

## Mipmap aggregation

The 256-point analyses first produce quantized 12-bit base frames. The first
stored `-'g'` level averages groups of those quantized codes; its final partial
group may be emitted. Higher stored mipmaps average complete groups of the
immediately preceding stored frames; incomplete higher-level groups are not
emitted.

## Validation

Permanent pinned-REAPER gates include:

```text
PCM16 strict-WDL spectrogram mode-3:
  122 / 122 complete files byte-identical

PCM16 portable/default FFT:
  122 / 122 exact -'g' comparisons

finite float32/RPKL -'g':
  128 / 128 decoded-frame and packed-payload cases exact
```

The finite float32 `-'g'` corpus is deliberately independent of the scalar RPKL
waveform proof.

# `-'r'` loudness generation

REAPER mode-3 stores two little-endian f32 values per time record/channel:

```text
f32 momentary_energy
f32 short_term_energy
```

Header `peak_count` is the number of f32 values per channel and therefore twice
the logical record count.

The byte-exact path uses libebur128-style K-weighting with two biquads convolved
into one fourth-order Direct Form II filter. Energy blocks are
`max(1, floor(sample_rate/40))` frames. Momentary and short-term rings contain 16
and 120 blocks. The observable update order is:

```text
old = ring[next]
sum = (sum + new_energy) - old
ring[next] = new_energy
```

An incomplete final 25 ms block is not inserted at EOF. Base records follow the
second positive waveform division; coarser levels average complete groups.

# Decoder provenance

For the tested ALAC/M4A path, REAPER's `reaper_video.so` was observed calling
FFmpeg entry points including `avformat_open_input`, `av_read_frame`,
`avcodec_send_packet`, and `avcodec_receive_frame`. A WAVE control process
recorded zero calls to the monitored functions. This statement is limited to the
validated REAPER 7.79 Linux configuration.

# Central-cache path policy

Byte compatibility and path compatibility are separate. Central-cache filenames
should not be guessed from `altpeakspath`. The canonical application-layer path
oracle is REAPER's `GetPeakFileNameEx`; see `REAPER_CENTRAL_CACHE.md`.

# Proof boundary and remaining work

Exhaustively established against the pinned oracle:

- RPKN PCM16 scalar quantization over all signed-int16 values;
- RPKL waveform scalar quantization over every finite f32 bit pattern;
- all 65,535 finite-f32 RPKL decision boundaries and 8,192 representable
  sign-asymmetric exact-half ties.

Byte-exact live-oracle evidence additionally covers finite whole-file edge and
broad operational matrices, plus the dedicated PCM16/float32 layer matrices.
Those stateful corpus gates are not a formal proof over every arbitrary-length
input sequence.

Still outside the current claim:

- source NaN, `+Inf`, and `-Inf` exact REAPER policy;
- RPKM compact waveform materialization through `WavePyramid`;
- legacy `-'l'` payload layout;
- REAPER versions/platforms/architectures outside named oracle matrices;
- codecs/decoder builds outside the tested provenance and whole-file gates.

Finite subnormals are **inside** the finite-f32 scalar proof and finite-edge
whole-file evidence; they are no longer listed as an unproven exceptional-value
case.
