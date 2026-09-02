# RPKX EOF extension research

This document records the current evidence for appending non-REAPER metadata to
a `.reapeaks` file without changing the standard REAPER region.

The central result is narrow but useful:

- pinned REAPER 7.79 Linux x86_64 accepts and byte-preserves arbitrary bytes
  appended after the computed end of the standard `.reapeaks` layers while the
  cache is reusable;
- corrected `PCM_Source_GetPeaks` reads from pure EOF-appended caches are
  identical to reads from the unmodified REAPER cache in the tested matrix; and
- when REAPER actually rebuilds the cache, the appended bytes are discarded
  because REAPER rewrites the standard cache from scratch.

RPKX is therefore a viable **coexistence extension while the REAPER cache is
reused**, but it must not be treated as metadata that REAPER itself preserves
across rebuilds.

## Scope and oracle

The live evidence in this document is pinned to:

- REAPER 7.79 Linux x86_64;
- Ubuntu 24.04 under Xvfb;
- a deterministic 48 kHz mono PCM16 WAV;
- `peakcachegenrs=300`, `showpeaks=64`, `peakcachegenmode=3`;
- fresh REAPER process per mutation/read/rebuild case.

The permanent workflow is
`.github/workflows/reaper-cache-extension-oracle.yml`.

A decisive run for the parser-policy and rebuild experiments is GitHub Actions
run `33612051368` (artifact `9839421017`). The run completed successfully.

Do not generalize these observations to all REAPER versions/platforms without a
separate live oracle.

## Standard `.reapeaks` region

Cockos documents the file format at:

<https://www.reaper.fm/sdk/reapeaks.txt>

For the RPKN/RPKL family, the standard region is conceptually:

```text
fixed header (18 bytes)
layer header table (8 bytes * layer_count)
layer 0 payload
layer 1 payload
...
layer N-1 payload
```

The fixed header contains:

```text
offset  size  meaning
0       4     magic: RPKM/RPKN/RPKL
4       1     channels
5       1     layer/mipmap count
6       4     source sample rate
10      4     low 32 bits of source stat().st_mtime
14      4     low 32 bits of source stat().st_size
```

Each layer header is:

```text
int32  division_or_special_token
uint32 count
```

The file does not contain a generic `payload_length` for each layer and does
not contain a total standard-region byte count. A reader computes the payload
size from the layer kind, count, channels and container version.

For known modern layers this makes the standard end calculable. Importantly,
the official format does not define a generic way to skip an unknown layer
token: there is no independent length field telling a reader how many bytes an
unknown token owns.

This distinction is why an EOF extension is substantially safer than inventing
an in-table custom layer.

## The tested standard cache

The deterministic fixture used by the extension oracle produced a 5518-byte
RPKN cache. Its native shape was the normal REAPER waveform + `-'s'` spectral +
`-'r'` loudness shape used by the existing source-stamp oracle.

The experimental timeline RPKX fixture appended 238 bytes, making the complete
file 5756 bytes:

```text
0                                      5518               5756
|---------------------------------------|------------------|
| standard REAPER .reapeaks region      | experimental RPKX|
|---------------------------------------|------------------|
                                        ^ computed standard end
```

The experimental suffix used this intentionally simple envelope:

```text
4 bytes  "RPKX"
4 bytes  version = 1
4 bytes  payload length
N bytes  JSON fixture payload
```

The JSON contained example frame-addressed tempo and chord events. This is an
oracle fixture, **not a frozen RPKX file-format specification**.

## EOF append acceptance

Starting from one fresh REAPER-generated cache, the oracle changed one region
at a time and then called `PCM_Source_BuildPeaks(src, 0)` in a fresh process.
It also hashed the cache before and after REAPER exited.

The important pure EOF cases all returned `BEGIN=0` and were byte-for-byte
preserved:

| case | input -> output | result |
| --- | ---: | --- |
| append one `0x00` byte | 5519 -> 5519 | reused, preserved |
| append one `0xff` byte | 5519 -> 5519 | reused, preserved |
| append 16 zero bytes | 5534 -> 5534 | reused, preserved |
| append empty RPKX envelope | 5530 -> 5530 | reused, preserved |
| append tempo/chord RPKX | 5756 -> 5756 | reused, preserved |
| repeat tempo/chord RPKX in a new process | 5756 -> 5756 | reused, preserved |
| append deterministic arbitrary 4 KiB | 9614 -> 9614 | reused, preserved |

This demonstrates that REAPER 7.79 does not require the physical file EOF to
coincide with its computed standard-layer end in this tested path.

### `BuildPeaks(..., 0)` is not a full structural validator

The broader 31-case matrix is also important because it shows what **not** to
infer from `BEGIN=0`.

27/31 mutations returned `BEGIN=0`. That set included deliberately malformed
layer-table/count/truncation cases. Only four tested mutations forced an
immediate rebuild:

- invalid magic (`RPKX` in place of `RPKN`);
- `channels=0`;
- `sample_rate=0`;
- `layer_count=0`.

Therefore `PCM_Source_BuildPeaks(src, 0) == 0` should be treated as REAPER's
reuse decision, not as proof that every byte of the cache is structurally
valid.

libreapeaks remains stricter than this shallow reuse check for the standard
region: required standard payload bytes must still exist and unknown in-table
layer tokens remain unsupported.

## Corrected `PCM_Source_GetPeaks` comparison

An early read oracle compared the entire allocated ReaScript buffer and
therefore compared unspecified slots after the actual returned sample count.
That was incorrect.

The corrected comparator uses the `PCM_Source_GetPeaks` return value correctly:

- low 20 bits: number of returned samples;
- output blocks remain spaced by the requested samples-per-channel count;
- only returned slots in valid max/min/extra blocks participate in comparison.

