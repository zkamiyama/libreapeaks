use reapeaks::format::{encode, GeneratedLayer, LayerHeader, Version};
use reapeaks::{
    append_rpkx_chunk, append_rpkx_chunk_file, read_rpkx, remove_rpkx_chunks,
    remove_rpkx_chunks_file, rpkx_file_lock_path, set_rpkx_chunk, set_rpkx_chunk_file, strip_rpkx,
    strip_rpkx_file, RpkxChunk, RpkxKey,
};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

const NS_A: [u8; 16] = [
    0x10, 0x7a, 0x92, 0x8e, 0x49, 0x02, 0x4c, 0x62, 0xa8, 0x27, 0xa0, 0x29, 0x83, 0xee, 0x11, 0x01,
];
const NS_B: [u8; 16] = [
    0x95, 0xf3, 0x4d, 0x20, 0x6c, 0x91, 0x4a, 0x0a, 0xb7, 0xa9, 0x8d, 0x75, 0xec, 0xc0, 0x02, 0xb4,
];
static CASE_COUNTER: AtomicU64 = AtomicU64::new(0);

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

struct TestDir(PathBuf);

impl TestDir {
    fn new(name: &str) -> Self {
        let id = CASE_COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "libreapeaks-rpkx-file-{}-{name}-{id}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        Self(path)
    }

