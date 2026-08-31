use reapeaks::{
    default_divisions, generate_f32, generate_f32_mode3, generate_pcm16, generate_pcm16_mode3,
    quantize_rpkl_f32, quantize_rpkn_f32, GenerateOptions, ReaPeaks, Version,
};

fn options(sample_rate: u32, channels: usize, divisions: Vec<u32>, spectral: bool) -> GenerateOptions {
    GenerateOptions {
        sample_rate,
        channels,
        divisions,
        source_mtime_low32: 0xfeed_beef,
        source_size_low32: 0xdead_cafe,
        spectral,
    }
}

#[test]
fn default_divisions_are_nested_across_dense_preference_grid() {
    let sample_rates = [
        16u32, 17, 40, 41, 8_000, 11_025, 22_050, 22_051, 32_000, 44_100, 48_000,
        88_200, 96_000, 176_400, 192_000, 384_000, 1_000_000,
    ];
    let peak_rates = [
        1u32, 2, 3, 7, 19, 20, 21, 40, 99, 100, 149, 150, 151, 199, 200, 201, 299, 300,
        301, 499, 500, 501, 999, 1_000, 1_001, 9_999, 1_000_000,
    ];

    for sample_rate in sample_rates {
        for peak_rate in peak_rates {
            let [fine, mid, coarse] = default_divisions(sample_rate, peak_rate);
            assert!(fine > 0, "sr={sample_rate} pps={peak_rate}");
            assert!(
                mid >= fine,
                "sr={sample_rate} pps={peak_rate}: {fine},{mid},{coarse}"
            );
            assert!(
                coarse >= mid,
                "sr={sample_rate} pps={peak_rate}: {fine},{mid},{coarse}"
            );
            assert_eq!(
                mid % fine,
                0,
                "sr={sample_rate} pps={peak_rate}: {fine},{mid},{coarse}"
            );
            assert_eq!(
                coarse % mid,
                0,
                "sr={sample_rate} pps={peak_rate}: {fine},{mid},{coarse}"
            );
            assert!(fine <= sample_rate.max(1));
            assert!(
                coarse >= sample_rate,
                "sr={sample_rate} pps={peak_rate}: {fine},{mid},{coarse}"
            );
            assert!(sample_rate / mid <= 20, "mid cadence too dense");
            assert!(sample_rate / coarse <= 1, "coarse cadence too dense");
        }
    }
}

#[test]
fn default_divisions_extremes_never_panic_or_return_zero() {
    for sample_rate in [0u32, 1, 2, 15, u32::MAX - 1, u32::MAX] {
        for peak_rate in [0u32, 1, 2, u32::MAX - 1, u32::MAX] {
            let result = std::panic::catch_unwind(|| default_divisions(sample_rate, peak_rate));
            let divisions = result.expect("default_divisions panicked");
            assert!(divisions.iter().all(|&value| value > 0));
        }
    }
}

#[test]
fn generator_validation_rejects_hostile_option_combinations() {
    let cases = [
        options(48_000, 0, vec![160], false),
        options(48_000, 256, vec![160], false),
        options(0, 1, vec![160], false),
        options(48_000, 1, vec![], false),
        options(48_000, 1, vec![0], false),
        options(48_000, 1, vec![i32::MAX as u32 + 1], false),
    ];
    for (index, opt) in cases.iter().enumerate() {
        assert!(
            generate_pcm16(&[], opt).is_err(),
            "case {index} unexpectedly accepted"
        );
    }

    let partial = options(48_000, 2, vec![160], false);
    assert!(generate_pcm16(&[0], &partial).is_err());

    let mode3_no_spectral = options(48_000, 1, vec![160, 2_400, 48_000], false);
    assert!(generate_pcm16_mode3(&[0; 160], &mode3_no_spectral).is_err());

    let mode3_one_division = options(48_000, 1, vec![160], true);
    assert!(generate_pcm16_mode3(&[0; 160], &mode3_one_division).is_err());

    let mode3_non_nested = options(48_000, 1, vec![160, 2_400, 48_001], true);
    assert!(generate_pcm16_mode3(&[0; 4_800], &mode3_non_nested).is_err());
}

#[test]
fn layer_count_boundary_is_checked_before_work() {
    let allowed = options(48_000, 1, vec![1; 127], true);
    let generated =
        generate_pcm16(&[], &allowed).expect("254 layers should fit u8 mipmap count");
    let parsed = ReaPeaks::parse(generated).expect("parse allowed layer-count boundary");
    assert_eq!(parsed.layer_headers.len(), 254);

    let rejected = options(48_000, 1, vec![1; 128], true);
    assert!(generate_pcm16(&[], &rejected).is_err());
}

