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
5. adversarial path/RPKX/state-refusal cases;
6. a source-change-during-generation race/no-write case;
7. an independent-REAPER cross-process shared-cache race;
8. real-host native-vs-reference benchmarks;
9. completion manifests that recheck all required evidence from the same build.

The final completion steps intentionally duplicate important assertions. Removing
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

## 5. Adversarial RPKX/path/refusal suite

Run:

```bash
python examples/reaper_rpkx_extension/host_tests/host_adversarial.py
```

These cases deliberately attack assumptions that are easy to miss in a normal
workflow test:

- Unicode, spaces, and symbols in the real media/cache path;
- a non-page-aligned `4 MiB + 4097` opaque RPKX payload;
- fake `RPKN`, `RPKL`, and `RPKX` magic embedded inside that opaque payload;
- exact same-size preservation and intentional grow/shrink relocation;
- an explicitly stale RPKX `SourceStamp` that must remain stale rather than being
  silently rebound by the REAPER adapter;
- a truncated RPKX entry that points beyond container EOF and therefore must be
  rejected with the SHA-256 of the **entire pre-existing cache unchanged**.

A passing result proves that the preserving path treats RPKX payload bytes as
opaque application data rather than scanning them for convincing-looking cache
magic.

## 6. Source-change race gate

Run after the base suite has produced its native control:

```bash
python examples/reaper_rpkx_extension/host_tests/host_source_race.py
```

This gate uses the **normal distributable extension**, not the diagnostic build.
It starts a real import-triggered raw-PCM16 job on a roughly 25-minute source,
waits until the extension logs a real `BEGIN`, and then changes the source file's
mtime while decoding is still in progress.

The only acceptable outcome for that raced job is safe refusal:

- the mutation must really occur after `BEGIN`;
- the production trace must report `source changed during decode`;
- the raced job must not reach a successful `DONE reuse=0` commit;
- REAPER must observe `final_status=-1`;
- the SHA-256 of the **whole existing cache, including RPKX**, must be identical
  before and after the refused operation.

The harness intentionally performs no follow-up rebuild. A later rebuild after
the new source stamp is stable would be a different, valid job and must not mask
the atomicity result of the raced job.

## 7. Cross-process shared-cache race

Run after the base suite has produced the native waveform and spectrogram
controls:

```bash
python examples/reaper_rpkx_extension/host_tests/host_cross_process_race.py
python examples/reaper_rpkx_extension/host_tests/completion_cross_process.py
```

This is deliberately different from the source-change race. Two **independent
REAPER 7.79 processes** load the normal extension and wait at a filesystem
barrier. The harness then releases both processes together against the same
five-minute PCM16 media file and the same `.reapeaks` cache carrying a 16 MiB
RPKX suffix. One process requests a same-size waveform rebuild while the other
requests a growing spectrogram rebuild.

The persistent `<cache>.rpkx.lock` is the only mechanism allowed to serialize
those two process-level writers. The gate requires:

- both independent REAPER processes to load the normal, non-diagnostic extension;
- both to prove the same deterministic start barrier and complete a real
  `DONE reuse=0` generation/commit;
- no production `ERROR` record in either process;
- the cache observed immediately after the race to contain either the exact
  same-platform native waveform prefix or exact native spectrogram prefix — no
  mixed/torn third state is accepted;
- the entire 16 MiB RPKX suffix to remain byte-for-byte identical;
- a third clean REAPER process to rebuild the raced cache back to exact native
  waveform bytes while preserving that same suffix, proving there is no latent
  redo/WAL corruption left behind.

`completion_cross_process.py` independently rereads the report, checks the
current `GITHUB_SHA`, normal-plugin and REAPER identities, the >=16 MiB suffix,
all three process traces, and the exact post-race/final suffix hashes. The release
workflow runs this second-level proof before it is allowed to package a binary.

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

## Completion manifests

Run after the other real-host suites:

```bash
python examples/reaper_rpkx_extension/host_tests/completion.py
python examples/reaper_rpkx_extension/host_tests/completion_cross_process.py
```

`completion.py` checks that the base, extended, adversarial, source-race, and
benchmark reports are all present and that the environment-bearing reports belong
to the current `GITHUB_SHA` and same normal extension, diagnostic extension,
REAPER binary, and downloaded REAPER archive. It then independently rechecks the
required case inventory and high-value exactness/refusal invariants, including the
source-race whole-cache no-write proof.

`completion_cross_process.py` separately makes the new independent-process race a
release-blocking proof rather than trusting only the test script's own `passed`
field.

Successful runs write:

```text
host-results/completion.json
host-results/COMPLETION.md
host-results/cross-process-completion.json
```

## CI and release workflows

The repository keeps the example separate from the normal library test suite:

- `.github/workflows/reaper-plugin.yml` — bridge/example build and fault tests on
  Ubuntu, macOS, and Windows;
- `.github/workflows/reaper-host.yml` — real REAPER 7.79 base, extended,
  adversarial, source-race, independent-process race, benchmarks, and both
  completion proofs on the same OS matrix;
- `.github/workflows/release-reaper-rpkx-example.yml` — for the explicit
  `release: v0.1.0` main commit (or an existing Release tag), rebuilds and reruns
  the same real-host gates on all three targets before packaging the normal
  reference binaries. The v0.1.0 release path requires all three validated
  platform packages before creating/updating the Release.

The normal `.github/workflows/ci.yml` remains the library's primary CI and also
syntax-checks the example's Python host helpers so directory/refactoring errors
are caught cheaply.

## Interpreting failures

Keep these categories separate:

- **library exactness failure** — root strict-WDL/oracle tests changed;
- **example build failure** — the reference host adapter no longer builds;
- **host correctness failure** — real REAPER behavior, standard bytes, RPKX
  preservation, race/refusal, recovery, or lifecycle proof failed;
- **performance-only failure** — correctness passed but the strict native-speed
  policy was not met on that runner.

The reference extension should never weaken the first three categories just to
make the fourth green.