    fn cache(&self) -> PathBuf {
        self.0.join("source.wav.reapeaks")
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn write(path: &Path, bytes: &[u8]) {
    fs::write(path, bytes).unwrap();
}

#[test]
fn file_set_matches_byte_editor_and_keeps_lock_sidecar_stable() {
    let dir = TestDir::new("set");
    let path = dir.cache();
    let base = base_cache();
    write(&path, &base);

    let chunk = RpkxChunk::new(NS_A, *b"CHRD", 3, 0x20, b"opaque chord bytes".to_vec());
    let expected = set_rpkx_chunk(&base, chunk.clone()).unwrap();
    let report = set_rpkx_chunk_file(&path, chunk).unwrap();

    assert!(report.changed);
    assert_eq!(report.old_file_len, base.len() as u64);
    assert_eq!(report.new_file_len, expected.len() as u64);
    assert_eq!(fs::read(&path).unwrap(), expected);
    assert!(rpkx_file_lock_path(&path).is_file());
}

#[test]
fn file_set_replaces_duplicate_keys_exactly_like_owning_api() {
    let dir = TestDir::new("duplicates");
    let path = dir.cache();
    let base = base_cache();
    let key = RpkxKey::new(NS_A, *b"MARK");
    let bytes = append_rpkx_chunk(
        &append_rpkx_chunk(
            &base,
            RpkxChunk::new(NS_A, *b"MARK", 1, 0, b"one".to_vec()),
        )
        .unwrap(),
        RpkxChunk::new(NS_A, *b"MARK", 2, 0, b"two".to_vec()),
    )
    .unwrap();
    write(&path, &bytes);

    let replacement = RpkxChunk::new(NS_A, *b"MARK", 9, 4, b"replacement".to_vec());
    let expected = set_rpkx_chunk(&bytes, replacement.clone()).unwrap();
    set_rpkx_chunk_file(&path, replacement).unwrap();
    let actual = fs::read(&path).unwrap();

    assert_eq!(actual, expected);
    assert_eq!(
        read_rpkx(&actual)
            .unwrap()
            .unwrap()
            .chunks_for(key)
            .count(),
        1
    );
}

#[test]
fn unchanged_large_payload_is_streamed_without_materializing_it() {
    let dir = TestDir::new("large");
    let path = dir.cache();
    let base = base_cache();
    let big = vec![0x5a; 8 * 1024 * 1024];
    let initial =
        set_rpkx_chunk(&base, RpkxChunk::new(NS_A, *b"BIG_", 1, 0, big)).unwrap();
    write(&path, &initial);

    let small = RpkxChunk::new(NS_B, *b"SMOL", 1, 0, b"ok".to_vec());
    let expected = set_rpkx_chunk(&initial, small.clone()).unwrap();
    let report = set_rpkx_chunk_file(&path, small).unwrap();

    assert_eq!(fs::read(&path).unwrap(), expected);
    assert_eq!(report.payload_bytes_written, 2);
    assert!(report.preserved_source_bytes() >= 8 * 1024 * 1024);
}

#[test]
fn same_size_set_uses_whole_file_std_copy_then_patches() {
    let dir = TestDir::new("same-size");
    let path = dir.cache();
    let base = base_cache();
    let initial = set_rpkx_chunk(
        &base,
        RpkxChunk::new(NS_A, *b"DATA", 1, 0, b"12345678".to_vec()),
    )
    .unwrap();
    write(&path, &initial);

    let replacement = RpkxChunk::new(NS_A, *b"DATA", 2, 7, b"ABCDEFGH".to_vec());
    let expected = set_rpkx_chunk(&initial, replacement.clone()).unwrap();
    let report = set_rpkx_chunk_file(&path, replacement).unwrap();

    assert_eq!(fs::read(&path).unwrap(), expected);
    assert_eq!(report.old_file_len, report.new_file_len);
    assert_eq!(report.source_bytes_copied, initial.len() as u64);
    assert_eq!(report.payload_bytes_written, 8);
    assert_eq!(report.metadata_bytes_written, 8);
}

#[test]
fn append_remove_and_strip_match_byte_editors_and_preserve_suffix() {
    let dir = TestDir::new("mutations");
    let path = dir.cache();
    let base = base_cache();
    let first = RpkxChunk::new(NS_A, *b"AAAA", 1, 0, b"alpha".to_vec());
    let second = RpkxChunk::new(NS_B, *b"BBBB", 2, 1, b"beta".to_vec());

    let mut bytes = set_rpkx_chunk(&base, first).unwrap();
    bytes.extend_from_slice(b"FOREIGN-SUFFIX");
    write(&path, &bytes);

    let expected_append = append_rpkx_chunk(&bytes, second.clone()).unwrap();
    append_rpkx_chunk_file(&path, second).unwrap();
    assert_eq!(fs::read(&path).unwrap(), expected_append);

    let key = RpkxKey::new(NS_A, *b"AAAA");
    let expected_remove = remove_rpkx_chunks(&expected_append, key).unwrap();
    remove_rpkx_chunks_file(&path, key).unwrap();
    assert_eq!(fs::read(&path).unwrap(), expected_remove);

    let expected_strip = strip_rpkx(&expected_remove).unwrap();
    strip_rpkx_file(&path).unwrap();
    assert_eq!(fs::read(&path).unwrap(), expected_strip);
    assert!(expected_strip.ends_with(b"FOREIGN-SUFFIX"));
}

#[test]
fn remove_missing_key_is_a_noop_without_replacing_file() {
    let dir = TestDir::new("noop");
    let path = dir.cache();
    let base = base_cache();
    write(&path, &base);
    let before = fs::metadata(&path).unwrap();

    let report = remove_rpkx_chunks_file(&path, RpkxKey::new(NS_A, *b"NONE")).unwrap();
    let after = fs::metadata(&path).unwrap();

    assert!(!report.changed);
    assert_eq!(fs::read(&path).unwrap(), base);
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        assert_eq!(before.ino(), after.ino());
    }
}

#[test]
fn file_updater_refuses_foreign_bytes_before_rpkx() {
    let dir = TestDir::new("foreign-leading");
    let path = dir.cache();
    let mut bytes = base_cache();
    bytes.extend_from_slice(b"OTHER");
    write(&path, &bytes);

    let error =
        set_rpkx_chunk_file(&path, RpkxChunk::new(NS_A, *b"DATA", 1, 0, Vec::new()))
            .unwrap_err();
    assert!(error.to_string().contains("non-RPKX trailing bytes"));
    assert_eq!(fs::read(&path).unwrap(), bytes);
}
