# REAPER source stamps and cache freshness

A `.reapeaks` header carries two fields used to detect an updated source file:

```text
uint32 low32(stat(source).st_mtime)
uint32 low32(stat(source).st_size)
```

This is documented by Cockos in the public format description:
<https://www.reaper.fm/sdk/reapeaks.txt>.

`st_mtime` is the source file's last-modification time. The `.reapeaks` field
stores whole seconds only and keeps the low 32 bits. `st_size` is the source
file's byte size, again reduced to its low 32 bits.

These values are a **cache freshness stamp**, not a collision-resistant source
identity. A source can change while preserving both values, for example if an
external tool rewrites the same number of bytes and restores the original
mtime. Applications implementing REAPER-style offline/online media handling
should therefore keep a stronger in-process fingerprint as well (file-id/inode,
full size, and nanosecond mtime are a useful baseline).

## Public API

Rust:

```rust
use reapeaks::{ReaPeaks, SourceStamp};

let stamp = SourceStamp::from_path("audio.wav")?;
let peaks = ReaPeaks::open("audio.wav.reapeaks")?;
let exact = peaks.matches_source_stamp(stamp);
```

`GenerateOptions::with_source_stamp(stamp)` and `set_source_stamp(stamp)` keep
the two generation fields coherent.

Python:

```python
mtime_low32, size_low32 = reapeaks.source_stamp("audio.wav")
peaks = reapeaks.ReaPeaks.open("audio.wav.reapeaks")
exact = peaks.matches_source_stamp(mtime_low32, size_low32)
# or: exact = peaks.matches_source("audio.wav")
```

C:

```c
RpkSourceStamp stamp;
rpk_source_stamp_from_path("audio.wav", &stamp);
rpk_get_source_stamp(handle, &stamp);
rpk_matches_source(handle, "audio.wav");
```

The C match functions return `1` for a match, `0` for a mismatch, and a negative
value on error.

## Exact matching versus REAPER's acceptance tolerance

The library comparison API is intentionally **exact and conservative**: both
32-bit fields must match.

Cockos documents that REAPER itself permits small mtime differences (a few
seconds), and also differences near one hour to accommodate DST-related
filesystem timestamp behavior. The exact acceptance boundaries are not part of
the published format contract. libreapeaks does not guess those thresholds.

Exact matching is a safe policy for applications: it may rebuild more often
than REAPER, but a stamp produced from the current source is still the stamp
REAPER expects when libreapeaks writes a shared cache.

## Live REAPER reuse gate

True cache sharing needs more than matching bytes and paths. The permanent
`reaper-source-stamp` workflow uses pinned **REAPER 7.79 x86_64 Linux** and
checks the public `PCM_Source_BuildPeaks` entrypoint directly.

The central interoperability case is:

1. create a deterministic source file;
2. let REAPER generate a cache and record its source stamp/canonical peak path;
3. verify `SourceStamp::from_path()` produces the same stamp;
4. replace the canonical cache with a libreapeaks-generated cache;
5. create the source in a fresh REAPER process;
6. call `PCM_Source_BuildPeaks(src, 0)` only;
7. require a return value of `0`, meaning REAPER says no peak-building work is
   necessary;
8. require the cache bytes to remain unchanged.

Negative controls deliberately corrupt the header mtime or source size and
require REAPER to request a rebuild. A separate content-only mutation preserves
both source fields and records whether REAPER still reuses the cache. This
separates metadata-based update detection from cache format/layer validity and
other rebuild conditions.

## Application-layer offline/online lifecycle

Window activation and media ownership remain application concerns. A typical
REAPER-like lifecycle is:

```text
active
  -> source/decoder open
  -> cache usable

window inactive
  -> stop playback
  -> release decoder/file/mmap handles
  -> external tools may replace source

window active again
  -> compare strong runtime fingerprint
  -> reopen source
  -> invalidate decoded/GPU state if changed
  -> validate/regenerate .reapeaks using SourceStamp
```

The reference players keep their stronger `(device, inode/file-id, size,
mtime_ns)` generation-time check in the application layer, while source-stamp
construction and `.reapeaks` freshness comparison are delegated to libreapeaks.
