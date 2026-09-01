# libreapeaks documentation

Start with the document that matches the question you are trying to answer.

## I want to know whether libreapeaks really matches REAPER

Read [`COMPATIBILITY.md`](COMPATIBILITY.md).

It is the compatibility contract: which REAPER version/platform is used as the
oracle, which complete-file cases are byte-identical, what waveform, `-'s'`
spectral, `-'g'` spectrogram, and `-'r'` loudness behavior is covered, which
native layer combinations REAPER actually emits, and which areas remain
unproven or unsupported.

Machine-readable validation metadata lives in
[`validation-summary.json`](validation-summary.json).

## I want the technical details of how REAPER's cache was reproduced

Read [`REVERSE_ENGINEERING.md`](REVERSE_ENGINEERING.md).

It records the oracle methodology and recovered behavior for:

- `.reapeaks` header/layer layout;
- RPKN/RPKL waveform quantization;
- `peakcachegenrs` division selection;
- WDL resampling/FFT and `-'s'` spectral generation;
- `-'g'` spectrogram framing, 12-bit packing, Blackman-Harris window placement,
  power quantization, and fine/coarse aggregation;
- `-'r'` loudness filtering, cadence, floating-point operation order, and EOF
  scheduling;
- FFmpeg decoder-path provenance;
- REAPER central-cache path policy.

The `-'g'` implementation is independently stress-tested in both portable and
`strict-wdl` builds. The pinned REAPER 7.79 stress gate currently covers 122
PCM16 cases; strict-WDL output is complete-file byte-identical for all 122, and
the portable/default FFT path is checked for exact `-'g'` output on the same
matrix.

A separate fresh-process oracle sweeps 71 REAPER `showpeaks` configurations and
locks the three native cache shapes: waveform-only, waveform + `-'s'` + `-'r'`,
and waveform + `-'s'` + `-'g'` + `-'r'`. No individual `s`/`g`/`r` cache was
observed.

## I want to share a cache path with REAPER

Read [`REAPER_CENTRAL_CACHE.md`](REAPER_CENTRAL_CACHE.md).

Byte compatibility and path compatibility are separate: generating the same
cache bytes does not by itself mean an application chose the path REAPER will
use. The document explains `reaper.ini`, `GetPeakFileNameEx`, persisted cache
maps, and current demo integration.

## I am building a waveform UI

Read [`GUI_WAVEFORM.md`](GUI_WAVEFORM.md).

It documents the native/lazy waveform pyramid, 4096-peak tile identity, RGBA8
waveform packing, `-'s'` spectral-code textures, and the corresponding Python/C
APIs. The runnable examples are documented in
[`../examples/PLAYER_DEMOS.md`](../examples/PLAYER_DEMOS.md).

## I need exact points at sample zoom

Read [`SOURCE_PCM_LOD.md`](SOURCE_PCM_LOD.md).

It explains why individual samples cannot be reconstructed from `.reapeaks`,
when the reference players hand off to source PCM, how decoded memory and
concurrency remain bounded, why a playback buffer is not automatically a
random-access source cache, and how to consume range-decode notifications and
the shared exact-line/dot draw plan.

## I am integrating from C or C++

Read [`C_ABI.md`](C_ABI.md), then use
[`../include/reapeaks.h`](../include/reapeaks.h) as the source of truth for the
actual exported ABI.

Rust, Python, and C now expose the same REAPER-oriented mode vocabulary:
waveform, spectral (`wave + s + r`), and PCM16 spectrogram (`wave + s + g + r`).
Float32 spectrogram generation remains unsupported until an exact float32
`-'g'` implementation exists.

## Source-of-truth order

When documentation and code disagree, use this order:

1. current public source/API definitions (`src/`, `include/reapeaks.h`,
   `src/python.rs`);
2. permanent live oracle workflows and their tests;
3. [`COMPATIBILITY.md`](COMPATIBILITY.md) for the tested compatibility claim;
4. the deeper explanatory documents in this directory;
5. historical oracle reports/workflows, which may describe intermediate
   reverse-engineering states rather than the current implementation.

Compatibility claims should always name the validated REAPER version and scope.
Do not promote an observed REAPER 7.79 implementation quirk into a universal
file-format or DSP rule without separate evidence.
