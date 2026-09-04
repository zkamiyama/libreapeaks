# RPKX v1 — REAPER Peaks eXtension container

RPKX is an application-extension container appended to a normal REAPER
`.reapeaks` cache. It lets independent applications keep opaque metadata or
analysis results in the same file without changing REAPER's standard layer
table.

`RPKX` expands to **REAPER Peaks eXtension**. This document defines the current
libreapeaks **RPKX container version 1**.

RPKX v1 deliberately does not define chord, tempo, beat, transcript, marker,
embedding, model-feature, or other payload schemas. Applications own those
schemas, including their timebase and compression choices.

## Design contract

RPKX v1 has five format-level responsibilities:

1. leave the complete standard REAPER region byte-for-byte unchanged;
2. expose the complete chunk inventory without loading chunk payloads;
3. make every payload independently seekable by offset and length;
4. give independent applications collision-resistant namespaces; and
5. bind the extension set to the same REAPER-compatible `SourceStamp` as the
   standard cache.

The v1 serialized representation is always **packed**. It has no free list,
allocator arena, tombstones, historical payloads, or internal holes. Updating a
packed container may require rewriting payload bytes; filesystem-level
optimizations such as reflinks, `copy_file_range`, temporary files, writer
locking, and atomic replacement are deliberately outside the wire format.

## Placement

For modern caches whose standard layer sizes are known:

```text
+--------------------------------+  offset 0
| REAPER RPKM/RPKN/RPKL region   |
| fixed header                   |
| all layer headers              |
| all standard layer payloads    |
+--------------------------------+  computed standard_end
| RPKX v1 header                 |  32 bytes
+--------------------------------+
| RPKX directory entry 0         |  48 bytes
| RPKX directory entry 1         |  48 bytes
| ...                            |
+--------------------------------+  payload_region_start
| payload 0                      |
| payload 1                      |
| ...                            |
+--------------------------------+  standard_end + container_len
| optional unrelated EOF bytes   |
+--------------------------------+  physical EOF
```

The RPKX magic MUST begin exactly at the computed end of the standard REAPER
region. `container_len` self-delimits RPKX, so unrelated bytes may follow it.
libreapeaks byte editors preserve such a suffix.

If non-RPKX bytes already begin immediately at `standard_end`, high-level RPKX
mutation APIs refuse to guess where a container should be inserted.

The terminal legacy `-'l'` loudness payload is still ambiguous to libreapeaks,
so its exact standard end cannot safely be separated from an EOF extension.
RPKX discovery/editing therefore rejects that legacy layout.

## Endianness

All multi-byte integers are unsigned little-endian unless stated otherwise.

## Container header

The v1 header is exactly 32 bytes:

| offset | size | field | v1 meaning |
| ---: | ---: | --- | --- |
| 0 | 4 | magic | ASCII `RPKX` |
| 4 | 2 | version | `1` |
| 6 | 2 | header_size | `32` |
| 8 | 4 | flags | container flags; no standard v1 bits assigned |
| 12 | 4 | chunk_count | number of directory entries |
| 16 | 8 | container_len | bytes from `RPKX` magic through the final payload |
| 24 | 4 | source_mtime_low32 | REAPER-compatible source mtime stamp |
| 28 | 4 | source_size_low32 | REAPER-compatible source size stamp |

`container_len` includes the header, directory, and payload region. It excludes
any unrelated suffix following the RPKX container.

A v1 reader requires `header_size == 32`. An incompatible future framing change
must use a new container version.

## Source binding

The source fields copy the compatibility identity stored at offsets 10 and 14
of the standard `.reapeaks` header:

```text
SourceStamp {
    mtime_low32: u32,
    size_low32:  u32,
}
```

This is deliberately REAPER's weak compatibility stamp, not a
collision-resistant runtime file identity. Applications may store stronger
identity in their own payload if required.

The default attach/update API requires the RPKX stamp to match the standard
cache. The explicit Rust `AllowSourceStampMismatch` policy exists only for
controlled migration/recovery.

## Directory

Immediately after the 32-byte header come exactly `chunk_count` fixed-size
48-byte entries. No payload byte appears before the complete directory.

Each directory entry is:

