use reapeaks::{
    decode_spectrogram_frame, default_divisions, encode_spectrogram_frame,
    generate_pcm16_mode3_with_spectrogram, GenerateOptions, ReaPeaks, SpectrogramFrame,
    SPECTROGRAM_BYTES_PER_CHANNEL_FRAME,
};
use std::sync::Arc;

fn options(sample_rate: u32, peak_rate: u32, channels: usize) -> GenerateOptions {
    GenerateOptions {
        sample_rate,
        channels,
        divisions: default_divisions(sample_rate, peak_rate).to_vec(),
        source_mtime_low32: 0x0bad_f00d,
        source_size_low32: 0xfeed_face,
        spectral: true,
    }
}

fn deterministic_pcm(frames: usize, channels: usize, salt: u32) -> Vec<i16> {
    let mut state = 0x9e37_79b9u32 ^ salt;
    let mut pcm = Vec::with_capacity(frames.saturating_mul(channels));
    for frame in 0..frames {
        for channel in 0..channels {
            state = state
                .wrapping_mul(1_664_525)
                .wrapping_add(1_013_904_223u32.wrapping_add(channel as u32));
            let noise = (((state >> 16) & 0xffff) as i32 - 32_768) / 5;
            let saw = (((frame * (37 + channel * 997)) % 12_289) as i32 - 6_144) * 3;
            pcm.push((noise + saw).clamp(-31_000, 31_000) as i16);
        }
    }
    pcm
}

#[test]
fn arbitrary_192_byte_frames_decode_encode_bijectively() {
    let mut state = 0x243f_6a88u32;
    for case in 0..4_096usize {
        let mut packed = [0u8; SPECTROGRAM_BYTES_PER_CHANNEL_FRAME];
        for byte in &mut packed {
            state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            *byte = (state >> 24) as u8;
        }
        let frame = decode_spectrogram_frame(&packed).expect("decode arbitrary packed frame");
        let encoded = encode_spectrogram_frame(&frame).expect("re-encode arbitrary packed frame");
        assert_eq!(encoded, packed, "case={case}");
    }
}

#[test]
fn tiny_and_empty_inputs_never_panic_and_remain_parseable() {
    let configurations = [
        (8_000u32, 100u32, 1usize),
        (48_000, 300, 8),
        (76_799, 300, 3),
        (76_800, 300, 4),
        (96_000, 300, 5),
        (192_000, 1_000, 2),
    ];
    let lengths = [
        0usize, 1, 2, 15, 16, 63, 64, 127, 128, 159, 160, 167, 168, 207, 208, 209, 254, 255,
        256, 257, 318, 319, 320, 321, 511, 512, 513,
    ];

    for (sample_rate, peak_rate, channels) in configurations {
        for &frames in &lengths {
            let pcm = deterministic_pcm(frames, channels, frames as u32 ^ sample_rate);
            let result = std::panic::catch_unwind(|| {
                generate_pcm16_mode3_with_spectrogram(
                    &pcm,
                    &options(sample_rate, peak_rate, channels),
                )
            });
            let bytes = result.unwrap_or_else(|_| {
                panic!(
                    "generation panicked sr={sample_rate} pps={peak_rate} ch={channels} frames={frames}"
                )
            });
            let bytes = bytes.unwrap_or_else(|error| {
                panic!(
                    "generation failed sr={sample_rate} pps={peak_rate} ch={channels} frames={frames}: {error}"
                )
            });
            ReaPeaks::parse(bytes).unwrap_or_else(|error| {
                panic!(
                    "parse failed sr={sample_rate} pps={peak_rate} ch={channels} frames={frames}: {error}"
                )
            });
        }
    }
}

