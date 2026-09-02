# REAPER 7.79 `.ReaPeaks` reverse-engineering notes

Date updated: 2026-09-02  
Primary oracle: REAPER 7.79 x86_64 Linux  
Current scope: waveform, `-'s'` spectral peaks, `-'g'` spectrogram, `-'r'`
loudness, division selection, decoder provenance, and cache-path policy

This is the technical record behind libreapeaks' compatibility implementation.
It distinguishes documented format facts from behavior measured against a live,
pinned REAPER executable and from details recovered by differential probing or
binary inspection. For the shorter compatibility contract, read
[`COMPATIBILITY.md`](COMPATIBILITY.md).

## Evidence labels

- **Official** — documented by Cockos.
- **Oracle** — directly measured from REAPER 7.79-generated `.reapeaks` files or
  public REAPER API results.
- **Runtime trace** — observed calls inside a running REAPER process.
- **Disassembly** — recovered from the REAPER 7.79 x86_64 binary and checked
  against differential probes.
- **Validated implementation** — continuously exercised by CI/oracle tests.

## Live oracle method

REAPER 7.79 is run headlessly under Xvfb. ReaScript drives
`PCM_Source_BuildPeaks` begin/run/finish. Golden media follow one rule that
proved essential during spectral work:

> **one media file = one fresh REAPER process**

Batching multiple peak builds in one process exposed state-sensitive behavior,
so permanent golden generation does not rely on a warmed REAPER instance.

Common mode-3 configuration is `peakcachegenmode=3`; `peakcachegenrs` is varied
throughout the oracle matrices rather than assumed to be 300.

# File layout

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

Each mipmap header is:

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
- RPKM is recognized/sized, but its compact waveform payload is not exposed
  through `WavePyramid`;
- `-'s'` layers are parsed into spectral peaks;
- `-'g'` layers are decoded into 128-bin `SpectrogramFrame` records;
- `-'r'` layers are parsed into momentary/short-term energy records;
- legacy `-'l'` payload layout remains intentionally unsupported.

# REAPER preference-derived divisions

A live matrix measured `peakcachegenrs` 100/150/200/300/500/1000 at several
rates. The recovered REAPER 7.79 rule is:

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

`default_divisions` implements this behavior and is exposed from Rust, Python,
and C.

# Waveform generation

## RPKN PCM16 quantizer — Oracle

Exhaustive signed-int16 probing recovered the asymmetric normalized mapping:

```text
x >= 0:  round_half_up(x * 32767)
x <  0: -round_half_up(-x * 32768)
```

Notable integer results:

```text
-32768 -> -32768
-1     -> -1
0      -> 0
16384  -> 16384
16385  -> 16384
32767  -> 32766
```

Recorded validation: **122,516 / 122,516** waveform buckets exact. A separate
PCM24 corpus gives **50,000 / 50,000** exact buckets.

## RPKL float waveform encoding — Official + Oracle

For magnitude `m = abs(x)`, the established non-tie behavior follows:

```text
m <= 1: code_mag ~= m * 24576
m >  1: code_mag ~= 24576 + 1024*log2(m)
```

Positive values clamp to 32767; negative magnitude clamps to 32768 before sign
application. REAPER initializes each RPKL bucket with `max=-1.0`, `min=+1.0`.
The recorded float corpus is **43,857 / 43,857** exact.

A later float32 spectrogram whole-file diagnostic exposed one exact linear
half-tie not covered by that corpus: source `-0.63934326171875` gives magnitude
`15712.5` after the 24576 scale, and REAPER 7.79 stored `-15712` while the old
half-up implementation stored `-15713`. This is a waveform-quantizer rounding
edge, independent of `-'g'`; the general exact RPKL tie rule should therefore
not be inferred from the earlier non-tie corpus alone.

## Waveform EOF scheduling — Oracle

Positive mipmaps do not reduce to a single recursive `ceil(frames/division)`
formula. The implementation preserves observed fine-bucket flush and upper-level
completion behavior. Permanent whole-file tests include exact and ±1-sample EOF
and loudness-window boundaries.

