use reapeaks::{
    decode_spectrogram_frame, default_divisions, encode_spectrogram_frame, generate_pcm16_mode3,
    generate_pcm16_mode3_with_spectrogram, GenerateOptions, ReaPeaks, SpectrogramFrame,
    SPECTROGRAM_BYTES_PER_CHANNEL_FRAME,
};
use std::f64::consts::PI;

fn options(sample_rate: u32, peak_rate: u32, channels: usize) -> GenerateOptions {
    GenerateOptions {
        sample_rate,
        channels,
        divisions: default_divisions(sample_rate, peak_rate).to_vec(),
        source_mtime_low32: 0x1122_3344,
        source_size_low32: 0x5566_7788,
        spectral: true,
    }
}

fn pcm(frames: usize, channels: usize) -> Vec<i16> {
    let mut out = Vec::with_capacity(frames * channels);
    let mut state = 0x91e1_0da5u32;
    for frame in 0..frames {
        for channel in 0..channels {
            state = state
                .wrapping_mul(1_664_525)
                .wrapping_add(1_013_904_223 + channel as u32);
            let noise = (((state >> 16) & 0xffff) as i32 - 32768) / 13;
            let saw = (((frame * (17 + channel * 2_003)) % 20_003) as i32 - 10_001) * 2;
            out.push((noise + saw).clamp(-30_000, 30_000) as i16);
        }
    }
    out
}

#[test]
fn spectrogram_toggle_does_not_change_preexisting_mode3_layers() {
    let matrix = [
        (8_000u32, 100u32, 1usize, 8_003usize),
        (22_050, 500, 3, 22_067),
        (32_000, 100, 5, 64_013),
        (44_100, 172, 7, 44_137),
        (48_000, 187, 8, 96_019),
        (48_000, 500, 2, 48_029),
        (76_799, 300, 3, 76_831),
        (76_800, 300, 4, 76_837),
        (76_801, 300, 5, 76_843),
        (96_000, 375, 6, 192_047),
        (176_400, 500, 3, 176_453),
        (192_000, 1_000, 7, 192_059),
    ];

    for (sample_rate, peak_rate, channels, frames) in matrix {
        let source = pcm(frames, channels);
        let opts = options(sample_rate, peak_rate, channels);
        let plain = ReaPeaks::parse(generate_pcm16_mode3(&source, &opts).unwrap()).unwrap();
        let with_g = ReaPeaks::parse(generate_pcm16_mode3_with_spectrogram(&source, &opts).unwrap())
            .unwrap();

        assert_eq!(plain.header.version, with_g.header.version);
        assert_eq!(plain.header.channels, with_g.header.channels);
        assert_eq!(plain.header.sample_rate, with_g.header.sample_rate);
        assert_eq!(plain.header.source_mtime_low32, with_g.header.source_mtime_low32);
        assert_eq!(plain.header.source_size_low32, with_g.header.source_size_low32);

        assert_eq!(plain.wave_layers.len(), with_g.wave_layers.len());
        for (before, after) in plain.wave_layers.iter().zip(&with_g.wave_layers) {
            assert_eq!(before.division, after.division);
            assert_eq!(before.peaks, after.peaks);
        }

        assert_eq!(plain.spectral_layers.len(), with_g.spectral_layers.len());
        for (before, after) in plain.spectral_layers.iter().zip(&with_g.spectral_layers) {
            assert_eq!(before.mirrored_division, after.mirrored_division);
            assert_eq!(before.peaks, after.peaks);
        }

        assert_eq!(plain.loudness_layers.len(), with_g.loudness_layers.len());
        for (before, after) in plain.loudness_layers.iter().zip(&with_g.loudness_layers) {
            assert_eq!(before.mirrored_division, after.mirrored_division);
            assert_eq!(before.peaks, after.peaks);
        }

        assert_eq!(with_g.spectrogram_layers.len(), opts.divisions.len() - 1);
    }
}

#[test]
fn packed_pair_bytes_are_cartesian_exact_at_msb_and_nibble_edges() {
    for msb1 in [0u16, 1, 0x7f, 0xfe, 0xff] {
        for msb2 in [0u16, 1, 0x7f, 0xfe, 0xff] {
            for low1 in 0u16..16 {
                for low2 in 0u16..16 {
                    let first = (msb1 << 4) | low1;
                    let second = (msb2 << 4) | low2;
                    let mut bins = [0u16; 128];
                    bins[0] = first;
                    bins[1] = second;
                    let frame = SpectrogramFrame { bins };
                    let packed = encode_spectrogram_frame(&frame).unwrap();
                    assert_eq!(packed[0], msb1 as u8);
                    assert_eq!(packed[1], ((low1 << 4) | low2) as u8);
                    assert_eq!(packed[2], msb2 as u8);
                    assert_eq!(decode_spectrogram_frame(&packed).unwrap(), frame);
                }
            }
        }
    }
}

#[test]
fn exact_bin_spectrogram_code_is_monotone_over_pcm_amplitude() {
    let sample_rate = 48_000u32;
    let frames = 5_008usize;
    let opts = options(sample_rate, 300, 1);
    let mut previous = 0u16;

    for amplitude_code in (0u32..=32_767).step_by(257) {
        let mut source = Vec::with_capacity(frames);
        for frame in 0..frames {
            let phase = 2.0 * PI * 6_000.0 * frame as f64 / sample_rate as f64;
            source.push((amplitude_code as f64 * phase.sin()).round() as i16);
        }
        let parsed = ReaPeaks::parse(
            generate_pcm16_mode3_with_spectrogram(&source, &opts).unwrap(),
        )
        .unwrap();
        let layer = &parsed.spectrogram_layers[0];
        assert!(layer.frame_count(1) >= 2);
        let code = layer.frames[1].bins[31];
        assert!(
            code >= previous,
            "non-monotone amplitude={amplitude_code} previous={previous} code={code}"
        );
        previous = code;
    }
}

#[test]
fn spectrogram_frame_decoder_rejects_every_wrong_length_near_192() {
    for length in 180usize..=204 {
        if length == SPECTROGRAM_BYTES_PER_CHANNEL_FRAME {
            continue;
        }
        assert!(decode_spectrogram_frame(&vec![0u8; length]).is_err());
    }
}
