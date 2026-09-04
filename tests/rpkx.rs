use reapeaks::format::{encode, GeneratedLayer, LayerHeader, Version};
use reapeaks::{
    append_rpkx_chunk, attach_rpkx, read_rpkx, read_rpkx_index, read_rpkx_payload,
    remove_rpkx_chunks, scan_rpkx, set_rpkx_chunk, standard_end, strip_rpkx, ReaPeaks,
    RpkxAttachPolicy, RpkxChunk, RpkxContainer, RpkxIndex, RpkxKey, SourceStamp,
    RPKX_DIRECTORY_ENTRY_SIZE, RPKX_HEADER_SIZE,
};
use std::io::{Cursor, Read, Seek, SeekFrom};

const NS_A: [u8; 16] = [
    0x10, 0x7a, 0x92, 0x8e, 0x49, 0x02, 0x4c, 0x62, 0xa8, 0x27, 0xa0, 0x29, 0x83, 0xee, 0x11, 0x01,
];
const NS_B: [u8; 16] = [
    0x95, 0xf3, 0x4d, 0x20, 0x6c, 0x91, 0x4a, 0x0a, 0xb7, 0xa9, 0x8d, 0x75, 0xec, 0xc0, 0x02, 0xb4,
];

fn base_cache() -> Vec<u8> {
    encode(
        Version::Rpkn,
        1,
        48_000,
        0x1234_5678,
        0x0002_ee2c,
        &[GeneratedLayer {
            header: LayerHeader {
                division: 160,
                peak_count: 1,
            },
            bytes: vec![0x34, 0x12, 0xcc, 0xed],
        }],
    )
    .unwrap()
}

#[test]
fn rpkx_set_preserves_standard_reaper_bytes() {
    let base = base_cache();
    let out = set_rpkx_chunk(
        &base,
        RpkxChunk::new(NS_A, *b"CHRD", 3, 0x20, b"opaque chord bytes".to_vec()),
    )
    .unwrap();

    assert_eq!(standard_end(&out).unwrap(), base.len());
    assert_eq!(&out[..base.len()], base.as_slice());
    assert!(out.len() > base.len() + RPKX_HEADER_SIZE);
    ReaPeaks::parse(out.clone()).unwrap();

    let container = read_rpkx(&out).unwrap().unwrap();
    assert_eq!(
        container.source_stamp,
        SourceStamp::new(0x1234_5678, 0x0002_ee2c)
    );
    let chunk = container.chunk(RpkxKey::new(NS_A, *b"CHRD")).unwrap();
    assert_eq!(chunk.version, 3);
    assert_eq!(chunk.flags, 0x20);
    assert_eq!(chunk.payload, b"opaque chord bytes");
}

#[test]
fn directory_is_contiguous_and_payloads_are_packed_after_it() {
    let base = base_cache();
    let out = append_rpkx_chunk(
        &set_rpkx_chunk(&base, RpkxChunk::new(NS_A, *b"AAAA", 1, 0, b"abc".to_vec())).unwrap(),
        RpkxChunk::new(NS_B, *b"BBBB", 2, 7, b"12345".to_vec()),
    )
    .unwrap();

    let index = read_rpkx_index(&out).unwrap().unwrap();
    assert_eq!(index.entries.len(), 2);
    let payload_start = (RPKX_HEADER_SIZE + 2 * RPKX_DIRECTORY_ENTRY_SIZE) as u64;
    assert_eq!(index.entries[0].payload_offset, payload_start);
    assert_eq!(index.entries[0].payload_len, 3);
    assert_eq!(index.entries[1].payload_offset, payload_start + 3);
    assert_eq!(index.entries[1].payload_len, 5);
    assert_eq!(index.container_len, payload_start + 8);

    let absolute = base.len() + payload_start as usize;
    assert_eq!(&out[absolute..absolute + 8], b"abc12345");
}

