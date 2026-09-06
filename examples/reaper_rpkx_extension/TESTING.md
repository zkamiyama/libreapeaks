# Testing the REAPER RPKX reference extension

The tests in this example verify a **host integration**, not the public API of the
root libreapeaks crate. Library correctness has its own normal/strict test suites;
these tests add evidence that the example behaves correctly inside a real REAPER
7.79 process.

## Test layers

The example is checked in increasing order of cost:

1. Rust bridge/store unit tests;
2. C++ extension build and load on each supported CI host;
3. ordinary real-REAPER acceptance cases;
4. extended profile/media-lifecycle cases;
5. real-host native-vs-reference benchmarks;
6. a completion manifest that rechecks required evidence from the same build.

The final completion step intentionally duplicates important assertions. Removing
or accidentally skipping a case must not turn the example green.

## 1. Bridge/store tests

From the repository root:

```bash
cargo test --release --manifest-path examples/reaper_rpkx_extension/Cargo.toml
```

These tests cover preservation and storage mechanics independently of REAPER,
including:

- same-size, grow, and shrink replacement;
- exact suffix preservation;
- stale source identity;
- malformed/unknown data refusal;
- read-only behavior;
- bounded streaming waveform geometry;
- fault injection and torn-transaction recovery.

The Rust bridge depends on the root crate's `strict-wdl` feature, so the generated
standard cache uses the same strict compatibility implementation as the rest of
the repository.

## 2. Build the real extension

The CI host workflow uses a pinned REAPER SDK checkout and builds both:

- the normal reference extension; and
- a separately hashed diagnostic extension with test fault hooks enabled.

After placing the pinned SDK at `.host-sdk`, the helper can reproduce the build:

```bash
python examples/reaper_rpkx_extension/host_tests/setup_host.py --build-only
```

Do not use the diagnostic binary as a distributable example artifact.

## 3. Real-host acceptance

The base suite launches fresh REAPER 7.79 processes and drives ordinary REAPER
actions rather than calling a private “build the cache now” shortcut:

```bash
python examples/reaper_rpkx_extension/host_tests/setup_host.py --install-only
python examples/reaper_rpkx_extension/host_tests/host_acceptance.py
```

On Linux CI the command is run under Xvfb because REAPER is a GUI application.
macOS/Windows startup helpers only handle the publisher's normal evaluation/audio
setup dialogs on disposable workers; unknown dialogs are not blindly dismissed.

Base cases include:

- native waveform, stale, float32, and spectrogram controls;
- ordinary import/missing-cache generation;
- `Peaks: Rebuild all peaks`;
- `Peaks: Rebuild peaks for selected items`;
- stale project/import media;
- spectrogram profile generation;
- reverse and offline/online source transitions;
- `peakcachegenmode` 0/1/2/3;
- injected post-generation failures.

The suite separately proves that an unwrapped native `PCM_source*` is safely
rejected by the example's public diagnostic/status API instead of being treated
as one of the wrapper objects.

## 4. Extended workflows

Run:

```bash
python examples/reaper_rpkx_extension/host_tests/host_extended.py
```

This suite adds:

- spectral and loudness native controls;
- spectral/loudness generation with RPKX relocation;
- spectrogram -> normal shrink;
- offline -> rebuild -> online regeneration;
- reverse failure atomicity;
- a roughly 25-minute PCM16 source proving the streaming path;
- Glue and Render media creation followed by an RPKX-bearing ordinary rebuild.

### Record scope

A newly recorded file has no pre-existing RPKX suffix, so live Record creation is
not itself a preservation test. Headless CI would also make success depend on an
audio driver/device rather than on this extension's cache logic.

For that reason live Record transport is not a mandatory completion gate. Once a
recorded PCM file exists and its cache contains RPKX, regeneration uses the same
ordinary PCM16/float32 preservation path that the base/long rebuild cases test.

## Exact-byte contract

A positive preservation case does not pass merely because REAPER can display a
result. Where a same-platform native control exists, the suite requires the
reference extension's **standard REAPEAKS region to be byte-identical** to native
REAPER 7.79 output.

For an existing RPKX suffix it additionally requires:

- exact suffix bytes before/after;
- exact suffix SHA-256;
- `tail_moved=0` for same-size standard replacement;
- positive relocation for an intentional grow/shrink case.

A log line such as `DONE reuse=1` proves only cache reuse. Tests that claim real
regeneration require a generated job and `DONE reuse=0`.

## Negative controls

The diagnostic extension can fail immediately after the standard generator
returns. The test then requires no write to have escaped:

- if the cache did not exist, it must remain absent;
- if a cache already existed, the SHA-256 of the **entire cache** must remain
  unchanged.

This catches destructive native fallback as well as damage limited to the RPKX
suffix.

## Performance benchmark

Run:

```bash
python examples/reaper_rpkx_extension/host_tests/benchmark.py
```

The benchmark uses fresh REAPER processes and a 10-second, 48 kHz stereo PCM16
fixture. It compares native REAPER with the example for waveform and spectrogram
profiles and uses 0, 16, and 64 MiB RPKX suffixes.

Pre-existing benchmark caches are flushed before the measured action. This keeps
benchmark setup I/O out of the plugin's durability time; the measured transaction
still has to perform its own WAL/sync work.

The report distinguishes:

- **peak-ready time** — when REAPER's peak build has completed;
- **durable-ready time** — peak-ready plus any required preserving WAL/fsync
  completion.

Each group is a shuffled three-run median. Current policy requires the reference
extension's 0/16/64 MiB peak-ready median to be strictly faster than the
same-host native median for both waveform and spectrogram, while independently
checking durability and RPKX-size regression budgets.

That performance policy is deliberately strict and may expose host variance. A
performance failure does **not** relax byte correctness: the benchmark first
requires exact standard bytes, untouched RPKX, the expected raw PCM16 path, and
the expected redo/sync path.

Do not document one historical benchmark run as a permanent speed guarantee.
The workflow result for the current commit is the source of truth.

## Completion manifest

Run after the other real-host suites:

```bash
python examples/reaper_rpkx_extension/host_tests/completion.py
```

`completion.py` checks that `report.json`, `extended-report.json`, and
`benchmark.json` all belong to the current `GITHUB_SHA` and to the same normal
extension, diagnostic extension, REAPER binary, and downloaded REAPER archive.
It also rechecks required case inventory and high-value invariants.

A successful run writes:

```text
host-results/completion.json
host-results/COMPLETION.md
```

## CI workflows

The repository keeps the example separate from the normal library test suite:

- `.github/workflows/reaper-plugin.yml` — bridge/example build and fault tests on
  Ubuntu, macOS, and Windows;
- `.github/workflows/reaper-host.yml` — real REAPER 7.79 acceptance, extended
  workflows, benchmarks, and completion manifest on the same OS matrix.

The normal `.github/workflows/ci.yml` remains the library's primary CI and also
syntax-checks the example's Python host helpers so directory/refactoring errors
are caught cheaply.

## Interpreting failures

Keep these categories separate:

- **library exactness failure** — root strict-WDL/oracle tests changed;
- **example build failure** — the reference host adapter no longer builds;
- **host correctness failure** — real REAPER behavior, standard bytes, RPKX
  preservation, recovery, or lifecycle proof failed;
- **performance-only failure** — correctness passed but the strict native-speed
  policy was not met on that runner.

The reference extension should never weaken the first three categories just to
make the fourth green.