# `-'s'` spectral generation

`-'s'` is the older per-peak spectral-frequency/density layer and must not be
confused with `-'g'` spectrogram bins.

One official `-'s'` code is:

```text
frequency_hz = code & 0x7fff
density      = (code >> 15) & 0x3fff
```

For source rates above 22,050 Hz, the recovered path uses a 22,050 Hz analysis
domain and a 1024-point WDL FFT. Window/sample precision, WDL resampler feed
size, phase refinement, magnitude accumulation, density calculation, and coarse
aggregation are all compatibility-sensitive.

At `source_rate <= 22050`, REAPER 7.79 creates `-'s'` layers whose payload codes
are zero; strict-WDL reproduces this as a version-specific compatibility quirk.

Near 22,051 Hz the exact WDL resampler feed granularity recovered by differential
sweep is:

```text
interleaved source buffer = 2048 samples
block_frames = max(1, 2048 / channels)
```

Upstream `WDL_fft_init()` has process-global first-use state; the strict bridge
uses `std::call_once` to make concurrent first use deterministic.

For non-Nyquist dominant bin `k`, REAPER's phase-refined frequency uses current
double precision phase and previous float32 complex values. Coarser `-'s'`
levels aggregate directly from the fine level. Density is averaged with floor;
frequency is selected from the fine record maximizing
`density * (32768 - frequency_hz)`.

Validation totals include **16,300 / 16,300** independent fine codes,
**96,222 / 96,222** all-mipmap codes, and **3,219 / 3,219** coarse aggregation
points.

# `-'g'` spectrogram generation

This section records the implementation recovered for PR #6 and locked against
the pinned REAPER 7.79 oracle.

## On-disk frame layout — Oracle + validated implementation

A logical channel/time frame contains:

```text
128 bins * 12 bits = 1536 bits = 192 bytes = 48 u32 words
```

The layer header uses token `-103`. Crucially, its `peak_count` is the number of
**u32 words per channel**, not the number of logical spectrogram frames.
Therefore:

```text
time_frames = peak_count / 48
payload_bytes = peak_count * channels * 4
```

Payload ordering is time-major, channel-inner.

Two 12-bit codes `a` and `b` are packed into three bytes:

```text
[a >> 4,
 ((a & 0x0f) << 4) | (b & 0x0f),
 b >> 4]
```

The decoder requires exactly 192 bytes for one channel frame; the encoder
rejects codes above `0x0fff`. Silence produces all-zero `-'g'` payload.

## FFT bin mapping — Oracle

The analysis FFT size is **256**. Stored bins are real-FFT bins **1 through
128**; DC bin 0 is omitted and Nyquist is retained. For example, at 48 kHz an
exact 6000 Hz tone lands in stored bin index 31 (FFT bin 32).

## Blackman-Harris window — Disassembly + Oracle

The recovered four-term coefficients are:

```text
A0 = 0.35875
A1 = 0.48829
A2 = 0.14128
A3 = 0.01168
```

REAPER 7.79 builds the window through a mixed f64/f32 sequence: phase and cosine
operations plus the unnormalized sum are double precision; each raw coefficient
is stored as float32; `1/sum` is converted to float32; normalization is a
float32 multiply. Phase advances by repeated addition rather than recomputation
from the sample index. This ordering is visible in the pinned binary and matters
for coherent tones.

PCM16 is first scaled by the float32 constant `1/32768`, multiplied by the
float32 window in float32, then promoted to f64 for the FFT input.

For IEEE float32 WAVE media, the validated RPKL `-'g'` path uses the source f32
sample directly (no PCM16 normalization), multiplies it by the same f32 window
in f32, then promotes the product to f64 for the FFT. This direct path is locked
by the 128-case live oracle below rather than inferred from file-format naming.

## Scheduler and window placement — Oracle

