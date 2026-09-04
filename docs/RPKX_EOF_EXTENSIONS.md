# RPKX EOF extension research

This document records the empirical basis for appending application-owned bytes
after REAPER's standard `.reapeaks` region. It is a research/evidence document,
not the production wire-format specification.

For the current production container, read [`RPKX_SPEC.md`](RPKX_SPEC.md).
RPKX v1 is now frozen in this repository as a packed directory-first layout:

```text
[standard REAPER region]
[RPKX 32-byte header]
[all 48-byte directory entries]
[all payloads, tightly packed]
[optional unrelated EOF suffix]
```

Earlier 12-byte and interleaved RPKX layouts discussed below were experiments
used to establish coexistence behavior. They are not accepted as current RPKX
v1.

## Scope

The live evidence in this document is pinned to:

- REAPER 7.79 Linux x86_64;
- Ubuntu 24.04 under Xvfb;
- deterministic 48 kHz mono PCM16 WAV fixtures;
- fresh REAPER process where the oracle requires process isolation.

Do not generalize these observations to other REAPER versions/platforms without
separate live testing.

The permanent workflow is:

`.github/workflows/reaper-cache-extension-oracle.yml`

## Why EOF extension rather than a custom REAPER layer

Cockos' standard `.reapeaks` layout is:

```text
fixed header (18 bytes)
all layer headers (8 bytes * layer_count)
all standard layer payloads
EOF
```

For known modern RPKM/RPKN/RPKL layers, libreapeaks can compute the standard end
from the layer kind, count, channel count, and container version. The standard
format does not include a generic payload length for an unknown layer token.
Consequently an unknown custom layer cannot be safely skipped by a reader that
does not understand that token.

An EOF extension avoids modifying the standard layer table and therefore has a
much smaller compatibility surface.

The legacy terminal `-'l'` loudness layout remains ambiguous to libreapeaks, so
that historical form cannot currently be split safely from an EOF extension.

## Original acceptance experiment

The original deterministic REAPER cache was 5,518 bytes. The early oracle used
a deliberately simple experimental suffix:

```text
4 bytes  "RPKX"
4 bytes  version = 1
4 bytes  payload length
N bytes  payload
```

A tempo/chord JSON fixture made the complete file 5,756 bytes. That envelope was
only an oracle fixture and was never the final RPKX specification.

The extension matrix also tested single trailing bytes, 16 zero bytes, a 4 KiB
deterministic arbitrary suffix, and several deliberately malformed standard
layer-table mutations.

For the pure EOF additions REAPER 7.79 returned
`PCM_Source_BuildPeaks(src, 0) == 0` and preserved the cache bytes. This showed
that, on this tested path, physical EOF does not need to equal REAPER's computed
standard-layer end.

### `BuildPeaks(..., 0)` is only a reuse signal

The wider mutation matrix is important because many malformed standard-layer
mutations also returned `BEGIN=0`. The decisive historical matrix classified
27/31 mutations as accepted/preserved while only these four immediately forced
a rebuild:

- invalid magic;
- `channels=0`;
- `sample_rate=0`;
- `layer_count=0`.

Therefore `BEGIN=0` must not be described as full structural validation.
libreapeaks remains stricter about the standard region.

## Corrected `PCM_Source_GetPeaks` oracle

An early read comparator incorrectly compared every allocated ReaScript buffer
slot, including slots beyond the returned sample count. Those slots are
unspecified.

The corrected comparator uses the `PCM_Source_GetPeaks` return value:

- low 20 bits are the number of returned samples;
- output blocks remain spaced by the requested samples-per-channel count;
- only valid max/min/optional-extra slots participate in the signature.

With that corrected comparator, every pure EOF case in the permanent gate was:

- `BEGIN=0`;
- `READ_OK=1`;
- `GetPeaks`-identical to the unmodified control cache; and
- byte-for-byte unchanged after REAPER exited.

Some mutations inside the standard layer table were accepted by the shallow
`BuildPeaks(..., 0)` probe but produced different `GetPeaks` results. This is
why RPKX stays outside the REAPER layer table.

## Forced rebuild behavior

The rebuild oracle advances the source mtime so the old cache becomes stale and
runs the normal BuildPeaks lifecycle on both a plain cache and an EOF-extended
cache.

Observed result:

```text
plain input:  BEGIN=1, 5518 -> 5518 bytes
RPKX input:   BEGIN=1, 5756 -> 5518 bytes

rebuilt outputs byte-identical: true
RPKX extension present after rebuild: false
RPKX suffix preserved after rebuild: false
```