#[test]
fn channel_count_u8_boundary_is_checked_without_panicking() {
    let sample_rate = 96_000;
    let frames = 320usize;

    let pcm255 = deterministic_pcm(frames, 255, 255);
    let result255 = std::panic::catch_unwind(|| {
        generate_pcm16_mode3_with_spectrogram(&pcm255, &options(sample_rate, 300, 255))
    });
    let bytes255 = result255.expect("255-channel generation panicked").unwrap();
    let parsed255 = ReaPeaks::parse(bytes255).expect("parse 255-channel cache");
    assert_eq!(parsed255.header.channels, 255);
    assert!(parsed255
        .spectrogram_layers
        .iter()
        .all(|layer| layer.frames.len() % 255 == 0));

    let pcm256 = deterministic_pcm(frames, 256, 256);
    let result256 = std::panic::catch_unwind(|| {
        generate_pcm16_mode3_with_spectrogram(&pcm256, &options(sample_rate, 300, 256))
    });
    assert!(result256.is_ok(), "256-channel rejection panicked");
    assert!(result256.unwrap().is_err(), "accepted 256 channels");
}

#[test]
fn custom_fine_divisions_around_256_are_total_and_parseable() {
    for fine in [254u32, 255, 256, 257, 258] {
        let channels = 3usize;
        let frames = fine as usize * 12 + 37;
        let pcm = deterministic_pcm(frames, channels, fine);
        let options = GenerateOptions {
            sample_rate: 48_000,
            channels,
            divisions: vec![fine, fine * 3, fine * 9],
            source_mtime_low32: fine,
            source_size_low32: !fine,
            spectral: true,
        };
        let result = std::panic::catch_unwind(|| {
            generate_pcm16_mode3_with_spectrogram(&pcm, &options)
        });
        let bytes = result
            .unwrap_or_else(|_| panic!("generation panicked fine={fine}"))
            .unwrap_or_else(|error| panic!("generation failed fine={fine}: {error}"));
        let parsed = ReaPeaks::parse(bytes)
            .unwrap_or_else(|error| panic!("parse failed fine={fine}: {error}"));
        assert_eq!(parsed.spectrogram_layers.len(), 2, "fine={fine}");
        for layer in parsed.spectrogram_layers {
            assert_eq!(layer.frames.len() % channels, 0, "fine={fine}");
            assert!(layer
                .frames
                .iter()
                .all(|frame| frame.bins.iter().all(|&bin| bin <= 0x0fff)));
        }
    }
}

#[test]
fn invalid_division_and_pcm_shapes_return_errors_without_panicking() {
    let bad_divisions = [
        vec![],
        vec![160],
        vec![0, 2_400, 48_000],
        vec![160, 0, 48_000],
        vec![160, 2_399, 48_000],
        vec![160, 2_400, 47_999],
        vec![u32::MAX - 1, u32::MAX],
    ];
    let pcm = deterministic_pcm(1_000, 2, 0x1234);
    for divisions in bad_divisions {
        let options = GenerateOptions {
            sample_rate: 48_000,
            channels: 2,
            divisions,
            source_mtime_low32: 1,
            source_size_low32: 2,
            spectral: true,
        };
        let result = std::panic::catch_unwind(|| {
            generate_pcm16_mode3_with_spectrogram(&pcm, &options)
        });
        assert!(result.is_ok(), "invalid divisions panicked");
        assert!(result.unwrap().is_err(), "invalid divisions were accepted");
    }

    let malformed_pcm = vec![0i16; 101];
    let result = std::panic::catch_unwind(|| {
        generate_pcm16_mode3_with_spectrogram(&malformed_pcm, &options(48_000, 300, 3))
    });
    assert!(result.is_ok(), "non-interleaved PCM length panicked");
    assert!(result.unwrap().is_err(), "accepted PCM not divisible by channels");
}

#[test]
fn every_truncated_prefix_of_valid_spectrogram_cache_is_rejected_without_panicking() {
    let channels = 1usize;
    let pcm = deterministic_pcm(320, channels, 0x55aa);
    let valid = generate_pcm16_mode3_with_spectrogram(&pcm, &options(96_000, 300, channels))
        .expect("generate truncation seed");
    ReaPeaks::parse(valid.clone()).expect("parse truncation seed");

    for cut in 0..valid.len() {
        let truncated = valid[..cut].to_vec();
        let result = std::panic::catch_unwind(|| ReaPeaks::parse(truncated));
        assert!(result.is_ok(), "parser panicked cut={cut}");
        assert!(result.unwrap().is_err(), "accepted truncation cut={cut}");
    }
}