#[test]
fn directory_prefix_can_be_parsed_without_payload_bytes() {
    let mut container = RpkxContainer::new(SourceStamp::new(11, 22));
    container.append_chunk(RpkxChunk::new(NS_A, *b"BIG_", 1, 0, vec![0x5a; 1024]));
    container.append_chunk(RpkxChunk::new(NS_B, *b"SMOL", 1, 0, b"ok".to_vec()));
    let encoded = container.encode().unwrap();
    let prefix_len = RPKX_HEADER_SIZE + 2 * RPKX_DIRECTORY_ENTRY_SIZE;

    let index = RpkxIndex::parse_prefix(&encoded[..prefix_len]).unwrap();
    assert_eq!(index.entries.len(), 2);
    assert_eq!(index.entries[0].payload_len, 1024);
    assert_eq!(index.entries[1].payload_len, 2);
    assert!(index.container_len as usize > prefix_len);
}

struct CountingCursor {
    inner: Cursor<Vec<u8>>,
    bytes_read: usize,
}

impl CountingCursor {
    fn new(bytes: Vec<u8>) -> Self {
        Self {
            inner: Cursor::new(bytes),
            bytes_read: 0,
        }
    }
}

impl Read for CountingCursor {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        let read = self.inner.read(buf)?;
        self.bytes_read += read;
        Ok(read)
    }
}

impl Seek for CountingCursor {
    fn seek(&mut self, pos: SeekFrom) -> std::io::Result<u64> {
        self.inner.seek(pos)
    }
}

#[test]
fn seekable_scan_does_not_read_large_payload_until_selected() {
    let base = base_cache();
    let payload = vec![0xa5; 2 * 1024 * 1024];
    let out = set_rpkx_chunk(&base, RpkxChunk::new(NS_A, *b"BIG_", 1, 0, payload.clone())).unwrap();
    let mut reader = CountingCursor::new(out);

    let index = scan_rpkx(&mut reader).unwrap().unwrap();
    assert_eq!(index.entries.len(), 1);
    assert_eq!(index.entries[0].payload_len, payload.len() as u64);
    assert!(
        reader.bytes_read < 256,
        "scan read {} bytes",
        reader.bytes_read
    );

    let before_payload = reader.bytes_read;
    let selected = read_rpkx_payload(&mut reader, &index, &index.entries[0]).unwrap();
    assert_eq!(selected, payload);
    assert_eq!(reader.bytes_read - before_payload, payload.len());
}

#[test]
fn unique_set_replaces_only_its_own_namespace_and_kind() {
    let base = base_cache();
    let a1 = RpkxChunk::new(NS_A, *b"DATA", 1, 0, b"a1".to_vec());
    let b = RpkxChunk::new(NS_B, *b"DATA", 7, 9, b"foreign".to_vec());
    let a2 = RpkxChunk::new(NS_A, *b"DATA", 2, 5, b"a2".to_vec());

    let out = set_rpkx_chunk(&base, a1).unwrap();
    let out = set_rpkx_chunk(&out, b.clone()).unwrap();
    let out = set_rpkx_chunk(&out, a2.clone()).unwrap();
    let container = read_rpkx(&out).unwrap().unwrap();

    assert_eq!(container.chunks.len(), 2);
    assert_eq!(container.chunk(a2.key), Some(&a2));
    assert_eq!(container.chunk(b.key), Some(&b));
}

#[test]
fn append_allows_duplicate_keys_and_remove_removes_all() {
    let base = base_cache();
    let key = RpkxKey::new(NS_A, *b"MARK");
    let out =
        append_rpkx_chunk(&base, RpkxChunk::new(NS_A, *b"MARK", 1, 0, b"one".to_vec())).unwrap();
    let out =
        append_rpkx_chunk(&out, RpkxChunk::new(NS_A, *b"MARK", 1, 0, b"two".to_vec())).unwrap();
    let container = read_rpkx(&out).unwrap().unwrap();
    assert_eq!(container.chunks_for(key).count(), 2);

    let stripped = remove_rpkx_chunks(&out, key).unwrap();
    assert_eq!(stripped, base);
}