Thus EOF extensions survive cache **reuse**, but a true REAPER rebuild rewrites
the standard cache and discards the unknown suffix. Applications must regenerate
or reattach their RPKX data after such a rebuild.

## Production packed-v1 large-extension oracle

After the production design moved to a packed directory-first container, the
oracle was extended to answer the performance question: does REAPER read all the
way to physical EOF when the appended RPKX becomes very large?

The production fixture used:

```text
32-byte RPKX v1 header
48-byte directory entry
one payload named LOAD
```

The payload was sparse/zero-filled and tested at 0, 1, 16, 128, and 512 MiB.
REAPER ran under `strace` tracing `openat`, `read`, `pread64`, `lseek`, `mmap`,
`munmap`, and `close`.

Decisive workflow run: `33842942251`.

Artifact:

- ID: `9925546482`
- ZIP SHA-256:
  `6c15e92e3ee961e35dfd5f39b2fe90d2890774ad2c7855d79f3c75509aef0cf6`

Observed:

| case | complete cache bytes | elapsed under strace | `.reapeaks` read bytes | `.reapeaks` pread64 bytes | `.reapeaks` mmap max | peaks |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| plain | 5,518 | 6.728 s | 0 | 5,518 | 0 | control |
| packed RPKX 0 MiB | 5,598 | 6.727 s | 0 | 5,598 | 0 | identical |
| packed RPKX 1 MiB | 1,054,174 | 6.627 s | 0 | 65,536 | 0 | identical |
| packed RPKX 16 MiB | 16,782,814 | 6.577 s | 0 | 65,536 | 0 | identical |
| packed RPKX 128 MiB | 134,223,326 | 6.779 s | 0 | 65,536 | 0 | identical |
| packed RPKX 512 MiB | 536,876,510 | 6.578 s | 0 | 65,536 | 0 | identical |

All packed-v1 cases returned `BEGIN=0`, completed the corrected GetPeaks probe,
and left cache size/mtime unchanged. No mmap of the `.reapeaks` file was
observed. For payloads of 1 MiB and larger, traced `pread64` traffic stayed at
65,536 bytes rather than increasing with physical EOF.

Within this pinned REAPER 7.79/Linux path, the evidence therefore strongly
indicates that REAPER does **not** sequentially read the complete RPKX payload
for these waveform/peak reads. Startup-plus-strace wall time also remained
roughly flat through the 512 MiB case.

### Limits of the large-file result

The result is deliberately scoped:

- the giant payload is sparse/zero-filled, so this primarily measures REAPER's
  syscall/control-flow behavior rather than cold physical-disk throughput;
- fresh REAPER startup and strace dominate wall time;
- the workflow does not impose a timing threshold, because runner timing is
  noisy;
- the compatibility gate is reuse, peak-read identity, and cache preservation;
- no claim is made for other REAPER versions or operating systems.

## libreapeaks parser policy

The standard parser now tolerates bytes only **after** all known standard layers
have been consumed. It still rejects truncation inside the required standard
region and unknown in-table tokens whose payload size cannot be inferred.

Production RPKX v1 adds a contiguous directory before all payloads. The Rust
seekable API can therefore discover the entire RPKX inventory without reading
opaque payloads and then seek directly to a selected payload. See
`RPKX_SPEC.md` for the exact wire format and APIs.

## Lifecycle

The empirical lifecycle remains:

```text
standard cache valid + source stamp matches
    -> REAPER reuses cache
    -> RPKX survives unchanged

standard cache stale / REAPER rebuilds
    -> REAPER writes standard cache
    -> RPKX disappears
    -> application detects absence
    -> application regenerates or reattaches its extension data
```

If an analysis result must survive independently of REAPER's rewrite, an
application may maintain a separate durable analysis store and use RPKX as its
co-located cache representation.

## Reproduction files

The current research/gates live in:

- `.github/workflows/reaper-cache-extension-oracle.yml`
- `tools/reaper_oracle/cache_extension_oracle.py`
- `tools/reaper_oracle/cache_extension_oracle_fixed.py`
- `tools/reaper_oracle/cache_read_probe.lua`
- `tools/reaper_oracle/cache_read_matrix.py`
- `tools/reaper_oracle/cache_read_matrix_fixed.py`
- `tools/reaper_oracle/cache_read_rpkx_gate.py`
- `tools/reaper_oracle/cache_rebuild_rpkx.py`
- `tools/reaper_oracle/rpkx_large_io_oracle.py`

The `*_fixed.py` wrappers are retained because earlier experiments exposed two
harness bugs (`-'r'` payload sizing and comparison of unused GetPeaks buffer
slots). They preserve the evidence history rather than rewriting it silently.