#[test]
fn deterministic_bitflip_fuzz_never_panics_parser() {
    let channels = 4usize;
    let pcm = deterministic_pcm(5_121, channels, 0xf00d);
    let valid = generate_pcm16_mode3_with_spectrogram(&pcm, &options(96_000, 300, channels))
        .expect("generate fuzz seed");
    ReaPeaks::parse(valid.clone()).expect("parse fuzz seed");
    let mut state = 0xdead_beefu32;

    for case in 0..4_096usize {
        let mut mutated = valid.clone();
        let flips = 1 + case % 3;
        for _ in 0..flips {
            state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            let offset = state as usize % mutated.len();
            state = state.rotate_left(11).wrapping_add(0xa511_e9b3);
            let mask = 1u8 << (state & 7);
            mutated[offset] ^= mask;
        }
        let result = std::panic::catch_unwind(|| ReaPeaks::parse(mutated));
        assert!(result.is_ok(), "parser panicked fuzz case={case}");
    }
}

#[test]
fn mixed_configuration_parallel_generation_matches_sequential_baselines() {
    let configurations = [
        (8_000u32, 100u32, 1usize, 1_777usize),
        (11_025, 1_000, 8, 2_003),
        (22_051, 500, 3, 3_007),
        (44_100, 172, 7, 5_011),
        (48_000, 187, 4, 5_119),
        (48_000, 500, 2, 5_121),
        (76_799, 300, 5, 6_013),
        (76_800, 300, 6, 6_019),
        (76_801, 300, 3, 6_023),
        (96_000, 375, 8, 7_001),
        (176_400, 500, 4, 7_003),
        (192_000, 1_000, 5, 7_009),
    ];

    let jobs: Vec<_> = configurations
        .iter()
        .enumerate()
        .map(|(index, &(sample_rate, peak_rate, channels, frames))| {
            let pcm = deterministic_pcm(frames, channels, index as u32);
            let baseline = generate_pcm16_mode3_with_spectrogram(
                &pcm,
                &options(sample_rate, peak_rate, channels),
            )
            .expect("generate mixed baseline");
            (sample_rate, peak_rate, channels, Arc::new(pcm), Arc::new(baseline))
        })
        .collect();

    let mut workers = Vec::new();
    for (worker, (sample_rate, peak_rate, channels, pcm, baseline)) in jobs.into_iter().enumerate() {
        workers.push(std::thread::spawn(move || {
            for round in 0..3 {
                let actual = generate_pcm16_mode3_with_spectrogram(
                    &pcm,
                    &options(sample_rate, peak_rate, channels),
                )
                .unwrap_or_else(|error| {
                    panic!("worker={worker} round={round} generation failed: {error}")
                });
                assert_eq!(
                    actual.as_slice(),
                    baseline.as_slice(),
                    "worker={worker} round={round} sr={sample_rate} pps={peak_rate} ch={channels}"
                );
            }
        }));
    }
    for worker in workers {
        worker.join().expect("mixed configuration worker panicked");
    }
}

#[test]
fn source_metadata_changes_do_not_affect_spectrogram_payload() {
    let sample_rate = 48_000u32;
    let channels = 3usize;
    let pcm = deterministic_pcm(9_777, channels, 0x7788);
    let mut first_options = options(sample_rate, 300, channels);
    first_options.source_mtime_low32 = 0;
    first_options.source_size_low32 = 1;
    let mut second_options = first_options.clone();
    second_options.source_mtime_low32 = u32::MAX;
    second_options.source_size_low32 = u32::MAX - 1;

    let first = ReaPeaks::parse(
        generate_pcm16_mode3_with_spectrogram(&pcm, &first_options).expect("generate first metadata"),
    )
    .expect("parse first metadata");
    let second = ReaPeaks::parse(
        generate_pcm16_mode3_with_spectrogram(&pcm, &second_options)
            .expect("generate second metadata"),
    )
    .expect("parse second metadata");
    assert_eq!(first.spectrogram_layers, second.spectrogram_layers);
}