#[test]
fn opaque_suffix_after_rpkx_is_preserved_on_update_and_strip() {
    let base = base_cache();
    let mut out =
        set_rpkx_chunk(&base, RpkxChunk::new(NS_A, *b"JSON", 1, 0, b"{}".to_vec())).unwrap();
    out.extend_from_slice(b"FOREIGN-EOF-EXTENSION");

    let updated = set_rpkx_chunk(
        &out,
        RpkxChunk::new(NS_A, *b"JSON", 2, 0, b"{\"v\":2}".to_vec()),
    )
    .unwrap();
    assert!(updated.ends_with(b"FOREIGN-EOF-EXTENSION"));

    let stripped = strip_rpkx(&updated).unwrap();
    let mut expected = base;
    expected.extend_from_slice(b"FOREIGN-EOF-EXTENSION");
    assert_eq!(stripped, expected);
}

#[test]
fn writer_refuses_to_overwrite_unrecognized_leading_tail() {
    let mut base = base_cache();
    base.extend_from_slice(b"OTHER");
    let error =
        set_rpkx_chunk(&base, RpkxChunk::new(NS_A, *b"DATA", 1, 0, Vec::new())).unwrap_err();
    assert!(error.to_string().contains("non-RPKX trailing bytes"));
}

#[test]
fn attach_requires_matching_source_stamp_by_default() {
    let base = base_cache();
    let stale = RpkxContainer::new(SourceStamp::new(1, 2));
    let error =
        attach_rpkx(&base, &stale, RpkxAttachPolicy::RequireMatchingSourceStamp).unwrap_err();
    assert!(error.to_string().contains("source stamp"));

    let out = attach_rpkx(&base, &stale, RpkxAttachPolicy::AllowSourceStampMismatch).unwrap();
    assert_eq!(
        read_rpkx(&out).unwrap().unwrap().source_stamp,
        SourceStamp::new(1, 2)
    );
}

#[test]
fn malformed_container_lengths_are_rejected() {
    let base = base_cache();
    let mut out =
        set_rpkx_chunk(&base, RpkxChunk::new(NS_A, *b"DATA", 1, 0, b"abc".to_vec())).unwrap();
    let start = base.len();
    let impossible = (out.len() as u64 + 100).to_le_bytes();
    out[start + 16..start + 24].copy_from_slice(&impossible);
    assert!(read_rpkx(&out).is_err());
}

#[test]
fn old_interleaved_v1_and_experimental_fixture_are_not_new_v1() {
    let stamp = SourceStamp::new(0x1234_5678, 0x0002_ee2c);

    let experimental = b"RPKX\x01\x00\x00\x00\x00\x00\x00\x00";
    assert!(RpkxContainer::parse(experimental).is_err());

    let mut old = Vec::new();
    old.extend_from_slice(b"RPKX");
    old.extend_from_slice(&1u16.to_le_bytes());
    old.extend_from_slice(&32u16.to_le_bytes());
    old.extend_from_slice(&0u32.to_le_bytes());
    old.extend_from_slice(&1u32.to_le_bytes());
    old.extend_from_slice(&(75u64).to_le_bytes());
    old.extend_from_slice(&stamp.mtime_low32.to_le_bytes());
    old.extend_from_slice(&stamp.size_low32.to_le_bytes());
    old.extend_from_slice(&NS_A);
    old.extend_from_slice(b"DATA");
    old.extend_from_slice(&1u32.to_le_bytes());
    old.extend_from_slice(&0u32.to_le_bytes());
    old.extend_from_slice(&0u32.to_le_bytes());
    old.extend_from_slice(&3u64.to_le_bytes());
    old.extend_from_slice(b"abc");
    assert_eq!(old.len(), 75);
    assert!(RpkxContainer::parse(&old).is_err());
}