| offset | size | field | v1 meaning |
| ---: | ---: | --- | --- |
| 0 | 16 | namespace | opaque application namespace, preferably stable UUID bytes |
| 16 | 4 | kind | application-defined FourCC |
| 20 | 4 | version | payload/schema version owned by the namespace |
| 24 | 4 | flags | payload flags owned by the namespace |
| 28 | 4 | reserved | MUST be zero in v1 |
| 32 | 8 | payload_offset | offset from the RPKX magic to this payload |
| 40 | 8 | payload_len | payload byte length |

The payload region starts at:

```text
payload_region_start = 32 + 48 * chunk_count
```

Because the entire directory is contiguous, a reader can discover every RPKX
chunk after reading only the standard REAPER fixed header/layer table plus:

```text
32 + 48 * chunk_count
```

RPKX bytes. It does not need to seek across, page in, or copy any opaque payload
just to discover what is present.

### Canonical packed layout

RPKX v1 has one canonical payload layout:

```text
entry[0].payload_offset = payload_region_start
entry[n+1].payload_offset = entry[n].payload_offset + entry[n].payload_len
last.payload_offset + last.payload_len = container_len
```

There are no alignment gaps. Zero-length payloads are legal and naturally share
the same boundary offset with the following payload.

A v1 reader rejects a directory whose offsets describe gaps, overlaps,
out-of-order payloads, or unused bytes inside `container_len`. This keeps the
serialized file compact and prevents allocator/free-list semantics from leaking
into the format.

## Namespaces, keys, and duplicates

Applications SHOULD use the canonical 16 bytes of a stable UUID as
`namespace`. The all-zero namespace is reserved for possible future
libreapeaks/common definitions; v1 defines none.

The logical key is:

```text
(namespace[16], kind[4])
```

Duplicates are legal. `append_chunk` preserves multiplicity. The high-level
`set_chunk` operation implements unique-value behavior by removing every
existing matching key and inserting one replacement at the first previous
position. `remove_chunks` removes every matching key.

RPKX assigns no semantics to serialized ordering beyond preserving it during
normal edits.

## Selective reading

The packed directory is specifically designed for large payloads.

A seekable reader can perform:

```text
read standard REAPER fixed header + layer table
    -> compute standard_end without reading standard payload bytes
seek standard_end
read 32-byte RPKX header
read 48 * chunk_count directory bytes
    -> complete RPKX inventory is now known
seek container_offset + selected.payload_offset
read selected.payload_len
```

The Rust APIs supporting this are:

```rust
let mut file = std::fs::File::open(path)?;
let index = reapeaks::scan_rpkx(&mut file)?.unwrap();

for entry in &index.entries {
    println!("{:?} {:?}: {} bytes", entry.key.namespace, entry.key.kind, entry.payload_len);
}

if let Some(entry) = index.entry(my_key) {
    let payload = reapeaks::read_rpkx_payload(&mut file, &index, entry)?;
}
```

For very large selected payloads, `copy_rpkx_payload()` streams exactly that
payload to a caller-provided writer without materializing it as a `Vec<u8>`.

`RpkxIndex::parse_prefix()` provides the same metadata-only parser when a caller
already has the header+directory bytes in memory.

`RpkxContainer::parse()` and `read_rpkx()` remain convenient **owning/eager**
APIs: they intentionally copy every payload into `Vec<u8>`. Large-file users
should use `scan_rpkx()` and selected-payload APIs instead.

## Parse validity

A canonical v1 container is structurally valid when:

- magic is `RPKX`;
- version is 1 and `header_size == 32`;
- the 32-byte header plus all 48-byte directory entries fits in
  `container_len`;
- every directory reserved field is zero;
- payload offsets form the exact canonical packed sequence;
- no offset/length arithmetic overflows;
- the final payload ends exactly at `container_len`; and
- when parsing a complete file/byte slice, the physical file contains all bytes
  through `container_len`.

Unknown namespaces, FourCCs, schema versions, flags, or payload contents are not
errors.

## Preservation and mutation rules

When the existing RPKX is decoded and re-encoded by libreapeaks byte editors:

- `[0, standard_end)` remains byte-for-byte unchanged;
- unrelated chunks retain namespace, kind, version, flags, payload bytes, and
  logical order;
- the resulting RPKX payload region is repacked canonically;
- an opaque suffix after the recognized RPKX container is preserved; and
- non-RPKX bytes immediately following the standard REAPER region are never
  overwritten implicitly.

Packed serialization means a small mutation can move later payload offsets.
This is intentional. RPKX v1 optimizes the on-disk representation for simple,
contiguous reads rather than implementing a miniature allocator.

