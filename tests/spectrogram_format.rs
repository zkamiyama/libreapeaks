use reapeaks::{
    decode_spectrogram_frame, parse_spectrogram_layers, ReaPeaks, ReaPeaksError, SPECTROGRAM_BINS,
    SPECTROGRAM_BYTES_PER_CHANNEL_FRAME, SPECTROGRAM_WORDS_PER_CHANNEL_FRAME,
};

fn encode_frame(bins: &[u16; SPECTROGRAM_BINS]) -> Vec<u8> {
    let mut out = Vec::with_capacity(SPECTROGRAM_BYTES_PER_CHANNEL_FRAME);
    for pair in 0..(SPECTROGRAM_BINS / 2) {
        let first = bins[pair * 2] & 0x0fff;
        let second = bins[pair * 2 + 1] & 0x0fff;
        out.push((first >> 4) as u8);
        out.push((((first & 0x0f) << 4) | (second & 0x0f)) as u8);
        out.push((second >> 4) as u8);
    }
    out
}

fn synthetic_file(first_g_count: u32, truncate: usize) -> Vec<u8> {
    let channels = 2u8;
    let headers = [
        (160i32, 4u32),
        (2400, 2),
        (48000, 1),
        (-103, first_g_count),
        (-103, SPECTROGRAM_WORDS_PER_CHANNEL_FRAME as u32),
    ];
    let mut out = Vec::new();
    out.extend_from_slice(b"RPKN");
    out.push(channels);
    out.push(headers.len() as u8);
    out.extend_from_slice(&48_000u32.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    for (division, count) in headers {
        out.extend_from_slice(&division.to_le_bytes());
        out.extend_from_slice(&count.to_le_bytes());
    }

    // RPKN waveform payloads: peak_count * channels * 4 bytes.
    out.extend(std::iter::repeat_n(0u8, 4 * 2 * 4));
    out.extend(std::iter::repeat_n(0u8, 2 * 2 * 4));
    out.extend(std::iter::repeat_n(0u8, 2 * 4));

    let first_time_frames = first_g_count as usize / SPECTROGRAM_WORDS_PER_CHANNEL_FRAME;
    for frame_index in 0..first_time_frames {
        for channel in 0..2usize {
            let mut bins = [0u16; SPECTROGRAM_BINS];
            for (bin_index, value) in bins.iter_mut().enumerate() {
                *value = ((frame_index * 1000 + channel * 100 + bin_index) & 0x0fff) as u16;
            }
            out.extend_from_slice(&encode_frame(&bins));
        }
    }
    for channel in 0..2usize {
        let mut bins = [0u16; SPECTROGRAM_BINS];
        for (bin_index, value) in bins.iter_mut().enumerate() {
            *value = ((3000 + channel * 100 + bin_index) & 0x0fff) as u16;
        }
        out.extend_from_slice(&encode_frame(&bins));
    }

    out.truncate(out.len().saturating_sub(truncate));
    out
}

#[test]
fn decodes_official_12bit_pair_packing() {
    let mut expected = [0u16; SPECTROGRAM_BINS];
    for (index, value) in expected.iter_mut().enumerate() {
        *value = match index % 8 {
            0 => 0,
            1 => 1,
            2 => 15,
            3 => 16,
            4 => 255,
            5 => 256,
            6 => 4094,
            _ => 4095,
        };
    }
    let raw = encode_frame(&expected);
    assert_eq!(raw.len(), 192);
    let decoded = decode_spectrogram_frame(&raw).unwrap();
    assert_eq!(decoded.bins, expected);
}

#[test]
fn parses_g_header_count_as_32bit_words_not_time_frames() {
    let raw = synthetic_file((SPECTROGRAM_WORDS_PER_CHANNEL_FRAME * 2) as u32, 0);
    let (channels, sample_rate, layers) = parse_spectrogram_layers(&raw).unwrap();
    assert_eq!(channels, 2);
    assert_eq!(sample_rate, 48_000);
    assert_eq!(layers.len(), 2);
    assert_eq!(layers[0].mirrored_division, 2400);
    assert_eq!(layers[0].frame_count(2), 2);
    assert_eq!(layers[1].mirrored_division, 48_000);
    assert_eq!(layers[1].frame_count(2), 1);
    assert_eq!(layers[0].frames[0].bins[0], 0);
    assert_eq!(layers[0].frames[1].bins[0], 100);
    assert_eq!(layers[0].frames[2].bins[127], 1127);
    assert_eq!(layers[1].frames[1].bins[127], 3227);
}

#[test]
fn main_reapeaks_parser_exposes_spectrogram_layers() {
    let raw = synthetic_file((SPECTROGRAM_WORDS_PER_CHANNEL_FRAME * 2) as u32, 0);
    let parsed = ReaPeaks::parse(raw).unwrap();
    assert_eq!(parsed.spectrogram_layers.len(), 2);
    assert_eq!(parsed.spectrogram_layers[0].mirrored_division, 2400);
    assert_eq!(parsed.spectrogram_layers[0].frame_count(2), 2);
    assert_eq!(parsed.spectrogram_layers[1].mirrored_division, 48_000);
    assert_eq!(parsed.spectrogram_layers[1].frame_count(2), 1);
    assert_eq!(parsed.spectrogram_layers[0].frames[2].bins[127], 1127);
    assert_eq!(parsed.spectrogram_layers[1].frames[1].bins[127], 3227);
}

#[test]
fn rejects_spectrogram_word_count_not_divisible_by_48() {
    let raw = synthetic_file(49, 0);
    let err = parse_spectrogram_layers(&raw).unwrap_err();
    assert!(matches!(err, ReaPeaksError::InvalidHeader(_)));
    let err = ReaPeaks::parse(raw).unwrap_err();
    assert!(matches!(err, ReaPeaksError::InvalidHeader(_)));
}

#[test]
fn rejects_truncated_spectrogram_payload() {
    let raw = synthetic_file((SPECTROGRAM_WORDS_PER_CHANNEL_FRAME * 2) as u32, 1);
    let err = parse_spectrogram_layers(&raw).unwrap_err();
    assert!(matches!(err, ReaPeaksError::Truncated));
    let raw = synthetic_file((SPECTROGRAM_WORDS_PER_CHANNEL_FRAME * 2) as u32, 1);
    let err = ReaPeaks::parse(raw).unwrap_err();
    assert!(matches!(err, ReaPeaksError::Truncated));
}
