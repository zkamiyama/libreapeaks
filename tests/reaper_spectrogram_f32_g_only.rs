use reapeaks::{
    format::TOKEN_SPECTROGRAM, generate_f32_mode3_with_spectrogram, GenerateOptions, ReaPeaks,
    Version, SPECTROGRAM_WORDS_PER_CHANNEL_FRAME,
};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

fn required_path(name: &str) -> PathBuf {
    env::var_os(name)
        .map(PathBuf::from)
        .unwrap_or_else(|| panic!("{name} must be set by the REAPER float spectrogram workflow"))
}

fn read_f32(path: &Path) -> Vec<f32> {
    let bytes = fs::read(path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    assert_eq!(bytes.len() % 4, 0);
    bytes
        .as_chunks::<4>()
        .0
        .iter()
        .map(|sample| f32::from_le_bytes(*sample))
        .collect()
}

fn raw_g_layers(bytes: &[u8], parsed: &ReaPeaks) -> Vec<(u32, Vec<u8>)> {
    assert_eq!(parsed.header.version, Version::Rpkl);
    let channels = usize::from(parsed.header.channels);
    let table_bytes = parsed.layer_headers.len() * 8;
    let mut offset = 18 + table_bytes;
    let mut output = Vec::new();
    for header in &parsed.layer_headers {
        let size = header.peak_count as usize * channels * 4;
        let end = offset + size;
        let payload = bytes
            .get(offset..end)
            .unwrap_or_else(|| panic!("truncated layer division={}", header.division));
        if header.division == TOKEN_SPECTROGRAM {
            output.push((header.peak_count, payload.to_vec()));
        }
        offset = end;
    }
    assert_eq!(offset, bytes.len());
    output
}

#[test]
#[ignore = "requires pinned REAPER and Xvfb"]
fn reaper779_f32_rpkl_g_payload_is_byte_identical() {
    let pcm_path = required_path("REAPEAKS_F32");
    let oracle_path = required_path("REAPEAKS_ORACLE");
    let output_path = required_path("LIBREAPEAKS_OUTPUT");

    let pcm = read_f32(&pcm_path);
    let oracle = fs::read(&oracle_path)
        .unwrap_or_else(|error| panic!("read {}: {error}", oracle_path.display()));
    let oracle_parsed = ReaPeaks::parse(oracle.clone()).expect("parse REAPER RPKL oracle");
    assert_eq!(oracle_parsed.header.version, Version::Rpkl);

    let channels = usize::from(oracle_parsed.header.channels);
    assert!(channels > 0);
    assert_eq!(pcm.len() % channels, 0);
    let divisions: Vec<u32> = oracle_parsed
        .layer_headers
        .iter()
        .filter_map(|header| (header.division > 0).then_some(header.division as u32))
        .collect();
    assert!(divisions.len() >= 3);

    let options = GenerateOptions {
        sample_rate: oracle_parsed.header.sample_rate,
        channels,
        divisions,
        source_mtime_low32: oracle_parsed.header.source_mtime_low32,
        source_size_low32: oracle_parsed.header.source_size_low32,
        spectral: true,
    };
    let generated = generate_f32_mode3_with_spectrogram(&pcm, &options, true)
        .expect("generate float mode-3 RPKL cache with spectrogram");
    fs::write(&output_path, &generated).expect("write generated RPKL cache");
    let generated_parsed = ReaPeaks::parse(generated.clone()).expect("parse generated RPKL cache");

    assert_eq!(
        oracle_parsed.spectrogram_layers.len(),
        generated_parsed.spectrogram_layers.len(),
        "f32 spectrogram layer count differs"
    );
    for (level, (expected, actual)) in oracle_parsed
        .spectrogram_layers
        .iter()
        .zip(&generated_parsed.spectrogram_layers)
        .enumerate()
    {
        assert_eq!(
            expected.mirrored_division, actual.mirrored_division,
            "f32 spectrogram mirrored division differs at level {level}"
        );
        assert_eq!(
            expected.frames, actual.frames,
            "f32 decoded spectrogram bins differ at level {level}"
        );
    }

    let oracle_g = raw_g_layers(&oracle, &oracle_parsed);
    let generated_g = raw_g_layers(&generated, &generated_parsed);
    assert_eq!(oracle_g.len(), generated_g.len());
    for (level, ((expected_count, expected), (actual_count, actual))) in
        oracle_g.iter().zip(&generated_g).enumerate()
    {
        assert_eq!(expected_count, actual_count, "g word count differs at level {level}");
        assert_eq!(
            *expected_count as usize % SPECTROGRAM_WORDS_PER_CHANNEL_FRAME,
            0
        );
        assert_eq!(expected, actual, "packed f32 g payload differs at level {level}");
    }

    println!(
        "F32_G_BYTE_IDENTICAL sample_rate={} channels={} source_frames={} g_layers={} g_words={:?}",
        oracle_parsed.header.sample_rate,
        channels,
        pcm.len() / channels,
        oracle_g.len(),
        oracle_g.iter().map(|(count, _)| *count).collect::<Vec<_>>()
    );
}