Filesystem mutation APIs may later optimize physical rewriting with reflinks or
kernel-side copying, but such optimizations MUST produce the same canonical
packed bytes.

## Earlier experimental and pre-freeze layouts

Two earlier research layouts used the same `RPKX` magic but are **not RPKX v1 as
defined here**:

1. the original oracle fixture
   `[RPKX][u32 version][u32 payload_len][payload]`; and
2. the pre-freeze implementation that used a 32-byte container header followed
   by interleaved 40-byte chunk-header/payload records.

They were experimental design probes before v1 was frozen. The current v1
reader intentionally rejects both rather than trying to heuristically interpret
multiple incompatible layouts under one version number.

## Rust owning/editing API

The existing byte-oriented API remains available:

```rust
let updated = reapeaks::set_rpkx_chunk(
    &reapeaks_bytes,
    reapeaks::RpkxChunk::new(namespace, *b"CHRD", 1, 0, payload),
)?;

let container = reapeaks::read_rpkx(&updated)?.unwrap();
```

Other operations include `append_rpkx_chunk`, `remove_rpkx_chunks`,
`strip_rpkx`, `attach_rpkx`, and `RpkxContainer::{parse, encode, set_chunk,
append_chunk, remove_chunks}`.

## Python and C APIs

The existing Python and C byte-oriented RPKX APIs continue to operate on the
canonical packed v1 encoding. Their current high-level read surfaces are owning
APIs; the Rust seekable API is the reference selective-I/O surface at this
stage. Do not infer that a language binding avoids loading a complete file just
because the wire format permits selective reading.

## REAPER coexistence evidence

RPKX is not an official Cockos `.reapeaks` feature. Compatibility claims are
therefore explicitly scoped to observed behavior.

The permanent workflow `.github/workflows/reaper-cache-extension-oracle.yml`
tests pinned **REAPER 7.79 Linux x86_64 on Ubuntu 24.04 under Xvfb**. In addition
to the earlier EOF acceptance/GetPeaks/rebuild matrix, it now exercises
production packed-v1 files with one sparse zero payload of 0, 1, 16, 128, and
512 MiB.

Observed in workflow run `33842942251`:

| case | complete cache bytes | wall time under strace | `.reapeaks` read() bytes | `.reapeaks` pread64() bytes | mmap max | GetPeaks vs control |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| plain | 5,518 | 6.728 s | 0 | 5,518 | 0 | identical |
| packed RPKX 0 MiB | 5,598 | 6.727 s | 0 | 5,598 | 0 | identical |
| packed RPKX 1 MiB | 1,054,174 | 6.627 s | 0 | 65,536 | 0 | identical |
| packed RPKX 16 MiB | 16,782,814 | 6.577 s | 0 | 65,536 | 0 | identical |
| packed RPKX 128 MiB | 134,223,326 | 6.779 s | 0 | 65,536 | 0 | identical |
| packed RPKX 512 MiB | 536,876,510 | 6.578 s | 0 | 65,536 | 0 | identical |

All cases returned `PCM_Source_BuildPeaks(src, 0) == 0`, completed the corrected
`PCM_Source_GetPeaks` probe, and left the cache size/mtime unchanged. No mmap of
the `.reapeaks` file was observed. For payloads of 1 MiB and larger, traced
`pread64` traffic against `.reapeaks` stayed at 65,536 bytes rather than scaling
with physical EOF.

This is strong evidence that **this tested REAPER path does not sequentially
read the entire RPKX payload** and that cache-read latency did not scale with the
512 MiB logical EOF extension in this experiment.

Important scope limitations:

- the large payload is sparse/zero-filled, so this is primarily an I/O syscall
  and control-flow oracle, not a benchmark of physically allocated cold-disk
  storage;
- wall time includes fresh REAPER startup and strace overhead;
- no performance threshold is currently gated, only reuse/read correctness and
  cache preservation; and
- the result must not be generalized to other REAPER versions/platforms without
  separate testing.

When REAPER is forced to rebuild a stale cache, the existing oracle still shows
that it rewrites the standard cache and discards the unknown EOF extension.
Applications must therefore regenerate or reattach their own RPKX data after a
real REAPER rebuild.

See [`RPKX_EOF_EXTENSIONS.md`](RPKX_EOF_EXTENSIONS.md) for the historical EOF
research and the distinction between shallow `BuildPeaks(..., 0)` acceptance
and corrected forced peak reads.
