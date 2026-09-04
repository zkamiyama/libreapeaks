use reapeaks::format::{encode, GeneratedLayer, LayerHeader, Version};
use reapeaks::{
    append_rpkx_chunk, append_rpkx_chunk_file, read_rpkx, remove_rpkx_chunks,
    remove_rpkx_chunks_file, rpkx_file_lock_path, set_rpkx_chunk, set_rpkx_chunk_file, strip_rpkx,
    strip_rpkx_file, RpkxChunk, RpkxKey, RPKX_DIRECTORY_ENTRY_SIZE, RPKX_HEADER_SIZE,
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
const NS_C: [u8; 16] = [
    0x32, 0xa6, 0xc1, 0x70, 0x0f, 0xf2, 0x48, 0xe7, 0x92, 0x65, 0x5a, 0x39, 0x11, 0xa4, 0x6b, 0xcd,
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

fn patterned_payload(mut state: u64, len: usize) -> Vec<u8> {
    let mut payload = Vec::with_capacity(len);
    for _ in 0..len {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        payload.push(state as u8);
    }
    payload
}

fn assert_no_temp_artifacts(dir: &TestDir) {
    for entry in fs::read_dir(&dir.0).unwrap() {
        let entry = entry.unwrap();
        let name = entry.file_name();
        let name = name.to_string_lossy();
        assert!(
            !name.contains(".rpkx-tmp-"),
            "temporary updater artifact leaked: {name}"
        );
    }
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
    assert_no_temp_artifacts(&dir);
}

#[test]
fn file_set_replaces_duplicate_keys_exactly_like_owning_api() {
    let dir = TestDir::new("duplicates");
    let path = dir.cache();
    let base = base_cache();
    let key = RpkxKey::new(NS_A, *b"MARK");
    let bytes = append_rpkx_chunk(
        &append_rpkx_chunk(&base, RpkxChunk::new(NS_A, *b"MARK", 1, 0, b"one".to_vec())).unwrap(),
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
        read_rpkx(&actual).unwrap().unwrap().chunks_for(key).count(),
        1
    );
    assert_no_temp_artifacts(&dir);
}

#[test]
fn unchanged_large_payload_is_streamed_without_materializing_it() {
    let dir = TestDir::new("large");
    let path = dir.cache();
    let base = base_cache();
    let big = vec![0x5a; 8 * 1024 * 1024];
    let initial = set_rpkx_chunk(&base, RpkxChunk::new(NS_A, *b"BIG_", 1, 0, big)).unwrap();
    write(&path, &initial);

    let small = RpkxChunk::new(NS_B, *b"SMOL", 1, 0, b"ok".to_vec());
    let expected = set_rpkx_chunk(&initial, small.clone()).unwrap();
    let report = set_rpkx_chunk_file(&path, small).unwrap();

    assert_eq!(fs::read(&path).unwrap(), expected);
    assert_eq!(report.payload_bytes_written, 2);
    assert!(report.preserved_source_bytes() >= 8 * 1024 * 1024);
    assert_no_temp_artifacts(&dir);
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
    assert_no_temp_artifacts(&dir);
}

#[test]
fn zero_length_same_size_update_and_growth_shrink_round_trip() {
    let dir = TestDir::new("zero-size-oscillation");
    let path = dir.cache();
    let base = base_cache();
    let mut expected = set_rpkx_chunk(
        &base,
        RpkxChunk::new(NS_A, *b"ZERO", 1, 0, Vec::new()),
    )
    .unwrap();
    write(&path, &expected);

    let zero = RpkxChunk::new(NS_A, *b"ZERO", 2, 0x55, Vec::new());
    expected = set_rpkx_chunk(&expected, zero.clone()).unwrap();
    let report = set_rpkx_chunk_file(&path, zero).unwrap();
    assert_eq!(report.old_file_len, report.new_file_len);
    assert_eq!(report.payload_bytes_written, 0);
    assert_eq!(report.metadata_bytes_written, 8);
    assert_eq!(fs::read(&path).unwrap(), expected);

    for (version, len) in [(3, 1usize), (4, 4097), (5, 0), (6, 8193), (7, 7)] {
        let chunk = RpkxChunk::new(
            NS_A,
            *b"ZERO",
            version,
            version ^ 0xa5,
            patterned_payload(version as u64 * 0x9e37_79b9, len),
        );
        expected = set_rpkx_chunk(&expected, chunk.clone()).unwrap();
        set_rpkx_chunk_file(&path, chunk).unwrap();
        assert_eq!(fs::read(&path).unwrap(), expected);
    }
    assert_no_temp_artifacts(&dir);
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
    assert_no_temp_artifacts(&dir);
}

#[test]
fn deterministic_adversarial_mutation_sequence_matches_owning_editor() {
    const KINDS: [[u8; 4]; 4] = [*b"K001", *b"K002", *b"K003", *b"K004"];
    const LENGTHS: [usize; 10] = [0, 1, 2, 7, 31, 255, 4095, 4096, 4097, 16 * 1024 + 3];
    const SUFFIX: &[u8] = b"FOREIGN\0SUFFIX\xff";

    let dir = TestDir::new("adversarial-sequence");
    let path = dir.cache();
    let base = base_cache();
    let anchor = RpkxChunk::new(NS_B, *b"KEEP", 1, 0, b"anchor".to_vec());
    let mut expected = set_rpkx_chunk(&base, anchor).unwrap();
    expected.extend_from_slice(SUFFIX);
    write(&path, &expected);

    let mut state = 0xd1b5_4a32_d192_ed03u64;
    for step in 0..96u32 {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        let namespace = if state & 1 == 0 { NS_A } else { NS_C };
        let kind = KINDS[((state >> 3) as usize) % KINDS.len()];
        let key = RpkxKey::new(namespace, kind);
        let operation = ((state >> 9) % 3) as u8;
        let len = LENGTHS[((state >> 13) as usize) % LENGTHS.len()];
        let payload = patterned_payload(state ^ step as u64, len);
        let chunk = RpkxChunk::new(namespace, kind, step + 1, state as u32, payload);

        match operation {
            0 => {
                expected = set_rpkx_chunk(&expected, chunk.clone()).unwrap();
                set_rpkx_chunk_file(&path, chunk).unwrap();
            }
            1 => {
                expected = append_rpkx_chunk(&expected, chunk.clone()).unwrap();
                append_rpkx_chunk_file(&path, chunk).unwrap();
            }
            _ => {
                expected = remove_rpkx_chunks(&expected, key).unwrap();
                remove_rpkx_chunks_file(&path, key).unwrap();
            }
        }

        let actual = fs::read(&path).unwrap();
        assert_eq!(actual, expected, "byte mismatch after mutation step {step}");
        assert!(actual.ends_with(SUFFIX), "suffix lost at mutation step {step}");
        assert_no_temp_artifacts(&dir);
    }

    let stripped = strip_rpkx(&expected).unwrap();
    strip_rpkx_file(&path).unwrap();
    assert_eq!(fs::read(&path).unwrap(), stripped);
    assert!(stripped.ends_with(SUFFIX));
    assert_no_temp_artifacts(&dir);
}

#[test]
fn malformed_containers_are_rejected_without_touching_original_or_leaking_temp() {
    let base = base_cache();
    let valid = set_rpkx_chunk(
        &base,
        RpkxChunk::new(NS_A, *b"DATA", 1, 0, b"payload".to_vec()),
    )
    .unwrap();
    let start = base.len();
    let first_entry = start + RPKX_HEADER_SIZE;

    let mut cases = Vec::new();

    let mut bad_header_size = valid.clone();
    bad_header_size[start + 6..start + 8].copy_from_slice(&31u16.to_le_bytes());
    cases.push(("header-size", bad_header_size));

    let mut bad_reserved = valid.clone();
    bad_reserved[first_entry + 28..first_entry + 32].copy_from_slice(&1u32.to_le_bytes());
    cases.push(("reserved", bad_reserved));

    let mut bad_payload_offset = valid.clone();
    bad_payload_offset[first_entry + 32..first_entry + 40].copy_from_slice(&0u64.to_le_bytes());
    cases.push(("payload-offset", bad_payload_offset));

    let mut bad_container_len = valid.clone();
    let impossible_len = (valid.len() - start) as u64 + 4096;
    bad_container_len[start + 16..start + 24].copy_from_slice(&impossible_len.to_le_bytes());
    cases.push(("container-len", bad_container_len));

    let mut bad_source_stamp = valid.clone();
    bad_source_stamp[start + 24..start + 28].copy_from_slice(&0xdead_beefu32.to_le_bytes());
    cases.push(("source-stamp", bad_source_stamp));

    assert_eq!(RPKX_DIRECTORY_ENTRY_SIZE, 48);
    for (name, damaged) in cases {
        let dir = TestDir::new(name);
        let path = dir.cache();
        write(&path, &damaged);

        let result = set_rpkx_chunk_file(
            &path,
            RpkxChunk::new(NS_C, *b"EVIL", 9, 0, b"should-not-land".to_vec()),
        );
        assert!(result.is_err(), "damaged case {name} unexpectedly updated");
        assert_eq!(
            fs::read(&path).unwrap(),
            damaged,
            "damaged case {name} modified the original file"
        );
        assert_no_temp_artifacts(&dir);
    }
}

#[test]
fn remove_missing_key_is_a_noop_without_replacing_file() {
    let dir = TestDir::new("noop");
    let path = dir.cache();
    let base = base_cache();
    write(&path, &base);
    #[cfg(unix)]
    let before = fs::metadata(&path).unwrap();

    let report = remove_rpkx_chunks_file(&path, RpkxKey::new(NS_A, *b"NONE")).unwrap();
    #[cfg(unix)]
    let after = fs::metadata(&path).unwrap();

    assert!(!report.changed);
    assert_eq!(fs::read(&path).unwrap(), base);
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        assert_eq!(before.ino(), after.ino());
    }
    assert_no_temp_artifacts(&dir);
}

#[test]
fn file_updater_refuses_foreign_bytes_before_rpkx() {
    let dir = TestDir::new("foreign-leading");
    let path = dir.cache();
    let mut bytes = base_cache();
    bytes.extend_from_slice(b"OTHER");
    write(&path, &bytes);

    let error =
        set_rpkx_chunk_file(&path, RpkxChunk::new(NS_A, *b"DATA", 1, 0, Vec::new())).unwrap_err();
    assert!(error.to_string().contains("non-RPKX trailing bytes"));
    assert_eq!(fs::read(&path).unwrap(), bytes);
    assert_no_temp_artifacts(&dir);
}
