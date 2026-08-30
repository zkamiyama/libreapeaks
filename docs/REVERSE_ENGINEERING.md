# REAPER 7.79 `.ReaPeaks` reverse-engineering notes

Date: 2026-08-30  
Primary oracle: REAPER 7.79 x86_64 Linux  
Scope: waveform + spectral peak cache compatibility

This document separates public format facts from behavior measured against a
live REAPER executable.

## Evidence labels

- **Official** — documented by Cockos.
- **Oracle** — directly measured from REAPER 7.79-generated files.
- **Disassembly** — recovered from the REAPER 7.79 x86_64 executable and checked
  against differential probes.
- **Pending strict-WDL** — math is recovered but byte-exact reproduction still
  depends on matching Cockos' numerical implementation.

## Public sources

- Cockos `.ReaPeaks` format:
  https://www.reaper.fm/sdk/reapeaks.txt
- ReaScript API:
  https://www.reaper.fm/sdk/reascript/reascripthelp.html
- Cockos WDL:
  https://github.com/justinfrankel/WDL

## Live oracle harness

REAPER was run under Xvfb. A Lua ReaScript creates a `PCM_source` for each media
file and drives:

```text
PCM_Source_BuildPeaks(source, 0)  # begin
PCM_Source_BuildPeaks(source, 1)  # run, repeated
PCM_Source_BuildPeaks(source, 2)  # finish
```

`GetPeakFileNameEx` is then used to locate the generated file. This avoids GUI
timing and makes fixture production deterministic except for the source mtime
field.

The oracle configuration used:

```text
peakcachegenmode=3
peakcachegenrs=300
```

At 44.1 kHz this yields positive divisions `147, 2205, 44100`; at 48 kHz,
`160, 2400, 48000`. The fine rate is a user preference, not a format constant.

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

# RPKN waveform writer

## PCM16 exhaustive map — Oracle

A 44.1 kHz mono WAV was built with 65,536 fine buckets. Each bucket contains 147
copies of exactly one PCM16 value, covering every value from -32768 to 32767.
REAPER's stored `(max,min)` pair for every bucket was read back.

Result: all negative PCM16 values are unchanged; non-negative values use:

```text
stored = round_half_up(v * 32767 / 32768)
```

Equivalent normalized mapping:

```text
if x >= 0: round_half_up(x * 32767)
else:      -round_half_up(-x * 32768)
```

Across the full waveform corpus, including the exhaustive map, 122,516 compared
wave buckets were byte-exact with this rule.

Notable boundaries:

```text
-32768 -> -32768
-1     -> -1
0      -> 0
16384  -> 16384
16385  -> 16384
32767  -> 32766
```

## 24-bit source probe — Oracle

A 22 MB PCM24 WAV was constructed with 50,000 constant fine buckets chosen from
arbitrary 24-bit values. Treating the decoder output as `x = int24 / 8388608`
and applying the asymmetric normalized mapping above matched **50,000/50,000**
buckets.

Therefore this is not merely an int16 accident; it describes the RPKN float-to-
peak quantizer used on integer media.

# RPKL waveform writer

## Value encoder — Official + Oracle

Cockos documents RPKL's linear region and logarithmic over-range formula. Live
REAPER was tested with 43,857 finite float values plus powers/high-range probes
up through +/-512.

Measured encoder:

```text
m = abs(x)

if m <= 1:
    code_mag = round_half_up(m * 24576)
else:
    code_mag = round_half_up(24576 + 1024*log2(m))

positive: clamp code_mag to 32767
negative: clamp code_mag to 32768, then negate
```

This yields:

```text
+1   ->  24576
+2   ->  25600
+8   ->  27648
+128 ->  31744
+256 ->  32767  (positive saturation)
-256 -> -32768  (exact negative endpoint)
```

The public text describes the logarithmic region as reaching 8.0, but the
published formula and REAPER behavior extend to about 256. The implementation
follows the formula/oracle.

## Bucket initialization — Oracle

RPKL waveform extrema are initialized to:

```text
max = -1.0
min = +1.0
```

before scanning samples.

Therefore a constant +2.0 bucket stores:

```text
max = encode(+2.0) = 25600
min = encode(+1.0) = 24576
```

and a constant -2.0 bucket stores:

```text
max = encode(-1.0) = -24576
min = encode(-2.0) = -25600
```

The 43,857-value map matched this exact bucket rule for every tested value.

# Format-dependent behavior

The same deterministic 48 kHz stereo signal was encoded into several formats
and opened by REAPER 7.79.

```text
WAV PCM16    RPKN
WAV PCM24    RPKN
WAV PCM32    RPKN
FLAC16       RPKN
FLAC24       RPKN
WAV float32  RPKL
MP3          RPKL
Vorbis       RPKL
Opus         RPKL
```

For WAV16/WAV24/WAV32/FLAC16/FLAC24, **all wave, spectral and loudness payloads
were byte-for-byte identical** when the decoded signal was identical.

