#![cfg(feature = "strict-wdl")]

use reapeaks::{generate_f32, generate_pcm16, GenerateOptions, ReaPeaks, SpectralPeak};

fn ch_seed(seed: u32, channel: usize) -> u32 {
    seed ^ ((channel as u32 + 1).wrapping_mul(0x9E37_79B9))
}

fn pcm_i16(channels: usize, frames: usize, seed: u32, shift: u32) -> Vec<i16> {
    let mut states: Vec<u32> = (0..channels).map(|c| ch_seed(seed, c)).collect();
    let mut out = Vec::with_capacity(frames * channels);
    for _ in 0..frames {
        for state in &mut states {
            *state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            let x = (((*state >> 16) & 0xffff) as i32) - 32768;
            let x = if shift == 0 { x } else { x / (1i32 << shift) };
            out.push(x.clamp(-32768, 32767) as i16);
        }
    }
    out
}

fn pcm_f32(channels: usize, frames: usize, seed: u32, gain: f32) -> Vec<f32> {
    let mut states: Vec<u32> = (0..channels).map(|c| ch_seed(seed, c)).collect();
    let mut out = Vec::with_capacity(frames * channels);
    for _ in 0..frames {
        for state in &mut states {
            *state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            let x = (((*state >> 16) & 0xffff) as i32) - 32768;
            out.push((x as f32 / 32768.0) * gain);
        }
    }
    out
}

fn fnv64(peaks: &[SpectralPeak]) -> u64 {
    let mut h = 0xcbf2_9ce4_8422_2325u64;
    for peak in peaks {
        for byte in peak.code().to_le_bytes() {
            h ^= u64::from(byte);
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    h
}

#[test]
fn reaper779_fresh_process_all_spectral_mipmaps_are_exact() {
    let mut cases = 0usize;
    let mut levels = 0usize;
    let mut codes = 0usize;

    for line in include_str!("data/spectral_mipmap_oracle.tsv").lines() {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let cols: Vec<&str> = line.split('\t').collect();
        assert_eq!(cols.len(), 8, "bad oracle row: {line}");

        let name = cols[0];
        let sample_rate: u32 = cols[1].parse().unwrap();
        let channels: usize = cols[2].parse().unwrap();
        let sample_type = cols[3];
        let divisions: Vec<u32> = cols[4].split(',').map(|x| x.parse().unwrap()).collect();
        let expected_counts: Vec<usize> = cols[5].split(',').map(|x| x.parse().unwrap()).collect();
        let expected_hashes: Vec<u64> = cols[6]
            .split(',')
            .map(|x| u64::from_str_radix(x, 16).unwrap())
            .collect();
        let spec: Vec<&str> = cols[7].split(',').collect();

        assert_eq!(divisions.len(), 3);
        assert_eq!(expected_counts.len(), 3);
        assert_eq!(expected_hashes.len(), 3);
        let frames = sample_rate as usize * 20;
        let options = GenerateOptions {
            sample_rate,
            channels,
            divisions,
            source_mtime_low32: 0,
            source_size_low32: 0,
            spectral: true,
        };

        let bytes = match sample_type {
            "i16" => {
                assert_eq!(spec[0], "noise");
                let seed: u32 = spec[1].parse().unwrap();
                let shift: u32 = spec[2].parse().unwrap();
                generate_pcm16(&pcm_i16(channels, frames, seed, shift), &options).unwrap()
            }
            "f32" => {
                assert_eq!(spec[0], "f32_noise");
                let seed: u32 = spec[1].parse().unwrap();
                let gain: f32 = spec[2].parse().unwrap();
                generate_f32(&pcm_f32(channels, frames, seed, gain), &options, true).unwrap()
            }
            other => panic!("unknown sample type {other}"),
        };

        let parsed = ReaPeaks::parse(bytes).unwrap();
        assert_eq!(
            parsed.spectral_layers.len(),
            3,
            "spectral layer count mismatch for {name}"
        );

        for (level, layer) in parsed.spectral_layers.iter().enumerate() {
            let expected_codes = expected_counts[level] * channels;
            assert_eq!(
                layer.peaks.len(),
                expected_codes,
                "spectral mipmap count mismatch for {name} level {level}"
            );
            assert_eq!(
                fnv64(&layer.peaks),
                expected_hashes[level],
                "spectral mipmap payload mismatch for {name} level {level}"
            );
            levels += 1;
            codes += expected_codes;
        }
        cases += 1;
    }

    assert_eq!(cases, 8);
    assert_eq!(levels, 24);
    eprintln!("SPECTRAL_MIPMAP exact_cases={cases} exact_levels={levels} exact_codes={codes}");
}
