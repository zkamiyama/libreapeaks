use reapeaks::{
    format::TOKEN_SPECTROGRAM, generate_pcm16_mode3, GenerateOptions, ReaPeaks, Version,
    SPECTROGRAM_WORDS_PER_CHANNEL_FRAME,
};
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

#[derive(Debug, Clone, PartialEq, Eq)]
struct RawLayer {
    division: i32,
    count: u32,
    payload: Vec<u8>,
}

fn split_rpkn_layers(bytes: &[u8], parsed: &ReaPeaks) -> Vec<RawLayer> {
    assert_eq!(parsed.header.version, Version::Rpkn);
    let channels = usize::from(parsed.header.channels);
    let table_bytes = parsed
        .layer_headers
        .len()
        .checked_mul(8)
        .expect("layer table byte count");
    let mut offset = 18usize.checked_add(table_bytes).expect("layer table end");
    let mut layers = Vec::with_capacity(parsed.layer_headers.len());

    for header in &parsed.layer_headers {
        // Every RPKN payload represented by the current parser uses four bytes
        // for each header count unit and channel. For -'g' that count unit is a
        // 32-bit word, not a 192-byte time frame.
        let size = (header.peak_count as usize)
            .checked_mul(channels)
            .and_then(|value| value.checked_mul(4))
            .expect("RPKN layer payload byte count");
        let end = offset.checked_add(size).expect("RPKN layer payload end");
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

fn assert_spectrogram_extension(
    oracle: &[u8],
    oracle_parsed: &ReaPeaks,
    generated: &[u8],
    generated_parsed: &ReaPeaks,
) {
    let channels = usize::from(oracle_parsed.header.channels);
    let oracle_layers = split_rpkn_layers(oracle, oracle_parsed);
    let generated_layers = split_rpkn_layers(generated, generated_parsed);
    let oracle_without_spectrogram: Vec<RawLayer> = oracle_layers
        .iter()
        .filter(|layer| layer.division != TOKEN_SPECTROGRAM)
        .cloned()
        .collect();

    assert_eq!(
        oracle_without_spectrogram, generated_layers,
        "all pre-existing mode-3 layers must stay byte-identical when REAPER adds -'g'"
    );

    assert_eq!(
        oracle_parsed.header.version,
        generated_parsed.header.version
    );
    assert_eq!(
        oracle_parsed.header.channels,
        generated_parsed.header.channels
    );
    assert_eq!(
        oracle_parsed.header.sample_rate,
        generated_parsed.header.sample_rate
    );
    assert_eq!(
        oracle_parsed.header.source_mtime_low32,
        generated_parsed.header.source_mtime_low32
    );
    assert_eq!(
        oracle_parsed.header.source_size_low32,
        generated_parsed.header.source_size_low32
    );

    let positive_divisions: Vec<u32> = oracle_parsed
        .layer_headers
        .iter()
        .filter_map(|header| (header.division > 0).then_some(header.division as u32))
        .collect();
    let spectrogram_headers: Vec<_> = oracle_parsed
        .layer_headers
        .iter()
        .filter(|header| header.division == TOKEN_SPECTROGRAM)
        .collect();

    assert_eq!(
        spectrogram_headers.len(),
        2,
        "REAPER 7.79 mode-3 -'g' layer count"
    );
    assert_eq!(
        oracle_parsed.spectrogram_layers.len(),
        spectrogram_headers.len()
    );
    assert!(positive_divisions.len() >= spectrogram_headers.len() + 1);

    for (index, header) in spectrogram_headers.iter().enumerate() {
        let words = header.peak_count as usize;
        assert_eq!(
            words % SPECTROGRAM_WORDS_PER_CHANNEL_FRAME,
            0,
            "-'g' count must be an integral number of 48-word channel frames"
        );
        let time_frames = words / SPECTROGRAM_WORDS_PER_CHANNEL_FRAME;
        let layer = &oracle_parsed.spectrogram_layers[index];
        assert_eq!(layer.frame_count(channels), time_frames);
        assert_eq!(layer.mirrored_division, positive_divisions[index + 1]);
    }

    println!(
        "ADVERSARIAL_SPECTROGRAM_EXTENSION_EXACT sample_rate={} channels={} old_layers={} g_layers={} g_words={:?} g_frames={:?}",
        oracle_parsed.header.sample_rate,
        channels,
        generated_parsed.layer_headers.len(),
        spectrogram_headers.len(),
        spectrogram_headers
            .iter()
            .map(|header| header.peak_count)
            .collect::<Vec<_>>(),
        spectrogram_headers
            .iter()
            .map(|header| header.peak_count as usize / SPECTROGRAM_WORDS_PER_CHANNEL_FRAME)
            .collect::<Vec<_>>(),
    );
}

// The workflow feeds this test REAPER-generated cases spanning exact and
// partial EOF buckets, non-48 kHz sample rates, peakcachegenrs variants, and
// mono/multichannel inputs. With normal REAPER display flags equality is whole
// file byte equality. With spectrogram display enabled, libreapeaks does not
// generate -'g' yet, so this gate requires every existing layer to remain byte
// exact and independently validates the observed REAPER -'g' structure.
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
    let generated_parsed =
        ReaPeaks::parse(generated.clone()).expect("parse generated mode-3 cache");

    let has_spectrogram = parsed
        .layer_headers
        .iter()
        .any(|header| header.division == TOKEN_SPECTROGRAM);
    if has_spectrogram {
        assert_spectrogram_extension(&oracle, &parsed, &generated, &generated_parsed);
        return;
    }

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