For float32 WAV produced from the same signal, spectral and loudness payloads
were identical; only the wave payload changed to RPKL representation.

This strongly supports designing the library around decoded samples plus an
explicit output peak encoding rather than guessing RPKN/RPKL from whether an
application happens to hold samples in `f32`.

# Spectral generation

## Peak count — Oracle

For each mirrored positive wave division `div`, tested files obey:

```text
spectral_peak_count = floor((source_frames - 1024) / div)
```

for sources longer than 1024 frames.

## Analysis rate / window — Disassembly + Oracle

The recovered path uses an internal stream near 22,050 Hz and a 1024-point FFT.
For the common fine level, the source division is converted to an analysis hop:

```text
hop = source_division * 22050 / source_sample_rate
```

The rolling input and scheduling recovered from the executable match the
existing differential model.

Window preparation has an important precision detail:

1. analysis-ring sample is `float`;
2. Hann coefficient is `float`;
3. multiplication is performed as float32;
4. the product is converted to double for the FFT input accumulator.

## FFT precision — Disassembly

The REAPER 7.79 path operates on a double FFT buffer. `strict-wdl` therefore
builds Cockos WDL with:

```text
WDL_FFT_REALSIZE=8
```

instead of WDL's default 4-byte `WDL_FFT_REAL`.

## Magnitudes — Disassembly

For a real 1024 FFT:

```text
mag[0]   = abs(DC)
mag[512] = abs(Nyquist)
mag[k]   = sqrt(re[k]^2 + im[k]^2), 1 <= k <= 511
```

The total magnitude is accumulated in double precision.

A second copy of every magnitude is rounded to float32. That float32 array is
used by the density second-moment calculation.

Dominant-bin selection begins with Nyquist as the candidate and then scans
1..511 using strict `>` comparison. DC is not a dominant-frequency candidate,
but it is included in density.

## Frequency refinement — Disassembly

For non-Nyquist dominant bin `k`, REAPER compares the current double phase with
the previous spectrum stored as float32 complex values.

Conceptually:

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

Then clamp to the 15-bit frequency field.

## Density — Disassembly

Constants visible in the target routine include `16383`, `4`, `262144=512^2`
and `1/1024`. The recovered expression is:

```text
total = sum(mag_f64[k], k=0..512)
spread = sum(float32(mag[k]) * (k-best_bin)^2, k=0..512)

density = trunc(
    0.5 + 16383 * (1 - 4*spread/(total*512^2))
)
```

clamped to `[0,16383]`.

## Fine-level reconstruction accuracy before WDL substitution — Oracle

Across 131 deterministic mono test files and 34,127 fine spectral points:

```text
frequency exact: 34,040 / 34,127 = 99.745%
density exact:   31,848 / 34,127 = 93.32%
full code exact: 31,776 / 34,127 = 93.11%
```

The mathematical reconstruction used a conventional FFT and a handwritten
WDL-resampler-equivalent model. Errors were concentrated in +/-1 boundaries,
impulses and very-low-energy cases. Since the disassembled formulas already
account for the remaining branches, the working hypothesis is that most
residual mismatch is numerical implementation order in Cockos WDL FFT/resampler.

The repository's `strict-wdl` feature substitutes the actual WDL routines. Its
CI golden fixtures are intentionally chosen to include a fractional-bin tone,
a tone+noise density case, and an impulse boundary.

## Coarser spectral levels — Oracle

Tested REAPER 7.79 files show that coarser spectral levels are aggregated
**directly from the fine spectral level**, not recursively from the immediately
preceding level.

For an output group:

```text
density_out = floor(mean(fine_density))
```

Frequency is taken from the fine peak maximizing:

```text
density * (32768 - frequency_hz)
```

This rule matched all 3,219 tested mid/coarse aggregate points in the research
set.

# GUI implications

The positive wave mipmaps are already a useful storage pyramid, but their rates
are deliberately sparse. For smooth arbitrary zoom levels, libreapeaks adds
metadata-only geometric display levels and derives visible ranges from the fine
native layer.

This avoids creating another persistent waveform cache while also avoiding an
eager in-memory copy of every derived level.

# Open items

1. Confirm `strict-wdl` exactness across the full 131-file spectral corpus on CI.
2. Expand RPKL tests to NaN/Inf/subnormal audio if those inputs matter.
3. Reverse-engineer/write spectrogram (`-'g'`) bins if required.
4. Loudness (`-'r'`) has an observed layout discrepancy: REAPER 7.79 fixtures in
   this lab occupy 4 bytes/channel/sample even though the public text describes
   two 32-bit floats. It remains opaque in the parser until resolved.
5. Add mmap-backed parsing for extremely long media if full peak vectors become
   a bottleneck; the GUI tile API is already designed so this can be introduced
   without changing frontend cache keys.
