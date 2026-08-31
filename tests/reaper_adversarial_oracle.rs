use reapeaks::{generate_pcm16_mode3, GenerateOptions, ReaPeaks, Version};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

fn required_path(name: &str) -> PathBuf {
    env::var_os(name)
        .map(PathBuf::from)
        .unwrap_or_else(|| panic!("{name} must be set by the REAPER oracle workflow"))
}

fn read_pcm16(path: &Path) -> Vec<i16> {
    let bytes = fs::read(path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    assert_eq!(bytes.len() % 2, 0, "{} has odd byte length", path.display());
    bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|sample| i16::from_le_bytes(*sample))
        .collect()
}

fn first_difference(left: &[u8], right: &[u8]) -> Option<usize> {
    left.iter()
        .zip(right)
        .position(|(a, b)| a != b)
        .or_else(|| (left.len() != right.len()).then_some(left.len().min(right.len())))
}

fn hex_window(bytes: &[u8], center: usize) -> String {
    let start = center.saturating_sub(16);
    let end = center.saturating_add(16).min(bytes.len());
    bytes[start..end]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<Vec<_>>()
        .join("")
}

#[test]
#[ignore = "requires pinned REAPER, Xvfb and FFmpeg"]
fn reaper_mode3_pcm16_is_byte_identical_for_adversarial_case() {
    let pcm_path = required_path("REAPEAKS_PCM16");
    let source_path = required_path("REAPEAKS_SOURCE");
    let oracle_path = required_path("REAPEAKS_ORACLE");
    let output_path = required_path("LIBREAPEAKS_OUTPUT");

    let pcm = read_pcm16(&pcm_path);
    let oracle = fs::read(&oracle_path)
        .unwrap_or_else(|error| panic!("read {}: {error}", oracle_path.display()));
    let parsed = ReaPeaks::parse(oracle.clone())
        .unwrap_or_else(|error| panic!("parse {}: {error}", oracle_path.display()));

    assert_eq!(parsed.header.version, Version::Rpkn);
    let channels = usize::from(parsed.header.channels);
    assert!(channels > 0);
    assert_eq!(pcm.len() % channels, 0);

    let divisions: Vec<u32> = parsed
        .layer_headers
        .iter()
        .filter_map(|header| (header.division > 0).then_some(header.division as u32))
        .collect();
    assert!(!divisions.is_empty());

    let source_metadata = fs::metadata(&source_path)
        .unwrap_or_else(|error| panic!("stat {}: {error}", source_path.display()));
    assert_eq!(
        parsed.header.source_size_low32,
        source_metadata.len() as u32
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        assert_eq!(
            parsed.header.source_mtime_low32,
            source_metadata.mtime() as u32
        );
    }

    let options = GenerateOptions {
        sample_rate: parsed.header.sample_rate,
        channels,
        divisions,
        source_mtime_low32: parsed.header.source_mtime_low32,
        source_size_low32: parsed.header.source_size_low32,
        spectral: true,
    };
    let generated = generate_pcm16_mode3(&pcm, &options).expect("generate mode-3 cache");
    fs::write(&output_path, &generated)
        .unwrap_or_else(|error| panic!("write {}: {error}", output_path.display()));

    if let Some(offset) = first_difference(&oracle, &generated) {
        panic!(
            "whole-file mismatch at byte {offset}: sample_rate={} channels={} frames={} oracle_len={} generated_len={} oracle_window={} generated_window={}",
            parsed.header.sample_rate,
            channels,
            pcm.len() / channels,
            oracle.len(),
            generated.len(),
            hex_window(&oracle, offset),
            hex_window(&generated, offset),
        );
    }

    println!(
        "ADVERSARIAL_BYTE_IDENTICAL sample_rate={} channels={} frames={} bytes={} layers={}",
        parsed.header.sample_rate,
        channels,
        pcm.len() / channels,
        oracle.len(),
        parsed.layer_headers.len(),
    );
}
