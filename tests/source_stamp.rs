use reapeaks::format::{encode_rpkn, ReaPeaks};
use reapeaks::SourceStamp;
use std::fs;
use std::time::{SystemTime, UNIX_EPOCH};

fn temp_path(name: &str) -> std::path::PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("libreapeaks-{name}-{}-{nonce}", std::process::id()))
}

#[test]
fn source_stamp_reduces_unix_seconds_and_size_to_low32() {
    let stamp = SourceStamp::from_unix_seconds_and_size(0x1_2345_6789, 0x2_abcd_ef01);
    assert_eq!(stamp.mtime_low32, 0x2345_6789);
    assert_eq!(stamp.size_low32, 0xabcd_ef01);

    let negative = SourceStamp::from_unix_seconds_and_size(-1, 0);
    assert_eq!(negative.mtime_low32, u32::MAX);
}

#[test]
fn generated_header_stamp_matches_source_until_source_changes() {
    let source = temp_path("source-stamp-audio.bin");
    fs::write(&source, [1u8, 2, 3, 4, 5]).unwrap();

    let stamp = SourceStamp::from_path(&source).unwrap();
    assert_eq!(stamp.size_low32, 5);

    let blob = encode_rpkn(1, 48_000, stamp.mtime_low32, stamp.size_low32, &[]).unwrap();
    let parsed = ReaPeaks::parse(blob).unwrap();

    assert_eq!(parsed.source_stamp(), stamp);
    assert!(parsed.matches_source_stamp(stamp));
    assert!(parsed.matches_source_path(&source).unwrap());

    fs::write(&source, [1u8, 2, 3, 4, 5, 6]).unwrap();
    assert!(!parsed.matches_source_path(&source).unwrap());

    let _ = fs::remove_file(source);
}