The recovered base-window shift for fine division `d` is:

```text
if d >= 256:
    shift = d - 256
else:
    shift = (d - 256) / 2   # signed integer division, truncating toward zero
```

Consequences:

- overlapping windows (`d < 256`) are centered around the division boundary;
- at and above one FFT width, REAPER right-aligns the 256-sample window to the
  base boundary;
- default 96 kHz / 300 pps has `d=320`, therefore `shift=+64`: the first full
  window starts at sample 64 and ends at sample 320;
- default 48 kHz / 300 pps has `d=160`, therefore `shift=-48`.

The 48 kHz leading edge exposed another non-obvious rule: REAPER does **not**
pretend unavailable negative-time samples are zeros under a fixed 256-point
window. The first analysis has only 208 real samples, so those samples receive a
new symmetric **208-point** Blackman-Harris window, normalized with the same
mixed-precision path, and the result is then zero-padded to the 256-point FFT.
The same resize-to-available rule covers lower-rate leading edges.

These rules are locked by explicit scheduler tests at the exact transition
samples and by live cases around fine divisions 255/256/257.

## FFT scaling, power, and quantizer — Disassembly + Oracle

The WDL real-FFT bridge returns the recovered raw scale, which is 2x a
conventional unscaled real DFT representation for the relevant path. The
portable FFT explicitly multiplies its output by 2 to match it.

REAPER quantizes **squared magnitude directly**, with no `sqrt`/`hypot` step:

```text
power = re*re + im*im
```

Constants recovered and validated by exact-bin amplitude sweeps:

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

Non-finite/non-positive power maps to 0; power at or above 1 maps to 4095.
Operating directly on power avoids an extra square-root rounding that changes
low sidelobes.

## Fine and coarse aggregation — Oracle

The 256-point analyses first produce already-quantized 12-bit base frames. The
first stored `-'g'` layer averages groups of those **quantized codes** according
to the ratio between the first two positive waveform divisions, using integer
floor division per bin. A final partial group is included at this first stored
level when the scheduler produces it.

Higher `-'g'` mipmaps then average complete groups of the immediately preceding
stored spectrogram frames according to the next nested division ratio. Partial
coarse groups are not emitted.

Spectrogram generation therefore requires positive divisions to be nested
integer multiples.

## Validation and stress — Validated implementation

The permanent pinned-REAPER PCM16 stress corpus contains **122 WAVE cases**.
Every source is built by a fresh REAPER 7.79 process. The strict-WDL test checks
headers, all non-`g` payloads, decoded bins, packed `-'g'` bytes, and finally the
whole RPKN byte stream:

```text
122 / 122 complete PCM16 spectrogram mode-3 files byte-identical
```

The portable/default FFT implementation is checked against the same 122 cases
for exact `-'g'` output.

A separate permanent IEEE float32/RPKL gate contains **128 adversarial WAVE
cases**, again using one fresh REAPER process per source. It deliberately checks
the spectrogram surface independently of unrelated RPKL waveform rounding:

```text
128 / 128 float32/RPKL cases: decoded -'g' frames exact
128 / 128 float32/RPKL cases: packed -'g' payload bytes exact
0 packed-payload failures
```

The float32 corpus spans 8 kHz through 192 kHz, 1-8 channels,
`peakcachegenrs` 100/150/300/500/1000 and neighboring branch values,
76,799/76,800/76,801 Hz, fine divisions around 255/256/257, scheduler
boundaries, long multichannel inputs, silence/DC, finite values above ±1.0,
very small finite values, exact-bin and off-bin tones, chirps, ramps, steps,
impulses, deterministic noise, sparse lanes, and deterministic randomized
cases.

The compatibility corpus is finite-valued. NaN/Inf exact REAPER behavior is not
inferred from it. libreapeaks sanitizes non-finite samples to zero for `-'g'`
analysis as a safety policy so malformed float media cannot poison FFT output or
panic generation; finite subnormals are accepted.

