# REAPER RPKX extension reference implementation

> **Example code, not part of the libreapeaks library API.**
>
> This directory contains an experimental REAPER extension that demonstrates one
> way to integrate `libreapeaks` into REAPER while preserving an RPKX payload
> appended to a `.reapeaks` file. It is intentionally kept under `examples/` so
> applications can copy, adapt, or replace the integration without making this
> extension part of libreapeaks' supported public API.

The example is currently pinned to **REAPER 7.79**. The extension refuses to load
on other REAPER versions rather than silently extending an unvalidated host.

## Want to install the extension?

If you want to **use** a prebuilt extension rather than study or build the
reference implementation, start with [`USER_GUIDE.md`](USER_GUIDE.md). It covers
release-asset selection, installation into REAPER's `UserPlugins`, normal use,
verification, uninstalling, platform limitations, and troubleshooting/safety
notes.

Verified release packages are produced from the normal extension build only. The
CI-only diagnostic binary with fault-injection hooks is not a distributable
plugin and is deliberately excluded from release packages.

## What the example demonstrates

The reference extension wraps supported REAPER `PCM_source` instances and lets
ordinary REAPER peak-cache operations regenerate the standard REAPEAKS region
without losing an existing RPKX suffix. The implementation demonstrates:

- byte-for-byte preservation of an existing RPKX suffix;
- exact same-platform REAPER 7.79 standard-cache bytes for the validated paths;
- conservative source-stamp validation instead of preserving stale metadata;
- crash-safe redo/WAL replacement and recovery;
- same-size rebuilds that overwrite the standard region without moving a large
  RPKX payload;
- safe refusal for unknown suffixes and unsupported/unwrapped source types;
- a raw PCM16 WAVE fast path and bounded streaming waveform generation;
- real-host regression tests for ordinary REAPER actions, including negative
  controls and performance checks.

This is a reference integration, not a promise that every REAPER version, codec,
third-party `PCM_source` wrapper, preference combination, or host environment is
supported.

## Directory layout

```text
examples/reaper_rpkx_extension/
├── README.md                 # scope, build and developer usage
├── USER_GUIDE.md             # install/use prebuilt release binaries
├── DESIGN.md                 # integration boundaries and safety invariants
├── TESTING.md                # real-REAPER test/benchmark contract
├── CMakeLists.txt            # C++ REAPER extension build
├── Cargo.toml                # Rust bridge crate; depends on libreapeaks
├── bridge.h                  # C ABI between the C++ host adapter and Rust
├── plugin.cpp                # REAPER PCM_source provider/wrapper
├── plugin_job.h              # generation jobs and host-facing scheduling
├── raw_pcm16_wave.h          # canonical PCM16 WAVE fast-path reader
├── windows_guard.h           # Windows guarded-clear handling
├── src/                      # Rust bridge/store implementation
│   ├── lib.rs
│   ├── read_guard.rs
│   ├── read_only.rs
│   ├── store.rs
│   └── stream_wave.rs
└── host_tests/               # disposable real-REAPER acceptance harness
    ├── setup_host.py
    ├── host_acceptance.py
    ├── host_actions.lua
    ├── host_extended.py
    ├── host_extended.lua
    ├── benchmark.py
    ├── benchmark.lua
    ├── completion.py
    ├── host_process.py
    └── macos_startup.applescript
```

The Rust bridge depends on the repository's root `libreapeaks` crate with the
`strict-wdl` feature. The REAPER-specific store, job scheduling, source wrapping,
and crash-safety policy stay inside this example rather than becoming core
library APIs.

## Build

Clone the repository with submodules because strict compatibility uses Cockos
WDL:

```bash
git clone --recurse-submodules https://github.com/zkamiyama/libreapeaks.git
cd libreapeaks
```

Build and test the Rust bridge first:

```bash
cargo test --release --manifest-path examples/reaper_rpkx_extension/Cargo.toml
cargo build --release --manifest-path examples/reaper_rpkx_extension/Cargo.toml
```

The C++ extension needs a REAPER SDK checkout and the Rust static bridge library.
The repository's host harness shows the exact CI build. With a pinned SDK at
`.host-sdk`, the equivalent CMake shape is:

```bash
cmake -S examples/reaper_rpkx_extension -B host-build \
  -DCMAKE_BUILD_TYPE=Release \
  -DREAPER_SDK="$PWD/.host-sdk" \
  -DWDL_ROOT="$PWD/third_party/WDL" \
  -DBRIDGE_LIBRARY="<path-to-rpkx_bridge-static-library>"
cmake --build host-build --config Release
```

The static bridge is normally
`examples/reaper_rpkx_extension/target/release/librpkx_bridge.a` on Unix-like
platforms and `examples/reaper_rpkx_extension/target/release/rpkx_bridge.lib` on
MSVC Windows.

For the repository's disposable integration build, after placing the pinned
REAPER SDK at `.host-sdk`, run:

```bash
python examples/reaper_rpkx_extension/host_tests/setup_host.py --build-only
```

`LRPK_ENABLE_TEST_HOOKS=ON` produces a diagnostic binary used only by negative
controls. Do not distribute that build.

## Manual installation of a local build

For normal users, prefer the packaged instructions in
[`USER_GUIDE.md`](USER_GUIDE.md).

For a locally built normal extension, copy the CMake output named `reaper_rpkx`
into REAPER's `UserPlugins` directory using the platform extension produced by
CMake (`.dll`, `.dylib`, or `.so`). On load, the example registers itself as:

```text
libreapeaks RPKX protection (experimental)
```

There is no user-facing configuration UI. The point of the example is to show a
transparent host integration: normal REAPER import/rebuild/display-profile
operations should use the wrapped source and preserve existing RPKX data.

## RPKX preservation scope

Preservation only applies when an RPKX suffix already exists. A newly recorded,
glued, or rendered media file has no pre-existing RPKX payload to preserve at
creation time. The relevant guarantee begins once that media's cache contains
RPKX and REAPER later regenerates the standard peak region.

The real-host suite therefore treats Glue/Render as a two-stage lifecycle:
create new media, then attach RPKX and prove an ordinary rebuild preserves it.
Live Record transport is not a headless correctness gate because recording also
depends on an available audio device; subsequent recorded-file peak regeneration
uses the same PCM preservation path demonstrated by the ordinary rebuild tests.

## Safety and architecture

Read [`DESIGN.md`](DESIGN.md) before adapting the example. In particular, do not
copy only the visible source wrapper while omitting source-stamp validation,
write-ahead recovery, read guards, or the Windows clear guard. Those pieces are
part of the preservation contract.

## Verification

[`TESTING.md`](TESTING.md) documents the bridge tests, real REAPER 7.79
acceptance suites, exact-byte controls, failure injection, long-source streaming,
and native-vs-reference benchmarks.

The host tests download/run REAPER and are intentionally separate from normal
`libreapeaks` library tests. A failure of this example's host integration should
not be interpreted as a change to the public Rust/Python/C library API, although
CI keeps the example buildable and its claimed integration behavior testable.

## Release binaries

`.github/workflows/release-reaper-rpkx-example.yml` builds the normal extension
for the validated Windows x86_64, Linux x86_64, and macOS arm64 targets when a
`v*` tag is released through that workflow. Before packaging, each target runs
the same real-REAPER 7.79 base, extended, benchmark, and completion gates used by
the host acceptance suite. Only a target that reaches the same-build completion
PASS is packaged.

The resulting archives contain the normal `reaper_rpkx` extension, this user
guide, license/third-party notices, and selected completion/benchmark evidence.
They do **not** contain the diagnostic extension. A `SHA256SUMS.txt` file is also
attached to the GitHub Release.

## Related library documentation

- [`../../docs/RPKX_SPEC.md`](../../docs/RPKX_SPEC.md) — RPKX v1 container/API.
- [`../../docs/RPKX_EOF_EXTENSIONS.md`](../../docs/RPKX_EOF_EXTENSIONS.md) —
  REAPER EOF-extension experiments behind the design.
- [`../../docs/SOURCE_STAMP.md`](../../docs/SOURCE_STAMP.md) — cache freshness
  and source identity.
- [`../../docs/COMPATIBILITY.md`](../../docs/COMPATIBILITY.md) — exactness scope
  of the libreapeaks library itself.

The library remains the reusable component. This directory is deliberately an
example of one host-specific integration built on top of it.