#[test]
fn empty_and_tiny_pcm_are_total_functions() {
    let sample_rates = [16u32, 8_000, 22_051, 44_100, 48_000, 96_000];
    let channels = [1usize, 2, 3, 6, 255];
    let frame_counts = [0usize, 1, 2, 3, 7, 15, 16, 17, 39, 40, 41];

    for sample_rate in sample_rates {
        let divisions = default_divisions(sample_rate, 300).to_vec();
        for channel_count in channels {
            for frames in frame_counts {
                let pcm = vec![0i16; frames * channel_count];
                let opt = options(sample_rate, channel_count, divisions.clone(), false);
                let result = std::panic::catch_unwind(|| generate_pcm16(&pcm, &opt));
                let generated = result
                    .expect("generator panicked")
                    .expect("valid tiny PCM rejected");
                let parsed =
                    ReaPeaks::parse(generated).expect("generated tiny file failed to parse");
                assert_eq!(parsed.header.channels as usize, channel_count);
                assert_eq!(parsed.header.sample_rate, sample_rate);
            }
        }
    }
}

#[test]
fn mode3_handles_short_lengths_around_every_scheduler_boundary() {
    let configurations = [
        (22_050u32, 300u32),
        (22_051, 300),
        (32_000, 300),
        (44_100, 150),
        (44_100, 300),
        (44_100, 500),
        (48_000, 150),
        (48_000, 300),
        (48_000, 500),
        (88_200, 300),
        (96_000, 1_000),
    ];

    for (sample_rate, peak_rate) in configurations {
        let divisions = default_divisions(sample_rate, peak_rate).to_vec();
        let block = (sample_rate as usize / 40).max(1);
        let mid = divisions[1] as usize;
        let candidates = [
            0usize,
            1,
            block.saturating_sub(1),
            block,
            block + 1,
            mid.saturating_sub(1),
            mid,
            mid + 1,
            16 * block - 1,
            16 * block,
            16 * block + 1,
            120 * block - 1,
            120 * block,
            120 * block + 1,
        ];
        for frames in candidates {
            let pcm = vec![0i16; frames];
            let opt = options(sample_rate, 1, divisions.clone(), true);
            let result = std::panic::catch_unwind(|| generate_pcm16_mode3(&pcm, &opt));
            let generated = result
                .expect("mode3 panicked")
                .expect("valid boundary input rejected");
            ReaPeaks::parse(generated).expect("mode3 boundary output did not parse");
        }
    }
}

#[test]
fn generated_files_are_deterministic_and_metadata_exact() {
    let mut pcm = Vec::new();
    for frame in 0..10_001i32 {
        pcm.push(((frame * 97) as i16).wrapping_add((frame >> 2) as i16));
        pcm.push((-(frame * 53) as i16).wrapping_sub((frame >> 3) as i16));
    }
    let opt = options(
        44_100,
        2,
        default_divisions(44_100, 500).to_vec(),
        true,
    );
    let first = generate_pcm16_mode3(&pcm, &opt).expect("first generation");
    let second = generate_pcm16_mode3(&pcm, &opt).expect("second generation");
    assert_eq!(first, second);
    let parsed = ReaPeaks::parse(first).expect("parse deterministic output");
    assert_eq!(parsed.header.source_mtime_low32, 0xfeed_beef);
    assert_eq!(parsed.header.source_size_low32, 0xdead_cafe);
    assert_eq!(parsed.header.version, Version::Rpkn);
}

#[test]
fn parser_rejects_every_truncation_of_valid_mode3_file_without_panicking() {
    let sample_rate = 48_000;
    let pcm = vec![0i16; 4_801 * 2];
    let opt = options(
        sample_rate,
        2,
        default_divisions(sample_rate, 300).to_vec(),
        true,
    );
    let valid = generate_pcm16_mode3(&pcm, &opt).expect("generate seed file");
    assert!(ReaPeaks::parse(valid.clone()).is_ok());
    for cut in 0..valid.len() {
        let prefix = valid[..cut].to_vec();
        let result = std::panic::catch_unwind(|| ReaPeaks::parse(prefix));
        assert!(
            result.is_ok(),
            "parser panicked at truncation {cut}/{}",
            valid.len()
        );
        assert!(
            result.unwrap().is_err(),
            "truncation {cut}/{} unexpectedly parsed",
            valid.len()
        );
    }
}

#[test]
fn parser_mutations_never_panic() {
    let sample_rate = 22_051;
    let pcm: Vec<i16> = (0..6_000)
        .map(|i| ((i * 1237) as i16).wrapping_add(i as i16))
        .collect();
    let opt = options(
        sample_rate,
        1,
        default_divisions(sample_rate, 500).to_vec(),
        true,
    );
    let valid = generate_pcm16_mode3(&pcm, &opt).expect("generate mutation seed");
    assert!(ReaPeaks::parse(valid.clone()).is_ok());

    let stride = (valid.len() / 257).max(1);
    for offset in (0..valid.len()).step_by(stride) {
        for mask in [0x01u8, 0x80, 0xff] {
            let mut mutated = valid.clone();
            mutated[offset] ^= mask;
            let result = std::panic::catch_unwind(|| ReaPeaks::parse(mutated));
            assert!(
                result.is_ok(),
                "parser panicked offset={offset} mask={mask:#x}"
            );
        }
    }
}