Independent property/adversarial tests additionally exhaust 12-bit packing,
round-trip arbitrary 192-byte frames, reject truncations and malformed counts,
probe 255/256 channel limits, exercise fine divisions 254..258, compare
multichannel output with independent mono lanes, and stress deterministic
parallel generation under normal/strict-WDL/sanitizer builds.

# `-'r'` loudness generation

REAPER 7.79 mode-3 stores two little-endian f32 values per time record/channel:

```text
f32 momentary_energy
f32 short_term_energy
```

The `-'r'` header `peak_count` is the number of f32 values per channel and is
twice the logical record count.

The byte-exact implementation uses libebur128-style K-weighting with the two
biquads convolved into one fourth-order Direct Form II filter. Energy blocks are
`max(1, floor(sample_rate/40))` frames. Momentary and short-term rings contain
16 and 120 blocks. The observable update order is:

```text
old = ring[next]
sum = (sum + new_energy) - old
ring[next] = new_energy
```

An incomplete final 25 ms block is not inserted at EOF. Normalization uses
`round(sample_rate/10) * 4` and `* 30` frame counts. Base loudness records follow
the second positive waveform division; coarser levels average complete groups.

Algebraically equivalent filter/ring rewrites can change the final f32 bytes and
are therefore not assumed compatible.

# FFmpeg decoder-path provenance

`GetMediaSourceType(...)=VIDEO` alone was not accepted as proof of FFmpeg use.
A dedicated Linux runtime trace observed REAPER's `reaper_video.so` calling
FFmpeg functions including:

```text
avio_alloc_context
avformat_open_input
av_read_frame
avcodec_send_packet
avcodec_receive_frame
```

for the tested ALAC/M4A source. The WAVE control recorded zero calls to those
monitored functions. This is evidence for that REAPER 7.79 Linux configuration,
not a universal codec/platform claim.

# Central-cache path policy

Byte compatibility and path compatibility are separate. REAPER may place caches
beside the source, in a subdirectory, or in alternate storage. Exact central
filenames must not be guessed from `altpeakspath`.

The canonical application-layer resolver delegates to the public REAPER API:

```text
GetPeakFileNameEx(source, ..., forWrite)
```

Repository helpers read the relevant `reaper.ini` values, can launch a
short-lived REAPER process to query paths, persist answers in a versioned cache
map, and reject malformed/unknown map formats instead of guessing. See
[`REAPER_CENTRAL_CACHE.md`](REAPER_CENTRAL_CACHE.md).

# Current validation totals

Representative permanent totals are recorded in
[`validation-summary.json`](validation-summary.json):

```text
FFmpeg/ALAC mode-3:                 8 / 8 whole files exact
adversarial mode-3:               16 / 16 whole files exact
PCM16 spectrogram strict-WDL:    122 / 122 whole files exact
float32/RPKL -'g':               128 / 128 payload cases exact
-'s' fresh-process primary:   10,112 / 10,112 codes exact
-'s' independent fine total:  16,300 / 16,300 codes exact
-'s' all-mipmap:              96,222 / 96,222 codes exact
waveform primary:            122,516 / 122,516 buckets exact
```

# Intentionally unsupported / unproven areas

The major RPKN PCM16 mode-3 waveform, `-'s'`, `-'g'`, and `-'r'` algorithms and
the finite float32/RPKL `-'g'` path are represented in the implementation and
live oracle suite. Remaining gaps include:

1. REAPER versions/platforms/architectures outside the named oracle matrices;
2. legacy `-'l'` payload layout;
3. RPKM compact waveform materialization;
4. exact REAPER NaN/Inf/subnormal policy for arbitrary float media;
5. whole-file RPKL identity across every finite waveform tie/rounding edge;
6. broader codec/decoder matrices;
7. mmap-backed parsing for extremely long files as a performance feature.

Future oracle work should continue to use fresh REAPER processes and should
label observed 7.79 behavior as such rather than silently promoting it to a
general DSP/file-format rule.