With that corrected comparator, all pure EOF-extension cases above were:

- `BEGIN=0`;
- `READ_OK=1`;
- `GetPeaks` signature identical to the plain control cache; and
- byte-for-byte unchanged after the read process exited.

The workflow now has a dedicated gate that fails unless these EOF cases remain
identical to control.

Some layer-table mutations that were still accepted by `BuildPeaks(..., 0)` did
produce different `GetPeaks` results. That is further evidence that **EOF append
and layer-table modification are different risk classes**. RPKX should remain
outside the REAPER layer table.

## Forced REAPER rebuild: RPKX is discarded

The rebuild oracle answers the most important lifecycle question.

Procedure:

1. take the plain 5518-byte REAPER cache and the 5756-byte
   `standard-cache + RPKX` cache;
2. move the source file mtime forward by 120 seconds without changing its
   content;
3. present each old-stamp cache to a fresh REAPER process;
4. run the normal `PCM_Source_BuildPeaks` Begin/Run/Finish lifecycle;
5. compare complete rebuilt outputs.

Observed result:

```text
plain input:  BEGIN=1, 5518 -> 5518 bytes
RPKX input:   BEGIN=1, 5756 -> 5518 bytes

rebuilt outputs byte-identical: true
RPKX extension present after rebuild: false
RPKX suffix preserved after rebuild: false
```

The interpretation is straightforward: when REAPER decides to regenerate the
cache, it writes the standard `.reapeaks` file it knows how to generate. It does
not preserve an unknown EOF suffix.

This is not a defect in the EOF-extension idea; it defines its lifecycle.
Applications that care about custom metadata must treat RPKX as an extension
they own and reattach/regenerate it after a REAPER rebuild.

## Parser policy in libreapeaks

The library previously required:

```text
computed standard end == physical EOF
```

and returned `trailing bytes after layers` otherwise.

That policy was stricter than the tested REAPER behavior and prevented future
EOF extensions. The parser policy has therefore been changed on the research
branch:

- `ReaPeaks::parse` accepts bytes after all known standard layers have been
  parsed;
- the complete input remains in `ReaPeaks::raw`, including the tail;
- `GpuCacheView` accepts the same tail and never includes it in a standard
  layer/tile range;
- `parse_spectrogram_layers` ignores the tail after the standard layers;
- truncation *inside* the required standard region remains an error;
- unknown in-table layer tokens remain an error/unsupported condition because
  their payload length cannot be derived safely.

Regression tests cover the decoded parser, raw GPU view and spectrogram
extractor with an RPKX-like suffix.

This is deliberately **not** equivalent to accepting arbitrary corruption. The
new tolerance begins only after the parser has successfully consumed all
standard layers it knows how to size.

### Legacy `-'l'` caveat

The legacy `-'l'` loudness payload layout is not fully established in
libreapeaks. The current parser treats a terminal legacy `-'l'` payload as
opaque remaining data. Consequently it cannot reliably split a hypothetical
RPKX suffix from that legacy payload until the old layout is recovered.

Modern RPKN/RPKL caches using the known `-'r'` loudness layout do not have this
ambiguity.

## Recommended RPKX lifecycle

The empirical lifecycle is now:

```text
standard cache valid + stamp matches
    -> REAPER reuses cache
    -> EOF RPKX survives unchanged

standard cache becomes stale / REAPER rebuilds
    -> REAPER rewrites standard cache
    -> RPKX disappears
    -> libreapeaks/application detects absence
    -> custom analysis is regenerated or reattached
```

This maps cleanly onto the existing ownership split:

- REAPER/libreapeaks standard cache freshness uses `SourceStamp` semantics;
- stronger source identity/change detection remains an application concern;
- RPKX metadata generation/versioning is owned by libreapeaks or the consuming
  application;
- after a rebuild, extension regeneration should be atomic and should not alter
  the standard REAPER bytes.

If preserving expensive analysis across a REAPER rebuild matters, the
application may keep a separate durable analysis store and use RPKX as the
co-located cache representation. A single-file-only design cannot rely on
REAPER to preserve unknown metadata during its own rewrite.

## Direction for an eventual RPKX format

The current JSON fixture should not be standardized. A production envelope
should be self-delimiting and forward-compatible independently of REAPER, for
example:

```text
RPKX header
  magic
  RPKX version
  total extension length
  flags/checksum

chunk table / length-delimited chunks
  TEMP  tempo timeline
  BEAT  beat/downbeat timeline
  CHRD  chord timeline
  KEY_  key timeline
  SECT  section timeline
  ...
```

Timeline positions should normally use source frames rather than floating-point
seconds so waveform, playback and analysis share one exact source coordinate
system.

Before freezing such a format, add live-oracle coverage for Windows/macOS and
multiple REAPER versions, and decide how a writer locates the standard end for
all supported historical layer layouts.

## Reproduction files

The research/gates live in:

- `.github/workflows/reaper-cache-extension-oracle.yml`
- `tools/reaper_oracle/cache_extension_oracle.py`
- `tools/reaper_oracle/cache_extension_oracle_fixed.py`
- `tools/reaper_oracle/cache_read_probe.lua`
- `tools/reaper_oracle/cache_read_matrix.py`
- `tools/reaper_oracle/cache_read_matrix_fixed.py`
- `tools/reaper_oracle/cache_read_rpkx_gate.py`
- `tools/reaper_oracle/cache_rebuild_rpkx.py`

The temporary `*_fixed.py` entrypoints exist because the first experimental
oracles exposed two mistakes in the research harness itself (`-'r'` byte sizing
and unused `GetPeaks` buffer-slot comparison). Permanent cleanup can fold those
corrections back into the base scripts after the evidence is reviewed.
