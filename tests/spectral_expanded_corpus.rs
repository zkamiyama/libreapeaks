#![allow(clippy::too_many_lines)]
use reapeaks::spectral::{build_fine_spectral, build_fine_spectral_f32};
use std::f64::consts::PI;

fn q16(x: f64) -> i16 {
    let y = x.clamp(-1.0, 1.0) * 32767.0;
    let v = if y >= 0.0 {
        (y + 0.5).floor()
    } else {
        (y - 0.5).ceil()
    };
    v.clamp(-32768.0, 32767.0) as i16
}

fn ch_seed(seed: u32, channel: usize) -> u32 {
    seed ^ ((channel as u32 + 1).wrapping_mul(0x9E37_79B9))
}

fn pcm_i16(sr: u32, channels: usize, frames: usize, spec: &[&str]) -> Vec<i16> {
    match spec[0] {
        "tone" => {
            let base: f64 = spec[1].parse().unwrap();
            let delta: f64 = spec[2].parse().unwrap();
            let amp: f64 = spec[3].parse().unwrap();
            let phase_step: f64 = spec[4].parse().unwrap();
            let mut out = Vec::with_capacity(frames * channels);
            for i in 0..frames {
                for c in 0..channels {
                    let f = base + delta * c as f64;
                    let phase = phase_step * c as f64;
                    out.push(q16(
                        amp * (2.0 * PI * f * i as f64 / sr as f64 + phase).sin()
                    ));
                }
            }
            out
        }
        "noise" => {
            let seed: u32 = spec[1].parse().unwrap();
            let shift: u32 = spec[2].parse().unwrap();
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
        "pattern" => {
            let mode = spec[1];
            let mut out = Vec::with_capacity(frames * channels);
            for i in 0..frames {
                for c in 0..channels {
                    let v = match mode {
                        "alt" => {
                            if (i + c) & 1 == 1 {
                                32767
                            } else {
                                -32768
                            }
                        }
                        "saw" => {
                            ((((i * 3 + c * 7) % 31) as f64 / 30.0 * 2.0 - 1.0) * 24000.0) as i32
                        }
                        "dc" => (c as i32 + 1) * 4096 * if c & 1 == 1 { -1 } else { 1 },
                        _ => panic!("unknown pattern {mode}"),
                    };
                    out.push(v.clamp(-32768, 32767) as i16);
                }
            }
            out
        }
        "impulse" => {
            let pos: usize = spec[1].parse().unwrap();
            let stride: usize = spec[2].parse().unwrap();
            let mut out = vec![0i16; frames * channels];
            for c in 0..channels {
                let p = pos + c * stride;
                if p < frames {
                    out[p * channels + c] = 30000 - c as i16 * 1000;
                }
            }
            out
        }
        other => panic!("unexpected i16 spec {other}"),
    }
}

fn pcm_f32(channels: usize, frames: usize, spec: &[&str]) -> Vec<f32> {
    assert_eq!(spec[0], "f32_noise");
    let seed: u32 = spec[1].parse().unwrap();
    let gain: f32 = spec[2].parse().unwrap();
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

fn fnv64(codes: impl IntoIterator<Item = u32>) -> u64 {
    let mut h = 0xcbf2_9ce4_8422_2325u64;
    for code in codes {
        for b in code.to_le_bytes() {
            h ^= b as u64;
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    h
}

fn full_file_codes_i16(pcm: &[i16], sr: u32, channels: usize, division: u32) -> Vec<u32> {
    let options = reapeaks::GenerateOptions {
        sample_rate: sr,
        channels,
        divisions: vec![division],
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: true,
    };
    let bytes = reapeaks::generate_pcm16(pcm, &options).unwrap();
    let parsed = reapeaks::ReaPeaks::parse(bytes).unwrap();
    assert_eq!(parsed.spectral_layers.len(), 1);
    parsed.spectral_layers[0]
        .peaks
        .iter()
        .map(|p| p.code())
        .collect()
}

fn full_file_codes_f32(pcm: &[f32], sr: u32, channels: usize, division: u32) -> Vec<u32> {
    let options = reapeaks::GenerateOptions {
        sample_rate: sr,
        channels,
        divisions: vec![division],
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: true,
    };
    let bytes = reapeaks::generate_f32(pcm, &options, true).unwrap();
    let parsed = reapeaks::ReaPeaks::parse(bytes).unwrap();
    assert_eq!(parsed.spectral_layers.len(), 1);
    parsed.spectral_layers[0]
        .peaks
        .iter()
        .map(|p| p.code())
        .collect()
}

#[test]
#[cfg(feature = "strict-wdl")]
fn reaper779_expanded_fresh_process_spectral_corpus_is_exact() {
    let mut cases = 0usize;
    let mut codes = 0usize;
    for line in include_str!("data/spectral_expanded_oracle.tsv").lines() {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let cols: Vec<&str> = line.split('\t').collect();
        assert_eq!(cols.len(), 9, "bad oracle row: {line}");
        let name = cols[0];
        let sr: u32 = cols[1].parse().unwrap();
        let channels: usize = cols[2].parse().unwrap();
        let frames: usize = cols[3].parse().unwrap();
        let sample_type = cols[4];
        let spec: Vec<&str> = cols[5].split(',').collect();
        let division: u32 = cols[6].parse().unwrap();
        let expected_count: usize = cols[7].parse().unwrap();
        let expected_hash = u64::from_str_radix(cols[8], 16).unwrap();

        let (got, full_file_codes) = if sample_type == "i16" {
            let pcm = pcm_i16(sr, channels, frames, &spec);
            (
                build_fine_spectral(&pcm, frames, channels, sr, division).unwrap(),
                full_file_codes_i16(&pcm, sr, channels, division),
            )
        } else {
            assert_eq!(sample_type, "f32");
            let pcm = pcm_f32(channels, frames, &spec);
            (
                build_fine_spectral_f32(&pcm, frames, channels, sr, division).unwrap(),
                full_file_codes_f32(&pcm, sr, channels, division),
            )
        };

        let expected_codes = expected_count * channels;
        assert_eq!(
            got.len(),
            expected_codes,
            "spectral count mismatch for {name}"
        );
        let hash = fnv64(got.iter().map(|p| p.code()));
        assert_eq!(hash, expected_hash, "spectral payload mismatch for {name}");

        assert_eq!(
            full_file_codes.len(),
            expected_codes,
            "full-file spectral count mismatch for {name}"
        );
        let full_file_hash = fnv64(full_file_codes);
        assert_eq!(
            full_file_hash, expected_hash,
            "full-file spectral payload mismatch for {name}"
        );

        cases += 1;
        codes += expected_codes;
    }
    assert_eq!(cases, 169);
    assert_eq!(codes, 6188);
    eprintln!(
        "SPECTRAL_EXPANDED exact_cases={cases} exact_codes={codes} full_file_exact_cases={cases} full_file_exact_codes={codes}"
    );
}