#[test]
fn parser_handles_adversarial_declared_counts_without_overflow_or_panic() {
    let tokens = [1i32, 160, -115, -114, -103];
    for channels in [1u8, 2, 6, 255] {
        for token in tokens {
            for count in [u32::MAX, u32::MAX - 1, 0x4000_0000, 0x7fff_ffff] {
                let mut raw = Vec::new();
                raw.extend_from_slice(b"RPKN");
                raw.push(channels);
                raw.push(1);
                raw.extend_from_slice(&48_000u32.to_le_bytes());
                raw.extend_from_slice(&0u32.to_le_bytes());
                raw.extend_from_slice(&0u32.to_le_bytes());
                raw.extend_from_slice(&token.to_le_bytes());
                raw.extend_from_slice(&count.to_le_bytes());
                let result = std::panic::catch_unwind(|| ReaPeaks::parse(raw));
                assert!(
                    result.is_ok(),
                    "panic channels={channels} token={token} count={count}"
                );
                assert!(result.unwrap().is_err());
            }
        }
    }
}

#[test]
fn f32_special_values_are_defined_and_generation_never_panics() {
    let values = [
        f32::NAN,
        f32::INFINITY,
        f32::NEG_INFINITY,
        0.0,
        -0.0,
        f32::MIN_POSITIVE,
        -f32::MIN_POSITIVE,
        f32::from_bits(1),
        f32::from_bits(0x8000_0001),
        1.0,
        -1.0,
        1.000_001,
        -1.000_001,
        255.0,
        -256.0,
        f32::MAX,
        -f32::MAX,
    ];
    for value in values {
        let rpkn = std::panic::catch_unwind(|| quantize_rpkn_f32(value));
        let rpkl = std::panic::catch_unwind(|| quantize_rpkl_f32(value));
        assert!(rpkn.is_ok(), "RPKN quantizer panicked for {value:?}");
        assert!(rpkl.is_ok(), "RPKL quantizer panicked for {value:?}");
    }

    let mut pcm = Vec::new();
    for _ in 0..128 {
        pcm.extend_from_slice(&values);
    }
    let opt = options(48_000, 1, vec![17, 85, 2_465], false);
    for large_range in [false, true] {
        let result = std::panic::catch_unwind(|| generate_f32(&pcm, &opt, large_range));
        let blob = result
            .expect("f32 generator panicked")
            .expect("f32 generation failed");
        ReaPeaks::parse(blob).expect("f32 output failed to parse");
    }
}

#[test]
fn f32_mode3_special_values_do_not_poison_structure() {
    let values = [
        f32::NAN,
        f32::INFINITY,
        f32::NEG_INFINITY,
        0.0,
        -0.0,
        0.5,
        -0.5,
    ];
    let pcm: Vec<f32> = (0..10_000).map(|i| values[i % values.len()]).collect();
    let opt = options(
        48_000,
        1,
        default_divisions(48_000, 300).to_vec(),
        true,
    );
    for large_range in [false, true] {
        let result = std::panic::catch_unwind(|| generate_f32_mode3(&pcm, &opt, large_range));
        let blob = result
            .expect("f32 mode3 panicked")
            .expect("f32 mode3 generation failed");
        ReaPeaks::parse(blob).expect("f32 mode3 output failed to parse");
    }
}

#[test]
fn randomized_valid_pcm_generation_and_parse_is_total() {
    let mut state = 0x4d59_5df4_d0f3_3173u64;
    let sample_rates = [
        8_000u32, 11_025, 16_000, 22_050, 22_051, 32_000, 44_100, 48_000, 88_200, 96_000,
    ];
    let peak_rates = [100u32, 150, 200, 300, 500, 1_000];
    let channel_counts = [1usize, 2, 3, 4, 6];

    for case in 0..256usize {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        let sample_rate = sample_rates[(state as usize) % sample_rates.len()];
        let peak_rate = peak_rates[((state >> 8) as usize) % peak_rates.len()];
        let channels = channel_counts[((state >> 16) as usize) % channel_counts.len()];
        let frames = ((state >> 24) as usize) % 2_048;
        let mut pcm = Vec::with_capacity(frames * channels);
        for _ in 0..frames * channels {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            pcm.push(state as i16);
        }
        let opt = options(
            sample_rate,
            channels,
            default_divisions(sample_rate, peak_rate).to_vec(),
            true,
        );
        let result = std::panic::catch_unwind(|| generate_pcm16_mode3(&pcm, &opt));
        let blob = result
            .unwrap_or_else(|_| panic!("generator panicked case={case}"))
            .unwrap_or_else(|error| panic!("generator rejected valid case={case}: {error}"));
        ReaPeaks::parse(blob)
            .unwrap_or_else(|error| panic!("parse failed case={case}: {error}"));
    }
}
