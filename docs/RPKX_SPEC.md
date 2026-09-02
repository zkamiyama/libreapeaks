# RPKX v1 — REAPER Peaks eXtension container

RPKX is an application-extension container appended to a normal REAPER
`.reapeaks` file. It exists so independent applications can store arbitrary
metadata or analysis results in the same cache file without modifying REAPER's
standard layer table or assigning global semantics to every payload.

`RPKX` expands to **REAPER Peaks eXtension**. The `RPK` prefix deliberately
follows REAPER's existing `RPKM` / `RPKN` / `RPKL` cache magics; `X` denotes the
extension container.

This document defines the libreapeaks **RPKX container version 1**. It does not
define chord, tempo, beat, transcript, marker, ML-feature, or any other payload
schema.

## Design contract

RPKX has four strict responsibilities:

1. leave the complete standard REAPER region byte-for-byte unchanged;
2. provide length-delimited framing so unknown chunks can be skipped safely;
3. give independently developed applications collision-resistant namespaces;
4. bind the extension set to the same REAPER-compatible `SourceStamp` as the
   standard cache.

Everything inside a chunk payload is opaque to libreapeaks. In particular the
library does **not** prescribe a timebase. A payload may use source sample
frames, fractional frames, seconds, PPQ, protobuf timestamps, JSON, FlatBuffers,
MessagePack, or any other schema chosen by its owner.

## Placement

For modern caches whose standard layer sizes are known:

```text
+-------------------------------+  offset 0
| REAPER RPKM/RPKN/RPKL region  |
| fixed header                  |
| all layer headers             |
| all standard layer payloads   |
+-------------------------------+  computed standard_end
| RPKX v1 container             |
+-------------------------------+  standard_end + container_len
| optional unrelated EOF bytes  |
+-------------------------------+  physical EOF
```

The RPKX magic MUST begin exactly at the computed end of the standard REAPER
region. `container_len` makes the RPKX container itself self-delimiting, so
unrelated bytes may follow it and are preserved by libreapeaks editors.

If non-RPKX bytes already begin immediately at `standard_end`, libreapeaks does
not guess where an RPKX container should be inserted and does not overwrite
those bytes. High-level RPKX mutation APIs return an error in that case.

The legacy `-'l'` loudness payload length is not established by libreapeaks, so
its true standard end cannot currently be separated safely from an EOF
extension. RPKX discovery/editing therefore rejects that ambiguous layout.

## Endianness

All multi-byte integers in RPKX v1 are unsigned little-endian integers, matching
REAPER `.reapeaks` integer byte order.

## Container header

The v1 container header is exactly 32 bytes:

| offset | size | field | v1 meaning |
| ---: | ---: | --- | --- |
| 0 | 4 | magic | ASCII `RPKX` |
| 4 | 2 | version | `1` |
| 6 | 2 | header_size | `32` |
| 8 | 4 | flags | container flags; no standard v1 bits assigned |
| 12 | 4 | chunk_count | number of following chunks |
| 16 | 8 | container_len | bytes from `RPKX` magic through final chunk payload |
| 24 | 4 | source_mtime_low32 | REAPER-compatible source mtime stamp |
| 28 | 4 | source_size_low32 | REAPER-compatible source size stamp |

`container_len` includes the 32-byte container header. It does not include any
unrelated bytes after the container.

A v1 reader requires `header_size == 32`. A future incompatible framing change
must use a new container version rather than silently changing v1 offsets.

Unknown container flag bits are preserved by decode/re-encode. No standard
container flag bits are assigned in v1.

## Source binding

The two source fields copy the same compatibility identity stored at offsets
10 and 14 of the standard `.reapeaks` header:

```text
SourceStamp {
    mtime_low32: u32,
    size_low32:  u32,
}
```

This is deliberately the REAPER-compatible weak stamp, not a cryptographic file
identity. Applications may store stronger identity inside their own chunks if
needed.

The default libreapeaks attach/update policy requires the RPKX stamp to match
the standard cache exactly. This matters because live REAPER evidence shows
that a real REAPER rebuild discards an EOF extension. An application that saved
an old RPKX before rebuild must not blindly reattach it to a different source.

An explicit `AllowSourceStampMismatch` escape hatch exists for controlled
migration/recovery tools.

## Chunk header

Each v1 chunk has a fixed 40-byte header followed immediately by its payload:

| offset | size | field | v1 meaning |
| ---: | ---: | --- | --- |
| 0 | 16 | namespace | opaque application namespace, preferably UUID bytes |
| 16 | 4 | kind | application-defined FourCC |
| 20 | 4 | version | payload/schema version defined by namespace owner |
| 24 | 4 | flags | payload flags defined by namespace owner |
| 28 | 4 | reserved | MUST be zero in v1 |
| 32 | 8 | payload_len | number of following opaque payload bytes |
| 40 | N | payload | uninterpreted bytes |

