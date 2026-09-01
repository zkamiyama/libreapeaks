use reapeaks::{
    format::TOKEN_SPECTROGRAM, generate_pcm16_mode3_with_spectrogram, GenerateOptions, ReaPeaks,
    Version,
};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

fn required_path(name: &str) -> PathBuf {
    env::var_os(name)
        .map(PathBuf::from)
        .unwrap_or_else(|| panic!("{name} must be set by the REAPER spectrogram stress workflow"))
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

#[derive(Debug, Clone, PartialEq, Eq)]
struct RawSpectrogramLayer {
    count: u32,
    payload: Vec<u8>,
}

fn raw_spectrogram_layers(bytes: &[u8], parsed: &ReaPeaks) -> Vec<RawSpectrogramLayer> {
    assert_eq!(parsed.header.version, Version::Rpkn);
    let channels = usize::from(parsed.header.channels);
    let mut offset = 18 + parsed.layer_headers.len() * 8;
    let mut layers = Vec::new();
    for header in &parsed.layer_headers {
        let size = header.peak_count as usize * channels * 4;
        let end = offset
            .checked_add(size)
            .expect("RPKN stress layer offset overflow");
        let payload = bytes
            .get(offset..end)
            .unwrap_or_else(|| panic!("truncated layer division={}", header.division));
        if header.division == TOKEN_SPECTROGRAM {
            layers.push(RawSpectrogramLayer {
                count: header.peak_count,
                payload: payload.to_vec(),
            });
        }
        offset = end;
    }
    assert_eq!(offset, bytes.len(), "unconsumed RPKN stress bytes");
    layers
}

#[test]
#[ignore = "requires pinned REAPER stress oracle"]
fn reaper779_pcm16_default_fft_spectrogram_is_byte_identical() {
    let pcm_path = required_path("REAPEAKS_PCM16");
    let oracle_path = required_path("REAPEAKS_ORACLE");
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
        .expect("generate default-FFT mode-3 cache with spectrogram");
    if let Some(path) = env::var_os("LIBREAPEAKS_DEFAULT_OUTPUT").map(PathBuf::from) {
        fs::write(&path, &generated)
            .unwrap_or_else(|error| panic!("write {}: {error}", path.display()));
    }
    let generated_parsed = ReaPeaks::parse(generated.clone()).expect("parse default-FFT cache");

    assert_eq!(
        oracle_parsed.header.channels,
        generated_parsed.header.channels
    );
    assert_eq!(
        oracle_parsed.header.sample_rate,
        generated_parsed.header.sample_rate
    );
    assert_eq!(
        oracle_parsed.spectrogram_layers, generated_parsed.spectrogram_layers,
        "decoded default-FFT spectrogram differs"
    );

    let expected = raw_spectrogram_layers(&oracle, &oracle_parsed);
    let actual = raw_spectrogram_layers(&generated, &generated_parsed);
    assert_eq!(expected, actual, "packed default-FFT -103 payload differs");

    println!(
        "SPECTROGRAM_DEFAULT_G_BYTE_IDENTICAL sample_rate={} channels={} source_frames={} g_frames={:?}",
        oracle_parsed.header.sample_rate,
        channels,
        pcm.len() / channels,
        oracle_parsed
            .spectrogram_layers
            .iter()
            .map(|layer| layer.frame_count(channels))
            .collect::<Vec<_>>()
    );
}
