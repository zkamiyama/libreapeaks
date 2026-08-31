use reapeaks::{default_divisions, generate_pcm16_mode3, GenerateOptions, ReaPeaks, Version};
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
    assert_eq!(bytes.len() % 2, 0, "{} has an odd byte count", path.display());
    let mut pcm = Vec::with_capacity(bytes.len() / 2);
    for sample in bytes.chunks(2) {
        pcm.push(i16::from_le_bytes([sample[0], sample[1]]));
    }
    pcm
}

fn fnv64(bytes: &[u8]) -> u64 {
    let mut hash = 0xcbf29ce484222325u64;
    for &byte in bytes {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
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
#[ignore = "requires pinned REAPER 7.79, Xvfb, and FFmpeg"]
fn reaper779_video_alac_mode3_is_byte_identical() {
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
    assert_eq!(parsed.header.channels, 2);
    assert_eq!(parsed.header.sample_rate, 48_000);

    let expected_divisions = default_divisions(parsed.header.sample_rate, 300);
    let divisions: Vec<u32> = parsed
        .layer_headers
        .iter()
        .filter_map(|header| (header.division > 0).then_some(header.division as u32))
        .collect();
    assert_eq!(divisions, expected_divisions.to_vec());

    let layout: Vec<(i32, u32)> = parsed
        .layer_headers
        .iter()
        .map(|header| (header.division, header.peak_count))
        .collect();
    assert_eq!(
        layout,
        [
            (160, 900),
            (2400, 60),
            (48_000, 3),
            (-115, 893),
            (-115, 59),
            (-115, 2),
            (-114, 120),
            (-114, 6),
        ]
        .to_vec()
    );

    let source_metadata = fs::metadata(&source_path)
        .unwrap_or_else(|error| panic!("stat {}: {error}", source_path.display()));
    assert_eq!(
        parsed.header.source_size_low32,
        source_metadata.len() as u32,
        "REAPER source-size header did not match the ALAC file"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        assert_eq!(
            parsed.header.source_mtime_low32,
            source_metadata.mtime() as u32,
            "REAPER source-mtime header did not match the ALAC file"
        );
    }

    let channels = usize::from(parsed.header.channels);
    assert_eq!(pcm.len() % channels, 0);
    assert_eq!(pcm.len() / channels, 144_000);

    let options = GenerateOptions {
        sample_rate: parsed.header.sample_rate,
        channels,
        divisions,
        source_mtime_low32: parsed.header.source_mtime_low32,
        source_size_low32: parsed.header.source_size_low32,
        spectral: true,
    };
    let generated = generate_pcm16_mode3(&pcm, &options).expect("generate complete mode-3 file");
    fs::write(&output_path, &generated)
        .unwrap_or_else(|error| panic!("write {}: {error}", output_path.display()));

    if let Some(offset) = first_difference(&oracle, &generated) {
        panic!(
            "whole-file mismatch at byte {offset}: oracle_len={} generated_len={} \
             oracle_fnv64={:016x} generated_fnv64={:016x} oracle_window={} generated_window={}",
            oracle.len(),
            generated.len(),
            fnv64(&oracle),
            fnv64(&generated),
            hex_window(&oracle, offset),
            hex_window(&generated, offset),
        );
    }

    println!(
        "REAPER_FFMPEG_BYTE_IDENTICAL bytes={} fnv64={:016x} layers={} wave={} spectral={} loudness={}",
        oracle.len(),
        fnv64(&oracle),
        parsed.layer_headers.len(),
        parsed.wave_layers.len(),
        parsed.spectral_layers.len(),
        parsed.loudness_layers.len(),
    );
}
