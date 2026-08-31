# libreapeaks documentation

Start with the document that matches the question you are trying to answer.

## I want to know whether libreapeaks really matches REAPER

Read [`COMPATIBILITY.md`](COMPATIBILITY.md).

It is the concise compatibility contract: which REAPER version/platform is used
as the oracle, which complete-file cases are byte-identical, what waveform,
spectral and loudness behavior is covered, and which areas are still unproven or
unsupported.

Machine-readable validation metadata lives in
[`validation-summary.json`](validation-summary.json).

## I want the technical details of how REAPER's cache was reproduced

Read [`REVERSE_ENGINEERING.md`](REVERSE_ENGINEERING.md).

It contains the oracle methodology and recovered behavior for:

- `.reapeaks` header/layer layout;
- RPKN/RPKL waveform quantization;
- `peakcachegenrs` division selection;
- WDL resampling/FFT and `-'s'` spectral generation;
- `-'r'` loudness filtering, block cadence, floating-point operation order, and
  EOF scheduling;
- the FFmpeg decoder-path provenance investigation;
- REAPER central-cache path policy.

## I want to share a cache path with REAPER

Read [`REAPER_CENTRAL_CACHE.md`](REAPER_CENTRAL_CACHE.md).

The key distinction is between:

- byte compatibility — generating the same cache content;
- path compatibility — writing it where REAPER expects it.

The document explains `reaper.ini`, `GetPeakFileNameEx`, persisted cache maps,
and the current difference between the reusable higher-level policy helper and
the older cache-mode names still exposed by the runnable demo CLIs.

## I am building a waveform UI

Read [`GUI_WAVEFORM.md`](GUI_WAVEFORM.md).

It documents the native/lazy waveform pyramid, 4096-peak tile identity, RGBA8
waveform packing, spectral-code textures, and the corresponding Python/C APIs.

The runnable examples are documented in
[`../examples/PLAYER_DEMOS.md`](../examples/PLAYER_DEMOS.md).

## I am integrating from C or C++

Read [`C_ABI.md`](C_ABI.md), then use
[`../include/reapeaks.h`](../include/reapeaks.h) as the source of truth for the
actual exported ABI.

The C document also calls out an important current limitation: C generation
exposes waveform plus optional spectral layers, while the complete recovered
mode-3 loudness writer is currently a Rust API.

## Source-of-truth order

When documentation and code ever appear to disagree, use this order:

1. current public source/API definitions (`src/`, `include/reapeaks.h`,
   `src/python.rs`);
2. permanent live oracle workflows and their tests;
3. [`COMPATIBILITY.md`](COMPATIBILITY.md) for the tested compatibility claim;
4. the deeper explanatory documents in this directory;
5. historical oracle reports/workflows, which may describe intermediate
   reverse-engineering states rather than the current implementation.

Compatibility claims should always name the validated REAPER version and scope.
Do not promote an observed REAPER 7.79 implementation quirk into a general file
format or DSP rule without separate evidence.
