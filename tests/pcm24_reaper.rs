use reapeaks::{
    default_divisions, generate_f32_reaper, generate_pcm24_i32_reaper, generate_pcm24_reaper,
    GenerateOptions, ReaperPeakMode,
};

fn fixture(frames: usize, channels: usize) -> Vec<i32> {
    let mut state = 0x7a15_5eed_d15c_a11eu64;
    let mut out = Vec::with_capacity(frames * channels);
    for index in 0..frames * channels {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        let raw = ((state >> 17) as u32) & 0x00ff_ffff;
        let mut sample = ((raw << 8) as i32) >> 8;
        sample = match index % 997 {
            0 => -8_388_608,
            1 => 8_388_607,
            2 => -1,
            3 => 0,
            4 => 1,
            _ => sample,
        };
        out.push(sample);
    }
    out
}

fn pack_pcm24le(samples: &[i32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(samples.len() * 3);
    for &sample in samples {
        let raw = (sample as u32) & 0x00ff_ffff;
        out.push(raw as u8);
        out.push((raw >> 8) as u8);
        out.push((raw >> 16) as u8);
    }
    out
}

#[test]
fn packed_and_i32_pcm24_match_materialized_f32_for_every_native_mode() {
    let channels = 2usize;
    let frames = 12_347usize;
    let pcm_i32 = fixture(frames, channels);
    let pcm24le = pack_pcm24le(&pcm_i32);
    let pcm_f32: Vec<f32> = pcm_i32
        .iter()
        .map(|&sample| sample as f32 / 8_388_608.0)
        .collect();
    let options = GenerateOptions {
        sample_rate: 48_000,
        channels,
        divisions: default_divisions(48_000, 300).to_vec(),
        source_mtime_low32: 0x1234_5678,
        source_size_low32: pcm24le.len() as u32,
        spectral: false,
    };

    for mode in [
        ReaperPeakMode::Waveform,
        ReaperPeakMode::Spectral,
        ReaperPeakMode::Spectrogram,
    ] {
        let expected = generate_f32_reaper(&pcm_f32, &options, false, mode).unwrap();
        let packed = generate_pcm24_reaper(&pcm24le, &options, mode).unwrap();
        let i32_cache = generate_pcm24_i32_reaper(&pcm_i32, &options, mode).unwrap();

        assert_eq!(&expected[..4], b"RPKN", "mode={mode:?}");
        assert_eq!(packed, expected, "packed PCM24 mismatch mode={mode:?}");
        assert_eq!(i32_cache, expected, "i32 PCM24 mismatch mode={mode:?}");
    }
}

#[test]
fn pcm24_inputs_reject_malformed_or_out_of_range_data() {
    let options = GenerateOptions {
        sample_rate: 48_000,
        channels: 2,
        divisions: default_divisions(48_000, 300).to_vec(),
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: false,
    };

    assert!(generate_pcm24_reaper(&[0, 0], &options, ReaperPeakMode::Waveform).is_err());
    assert!(generate_pcm24_reaper(&[0, 0, 0], &options, ReaperPeakMode::Waveform).is_err());
    assert!(generate_pcm24_i32_reaper(
        &[8_388_608, 0],
        &options,
        ReaperPeakMode::Waveform,
    )
    .is_err());
    assert!(generate_pcm24_i32_reaper(
        &[-8_388_609, 0],
        &options,
        ReaperPeakMode::Waveform,
    )
    .is_err());
}
