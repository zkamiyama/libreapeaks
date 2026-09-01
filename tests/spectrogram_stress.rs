use reapeaks::{
    decode_spectrogram_frame, default_divisions, encode_spectrogram_frame,
    generate_pcm16_mode3_with_spectrogram, GenerateOptions, ReaPeaks, SpectrogramFrame,
};
use std::sync::Arc;

fn options(sample_rate: u32, channels: usize, peak_rate: u32) -> GenerateOptions {
    GenerateOptions {
        sample_rate,
        channels,
        divisions: default_divisions(sample_rate, peak_rate).to_vec(),
        source_mtime_low32: 0x1357_9bdf,
        source_size_low32: 0x2468_ace0,
        spectral: true,
    }
}

fn lane_sample(frame: usize, channel: usize) -> i16 {
    let a = ((frame as u64 * (97 + channel as u64 * 18_376)
        + channel as u64 * 1_000_003)
        % 56_001) as i32
        - 28_000;
    let b = (((frame / (channel + 1).max(1)) % 257) as i32 - 128) * (channel as i32 + 1);
    (a + b).clamp(-30_000, 30_000) as i16
}

fn lane_pcm(frames: usize, channels: usize) -> Vec<i16> {
    let mut pcm = Vec::with_capacity(frames * channels);
    for frame in 0..frames {
        for channel in 0..channels {
            pcm.push(lane_sample(frame, channel));
        }
    }
    pcm
}

fn generate(pcm: &[i16], sample_rate: u32, channels: usize, peak_rate: u32) -> Vec<u8> {
    generate_pcm16_mode3_with_spectrogram(pcm, &options(sample_rate, channels, peak_rate))
        .expect("spectrogram stress generation")
}

#[test]
fn exhaustive_12bit_spectrogram_packing_roundtrips() {
    for seed in 0u16..=0x0fff {
        let mut bins = [0u16; 128];
        for (index, bin) in bins.iter_mut().enumerate() {
            *bin = (seed.wrapping_add((index as u16).wrapping_mul(257))) & 0x0fff;
        }
        let frame = SpectrogramFrame { bins };
        let packed = encode_spectrogram_frame(&frame).expect("encode 12-bit frame");
        let decoded = decode_spectrogram_frame(&packed).expect("decode packed frame");
        assert_eq!(decoded, frame, "seed={seed}");
    }
}

#[test]
fn multichannel_spectrogram_equals_independent_mono_lanes() {
    let sample_rate = 44_100;
    let peak_rate = 301;
    let channels = 7;
    let frames = 17_003;
    let pcm = lane_pcm(frames, channels);
    let multi = ReaPeaks::parse(generate(&pcm, sample_rate, channels, peak_rate))
        .expect("parse multichannel stress cache");

    for channel in 0..channels {
        let mono_pcm: Vec<i16> = pcm
            .chunks_exact(channels)
            .map(|frame| frame[channel])
            .collect();
        let mono = ReaPeaks::parse(generate(&mono_pcm, sample_rate, 1, peak_rate))
            .expect("parse mono lane cache");
        assert_eq!(multi.spectrogram_layers.len(), mono.spectrogram_layers.len());
        for (level, (multi_layer, mono_layer)) in multi
            .spectrogram_layers
            .iter()
            .zip(&mono.spectrogram_layers)
            .enumerate()
        {
            assert_eq!(multi_layer.mirrored_division, mono_layer.mirrored_division);
            assert_eq!(multi_layer.frame_count(channels), mono_layer.frame_count(1));
            for time in 0..mono_layer.frame_count(1) {
                assert_eq!(
                    multi_layer.frames[time * channels + channel],
                    mono_layer.frames[time],
                    "level={level} time={time} channel={channel}"
                );
            }
        }
    }
}

#[test]
fn channel_permutation_and_sign_inversion_are_exact_equivariances() {
    let sample_rate = 96_000;
    let peak_rate = 375;
    let channels = 5;
    let frames = 21_337;
    let pcm = lane_pcm(frames, channels);
    let original = ReaPeaks::parse(generate(&pcm, sample_rate, channels, peak_rate))
        .expect("parse original cache");

    let inverted_pcm: Vec<i16> = pcm.iter().map(|&sample| -sample).collect();
    let inverted = ReaPeaks::parse(generate(
        &inverted_pcm,
        sample_rate,
        channels,
        peak_rate,
    ))
    .expect("parse sign-inverted cache");
    assert_eq!(
        original.spectrogram_layers, inverted.spectrogram_layers,
        "magnitude spectrogram changed under exact sign inversion"
    );

    let permutation = [4usize, 1, 3, 0, 2];
    let mut permuted_pcm = Vec::with_capacity(pcm.len());
    for frame in pcm.chunks_exact(channels) {
        for &old_channel in &permutation {
            permuted_pcm.push(frame[old_channel]);
        }
    }
    let permuted = ReaPeaks::parse(generate(
        &permuted_pcm,
        sample_rate,
        channels,
        peak_rate,
    ))
    .expect("parse permuted cache");

    for (level, (expected, actual)) in original
        .spectrogram_layers
        .iter()
        .zip(&permuted.spectrogram_layers)
        .enumerate()
    {
        assert_eq!(expected.mirrored_division, actual.mirrored_division);
        assert_eq!(expected.frame_count(channels), actual.frame_count(channels));
        for time in 0..expected.frame_count(channels) {
            for (new_channel, &old_channel) in permutation.iter().enumerate() {
                assert_eq!(
                    actual.frames[time * channels + new_channel],
                    expected.frames[time * channels + old_channel],
                    "level={level} time={time} new_channel={new_channel} old_channel={old_channel}"
                );
            }
        }
    }
}

