use reapeaks::{
    format::TOKEN_SPECTROGRAM, generate_pcm16_mode3_with_spectrogram, GenerateOptions, ReaPeaks,
    SpectrogramFrame, Version, SPECTROGRAM_WORDS_PER_CHANNEL_FRAME,
};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

fn required_path(name: &str) -> PathBuf {
    env::var_os(name)
        .map(PathBuf::from)
        .unwrap_or_else(|| panic!("{name} must be set by the REAPER spectrogram exact workflow"))
}

fn read_pcm16(path: &Path) -> Vec<i16> {
    let bytes = fs::read(path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    assert_eq!(bytes.len() % 2, 0, "{} has odd byte length", path.display());
    bytes
        .chunks_exact(2)
        .map(|sample| i16::from_le_bytes([sample[0], sample[1]]))
        .collect()
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct RawLayer {
    division: i32,
    count: u32,
    payload: Vec<u8>,
}

fn split_rpkn_layers(bytes: &[u8], parsed: &ReaPeaks) -> Vec<RawLayer> {
    assert_eq!(parsed.header.version, Version::Rpkn);
    let channels = usize::from(parsed.header.channels);
    let table_bytes = parsed.layer_headers.len() * 8;
    let mut offset = 18 + table_bytes;
    let mut layers = Vec::with_capacity(parsed.layer_headers.len());
    for header in &parsed.layer_headers {
        let size = header.peak_count as usize * channels * 4;
        let end = offset + size;
        let payload = bytes
            .get(offset..end)
            .unwrap_or_else(|| panic!("truncated layer division={}", header.division))
            .to_vec();
        layers.push(RawLayer {
            division: header.division,
            count: header.peak_count,
            payload,
        });
        offset = end;
    }
    assert_eq!(offset, bytes.len(), "unconsumed bytes after RPKN layers");
    layers
}

fn compare_spectrogram_frames(
    level: usize,
    channels: usize,
    oracle: &[SpectrogramFrame],
    generated: &[SpectrogramFrame],
) {
    assert_eq!(
        oracle.len(),
        generated.len(),
        "spectrogram frame record count differs at level {level}"
    );
    let mut mismatch_count = 0usize;
    let mut max_abs_delta = 0i32;
    let mut signed_delta_sum = 0i64;
    let mut first = None;

    for (record, (expected, actual)) in oracle.iter().zip(generated).enumerate() {
        for bin in 0..expected.bins.len() {
            let expected_code = i32::from(expected.bins[bin]);
            let actual_code = i32::from(actual.bins[bin]);
            let delta = actual_code - expected_code;
            if delta != 0 {
                mismatch_count += 1;
                max_abs_delta = max_abs_delta.max(delta.abs());
                signed_delta_sum += i64::from(delta);
                first.get_or_insert((record, bin, expected_code, actual_code, delta));
            }
        }
    }

    let total = oracle.len() * 128;
    println!(
        "SPECTROGRAM_EXACT_STATS level={level} values={total} mismatches={mismatch_count} max_abs_delta={max_abs_delta} signed_delta_sum={signed_delta_sum}"
    );
    if let Some((record, bin, expected, actual, delta)) = first {
        let time = record / channels;
        let channel = record % channels;
        panic!(
            "spectrogram mismatch level={level} time={time} channel={channel} bin={bin} oracle={expected} generated={actual} delta={delta} mismatches={mismatch_count}/{total} max_abs_delta={max_abs_delta} signed_delta_sum={signed_delta_sum}"
        );
    }
}

#[test]
#[ignore = "requires pinned REAPER and Xvfb"]
fn reaper779_pcm16_spectrogram_is_byte_identical() {
    let pcm_path = required_path("REAPEAKS_PCM16");
    let oracle_path = required_path("REAPEAKS_ORACLE");
    let output_path = required_path("LIBREAPEAKS_OUTPUT");

    let pcm = read_pcm16(&pcm_path);
    let oracle = fs::read(&oracle_path)
        .unwrap_or_else(|error| panic!("read {}: {error}", oracle_path.display()));
    let oracle_parsed = ReaPeaks::parse(oracle.clone())
        .unwrap_or_else(|error| panic!("parse {}: {error}", oracle_path.display()));
    assert_eq!(oracle_parsed.header.version, Version::Rpkn);

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
    let generated = generate_pcm16_mode3_with_spectrogram(&pcm, &options)
        .expect("generate mode-3 cache with spectrogram");
    fs::write(&output_path, &generated)
        .unwrap_or_else(|error| panic!("write {}: {error}", output_path.display()));
    let generated_parsed = ReaPeaks::parse(generated.clone()).expect("parse generated cache");

    assert_eq!(oracle_parsed.header, generated_parsed.header, "RPKN header differs");
    assert_eq!(
        oracle_parsed.layer_headers, generated_parsed.layer_headers,
        "layer header table differs"
    );

    let oracle_layers = split_rpkn_layers(&oracle, &oracle_parsed);
    let generated_layers = split_rpkn_layers(&generated, &generated_parsed);
    assert_eq!(oracle_layers.len(), generated_layers.len());
    for (index, (expected, actual)) in oracle_layers.iter().zip(&generated_layers).enumerate() {
        assert_eq!(expected.division, actual.division, "layer {index} division differs");
        assert_eq!(expected.count, actual.count, "layer {index} count differs");
        if expected.division != TOKEN_SPECTROGRAM {
            assert_eq!(expected.payload, actual.payload, "non-spectrogram layer {index} differs");
        }
    }

    assert_eq!(
        oracle_parsed.spectrogram_layers.len(),
        generated_parsed.spectrogram_layers.len(),
        "spectrogram layer count differs"
    );
    for (level, (expected, actual)) in oracle_parsed
        .spectrogram_layers
        .iter()
        .zip(&generated_parsed.spectrogram_layers)
        .enumerate()
    {
        assert_eq!(expected.mirrored_division, actual.mirrored_division);
        compare_spectrogram_frames(level, channels, &expected.frames, &actual.frames);
    }

    for (level, (expected, actual)) in oracle_layers
        .iter()
        .filter(|layer| layer.division == TOKEN_SPECTROGRAM)
        .zip(
            generated_layers
                .iter()
                .filter(|layer| layer.division == TOKEN_SPECTROGRAM),
        )
        .enumerate()
    {
        assert_eq!(
            expected.count as usize % SPECTROGRAM_WORDS_PER_CHANNEL_FRAME,
            0
        );
        assert_eq!(
            expected.payload, actual.payload,
            "packed spectrogram payload differs at level {level} despite equal decoded bins"
        );
    }

    assert_eq!(oracle, generated, "whole RPKN file differs after layer checks");
    println!(
        "SPECTROGRAM_BYTE_IDENTICAL sample_rate={} channels={} source_frames={} bytes={} g_frames={:?}",
        oracle_parsed.header.sample_rate,
        channels,
        pcm.len() / channels,
        oracle.len(),
        oracle_parsed
            .spectrogram_layers
            .iter()
            .map(|layer| layer.frame_count(channels))
            .collect::<Vec<_>>()
    );
}
