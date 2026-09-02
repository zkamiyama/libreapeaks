use reapeaks::format::{encode, GeneratedLayer, LayerHeader, Version};
use reapeaks::{
    append_rpkx_chunk, attach_rpkx, read_rpkx, remove_rpkx_chunks, set_rpkx_chunk, standard_end,
    strip_rpkx, ReaPeaks, RpkxAttachPolicy, RpkxChunk, RpkxContainer, RpkxKey, SourceStamp,
    RPKX_HEADER_SIZE,
};

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