#[test]
fn parallel_generation_is_byte_deterministic() {
    let sample_rate = 48_000;
    let peak_rate = 187;
    let channels = 4;
    let pcm = Arc::new(lane_pcm(16_777, channels));
    let baseline = Arc::new(generate(&pcm, sample_rate, channels, peak_rate));

    let mut workers = Vec::new();
    for worker in 0..8 {
        let pcm = Arc::clone(&pcm);
        let baseline = Arc::clone(&baseline);
        workers.push(std::thread::spawn(move || {
            for round in 0..3 {
                let generated = generate(&pcm, sample_rate, channels, peak_rate);
                assert_eq!(
                    generated.as_slice(),
                    baseline.as_slice(),
                    "worker={worker} round={round}"
                );
            }
        }));
    }
    for worker in workers {
        worker.join().expect("spectrogram worker panicked");
    }
}

#[test]
fn zero_pcm_stays_zero_across_rate_preference_and_channel_extremes() {
    let configurations = [
        (8_000u32, 100u32, 1usize),
        (22_050, 500, 3),
        (32_000, 100, 5),
        (44_100, 150, 7),
        (48_000, 500, 8),
        (76_799, 300, 2),
        (76_800, 300, 4),
        (96_000, 375, 6),
        (192_000, 1_000, 3),
    ];
    for (sample_rate, peak_rate, channels) in configurations {
        let frames = sample_rate as usize / 3 + 137;
        let pcm = vec![0i16; frames * channels];
        let parsed = ReaPeaks::parse(generate(&pcm, sample_rate, channels, peak_rate))
            .expect("parse zero cache");
        for (level, layer) in parsed.spectrogram_layers.iter().enumerate() {
            assert!(
                layer
                    .frames
                    .iter()
                    .all(|frame| frame.bins.iter().all(|&bin| bin == 0)),
                "nonzero silence code sr={sample_rate} pps={peak_rate} ch={channels} level={level}"
            );
        }
    }
}

#[test]
fn randomized_scheduler_matrix_is_total_deterministic_and_12bit() {
    let sample_rates = [
        8_000u32, 11_025, 16_000, 22_050, 22_051, 24_000, 32_000, 44_100, 48_000,
        76_799, 76_800, 76_801, 88_200, 96_000, 176_400, 192_000,
    ];
    let peak_rates = [
        100u32, 149, 150, 171, 172, 173, 187, 188, 200, 299, 300, 301, 374, 375,
        376, 499, 500, 501, 1_000,
    ];
    let mut state = 0x6d2b_79f5u32;

    for case in 0..48usize {
        state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
        let sample_rate = sample_rates[state as usize % sample_rates.len()];
        state = state.rotate_left(7).wrapping_add(case as u32 * 0x9e37_79b9);
        let peak_rate = peak_rates[state as usize % peak_rates.len()];
        let channels = 1 + ((state >> 9) as usize % 8);
        let divisions = default_divisions(sample_rate, peak_rate);
        let fine = divisions[0] as usize;
        let mid = divisions[1] as usize;
        let boundaries = [
            0usize,
            1,
            255,
            256,
            257,
            fine.saturating_sub(1),
            fine,
            fine.saturating_add(1),
            mid.saturating_sub(1),
            mid,
            mid.saturating_add(1),
            mid.saturating_mul(2).saturating_sub(1),
            mid.saturating_mul(2),
            mid.saturating_mul(2).saturating_add(1),
        ];
        let frames = if case % 3 == 0 {
            boundaries[case % boundaries.len()]
        } else {
            1 + ((state >> 3) as usize % 7_500)
        };
        let pcm = lane_pcm(frames, channels);
        let result = std::panic::catch_unwind(|| generate(&pcm, sample_rate, channels, peak_rate));
        let bytes = result.unwrap_or_else(|_| {
            panic!("panic case={case} sr={sample_rate} pps={peak_rate} ch={channels} frames={frames}")
        });
        let parsed = ReaPeaks::parse(bytes.clone()).unwrap_or_else(|error| {
            panic!("parse case={case} sr={sample_rate} pps={peak_rate} ch={channels} frames={frames}: {error}")
        });
        assert_eq!(parsed.spectrogram_layers.len(), 2, "case={case}");
        for layer in &parsed.spectrogram_layers {
            assert_eq!(layer.frames.len() % channels, 0, "case={case}");
            assert!(
                layer
                    .frames
                    .iter()
                    .all(|frame| frame.bins.iter().all(|&bin| bin <= 0x0fff)),
                "out-of-range 12-bit bin case={case}"
            );
        }
        if case % 8 == 0 {
            assert_eq!(
                bytes,
                generate(&pcm, sample_rate, channels, peak_rate),
                "nondeterminism case={case}"
            );
        }
    }
}