Chunks are packed consecutively with no alignment padding.

### Namespace collision policy

Applications SHOULD allocate a stable UUID and store its canonical 16 bytes in
`namespace`. No central registry is required. Two applications may use the same
FourCC because namespace participates in the key.

The all-zero namespace is reserved for future libreapeaks/common standardized
chunks. RPKX v1 itself defines none.

### Chunk keys and duplicates

The logical key is:

```text
(namespace[16], kind[4])
```

Duplicates are legal. This supports append-only observations, multiple model
outputs, or other application-defined multiplicity.

The high-level `set_chunk` API implements the common unique-value behavior: it
removes all existing chunks with the same key and inserts one replacement at
the first previous position. `append_chunk` deliberately permits duplicates.
`remove_chunks` removes every matching key.

No ordering semantics are assigned by RPKX v1 beyond preserving serialized
order.

## Parse validity

A v1 container is structurally valid when:

- magic is `RPKX`;
- version is 1 and header size is 32;
- `container_len` fits within available bytes;
- exactly `chunk_count` chunks can be walked using their `payload_len` values;
- each chunk reserved field is zero; and
- the final chunk ends exactly at `container_len`.

Unknown namespace, kind, version, flags, or payload contents are not errors.
They are the normal extension mechanism.

## Preservation rules

When updating one chunk, libreapeaks guarantees:

- bytes `[0, standard_end)` are copied unchanged;
- unrelated RPKX chunks retain namespace, kind, version, flags and payload;
- an opaque suffix following the recognized RPKX container is copied unchanged;
- non-RPKX bytes that precede any discoverable RPKX are never overwritten
  implicitly.

This is the key interoperability contract for multiple independent RPKX users.

## Rust API

Core types and operations are exported from the crate root:

```rust
use reapeaks::{
    read_rpkx, set_rpkx_chunk, RpkxChunk, RpkxKey, RpkxContainer,
};

let namespace = [/* stable UUID bytes */ 0u8; 16];
let key = RpkxKey::new(namespace, *b"CHRD");

let updated = set_rpkx_chunk(
    &reapeaks_bytes,
    RpkxChunk::new(namespace, *b"CHRD", 1, 0, my_payload),
)?;

let container = read_rpkx(&updated)?.unwrap();
let payload = &container.chunk(key).unwrap().payload;
```

The lower-level API also exposes:

- `standard_end()`
- `reapeaks_source_stamp()`
- `RpkxContainer::parse()` / `encode()`
- `RpkxContainer::{set_chunk, append_chunk, remove_chunks}`
- `attach_rpkx()` with `RpkxAttachPolicy`
- `strip_rpkx()`
- `append_rpkx_chunk()`
- `remove_rpkx_chunks()`

## Python API

The Python module exposes opaque chunks and byte-to-byte editors:

```python
ns = bytes.fromhex("107a928e49024c62a827a02983ee1101")

updated = libreapeaks.rpkx_set_chunk(
    cache_bytes,
    ns,
    b"CHRD",
    1,
    payload,
)

for chunk in libreapeaks.rpkx_chunks(updated):
    print(chunk.namespace, chunk.kind, chunk.version, chunk.flags, chunk.payload)
```

Available functions are `rpkx_chunks`, `rpkx_container_info`,
`rpkx_set_chunk`, `rpkx_append_chunk`, `rpkx_remove_chunks`, and `rpkx_strip`.

## C API

`include/reapeaks.h` exposes `RpkxChunkInfo`, read access on `RpkHandle`, and
byte-to-byte `rpk_rpkx_*` mutation functions. Mutation results use `RpkBuffer`
and must be released with `rpk_buffer_free()`.

## REAPER interoperability boundary

RPKX is not an official Cockos `.reapeaks` feature. The coexistence basis is
behavioral evidence from pinned REAPER 7.79 Linux x86_64:

- pure EOF additions were reused and preserved byte-for-byte;
- corrected `PCM_Source_GetPeaks` reads were identical with and without the EOF
  additions in the tested matrix;
- forcing REAPER to rebuild rewrote the cache and discarded the appended bytes.

See [`RPKX_EOF_EXTENSIONS.md`](RPKX_EOF_EXTENSIONS.md) for the oracle evidence
and scope. RPKX therefore belongs to the application-owned cache lifecycle:
REAPER can safely reuse it in the tested configuration, but applications must
be prepared to regenerate or reattach their own chunks after REAPER rewrites the
standard cache.