#[test]
fn completed_spectrogram_prefix_is_stable_under_zero_tail_extension() {
    for (sample_rate, peak_rate, channels) in [
        (48_000u32, 300u32, 3usize),
        (96_000, 375, 5),
        (44_100, 150, 2),
    ] {
        let divisions = default_divisions(sample_rate, peak_rate);
        let base_frames = divisions[1] as usize * 4 + divisions[0] as usize / 2 + 17;
        let short_pcm = lane_pcm(base_frames, channels);
        let mut long_pcm = short_pcm.clone();
        long_pcm.resize(
            (base_frames + divisions[1] as usize + 113) * channels,
            0,
        );
        let short = ReaPeaks::parse(generate(
            &short_pcm,
            sample_rate,
            channels,
            peak_rate,
        ))
        .expect("parse short cache");
        let long = ReaPeaks::parse(generate(&long_pcm, sample_rate, channels, peak_rate))
            .expect("parse extended cache");

        for (level, (short_layer, long_layer)) in short
            .spectrogram_layers
            .iter()
            .zip(&long.spectrogram_layers)
            .enumerate()
        {
            let short_time = short_layer.frame_count(channels);
            let stable_time = short_time.saturating_sub(1);
            assert!(long_layer.frame_count(channels) >= stable_time);
            assert_eq!(
                &short_layer.frames[..stable_time * channels],
                &long_layer.frames[..stable_time * channels],
                "prefix changed sr={sample_rate} pps={peak_rate} ch={channels} level={level}"
            );
        }
    }
}

#[test]
fn many_nested_spectrogram_levels_remain_parseable_and_bounded() {
    let sample_rate = 48_000;
    let channels = 2;
    let frames = 12_345;
    let pcm = lane_pcm(frames, channels);
    let options = GenerateOptions {
        sample_rate,
        channels,
        divisions: vec![64, 128, 256, 512, 1_024, 2_048, 4_096],
        source_mtime_low32: 7,
        source_size_low32: 11,
        spectral: true,
    };
    let bytes = generate_pcm16_mode3_with_spectrogram(&pcm, &options)
        .expect("generate many-level spectrogram");
    let parsed = ReaPeaks::parse(bytes).expect("parse many-level spectrogram");
    assert_eq!(parsed.spectrogram_layers.len(), options.divisions.len() - 1);
    for layer in parsed.spectrogram_layers {
        assert_eq!(layer.frames.len() % channels, 0);
        assert!(
            layer
                .frames
                .iter()
                .all(|frame| frame.bins.iter().all(|&bin| bin <= 0x0fff))
        );
    }
}

#[test]
fn hostile_spectrogram_header_mutations_fail_without_panicking() {
    let sample_rate = 48_000;
    let channels = 4;
    let pcm = lane_pcm(9_777, channels);
    let valid = generate(&pcm, sample_rate, channels, 300);
    ReaPeaks::parse(valid.clone()).expect("valid mutation seed");

    let layer_count = usize::from(valid[5]);
    let mut g_headers = Vec::new();
    for index in 0..layer_count {
        let offset = 18 + index * 8;
        let division = i32::from_le_bytes(valid[offset..offset + 4].try_into().unwrap());
        if division == -103 {
            g_headers.push(offset);
        }
    }
    assert!(!g_headers.is_empty());

    for &header in &g_headers {
        for count in [1u32, 47, 49, u32::MAX] {
            let mut mutated = valid.clone();
            mutated[header + 4..header + 8].copy_from_slice(&count.to_le_bytes());
            let result = std::panic::catch_unwind(|| ReaPeaks::parse(mutated));
            assert!(result.is_ok(), "parser panicked count={count}");
            assert!(result.unwrap().is_err(), "accepted hostile count={count}");
        }
    }

    for channels_byte in [0u8, 255] {
        let mut mutated = valid.clone();
        mutated[4] = channels_byte;
        let result = std::panic::catch_unwind(|| ReaPeaks::parse(mutated));
        assert!(result.is_ok(), "parser panicked channels={channels_byte}");
        assert!(result.unwrap().is_err(), "accepted channels={channels_byte}");
    }

    let mut zero_rate = valid.clone();
    zero_rate[6..10].fill(0);
    let result = std::panic::catch_unwind(|| ReaPeaks::parse(zero_rate));
    assert!(result.is_ok());
    assert!(result.unwrap().is_err());

    let mut impossible_table = valid;
    impossible_table[5] = 255;
    let result = std::panic::catch_unwind(|| ReaPeaks::parse(impossible_table));
    assert!(result.is_ok());
    assert!(result.unwrap().is_err());
}
