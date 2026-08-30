#![cfg(feature = "strict-wdl")]
#![allow(clippy::too_many_lines)]

use reapeaks::{generate_f32, generate_pcm16, GenerateOptions, ReaPeaks, SpectralPeak};

fn ch_seed(seed: u32, channel: usize) -> u32 {
    seed ^ ((channel as u32 + 1).wrapping_mul(0x9E37_79B9))
}

fn parse_u32_auto(text: &str) -> u32 {
    if let Some(hex) = text.strip_prefix("0x") {
        u32::from_str_radix(hex, 16).unwrap()
    } else {
        text.parse().unwrap()
    }
}

fn clamp16(value: i64) -> i16 {
    value.clamp(i16::MIN as i64, i16::MAX as i64) as i16
}

fn pcm_i16(channels: usize, frames: usize, spec: &[&str]) -> Vec<i16> {
    match spec[0] {
        "silence" => vec![0; frames * channels],
        "alt" => {
            let amp: i16 = spec[1].parse().unwrap();
            let mut out = Vec::with_capacity(frames * channels);
            for i in 0..frames {
                for channel in 0..channels {
                    out.push(if (i + channel) & 1 == 1 { amp } else { -amp });
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
                    let mut value = (((*state >> 16) & 0xffff) as i32) - 32768;
                    if shift != 0 {
                        value /= 1i32 << shift;
                    }
                    out.push(value.clamp(i16::MIN as i32, i16::MAX as i32) as i16);
                }
            }
            out
        }
        "square" => {
            let period: usize = spec[1].parse().unwrap();
            let amp: i16 = spec[2].parse().unwrap();
            let channel_offset: usize = spec[3].parse().unwrap();
            let half = (period / 2).max(1);
            let mut out = Vec::with_capacity(frames * channels);
            for i in 0..frames {
                for channel in 0..channels {
                    let phase = (i + channel * channel_offset) % period;
                    out.push(if phase < half { amp } else { -amp });
                }
            }
            out
        }
        "triangle" => {
            let period: usize = spec[1].parse().unwrap();
            let amp: i64 = spec[2].parse().unwrap();
            let channel_offset: usize = spec[3].parse().unwrap();
            let half = (period / 2).max(1);
            let tail = (period - half).max(1);
            let mut out = Vec::with_capacity(frames * channels);
            for i in 0..frames {
                for channel in 0..channels {
                    let phase = (i + channel * channel_offset) % period;
                    let value = if phase < half {
                        -amp + (2 * amp * phase as i64) / half as i64
                    } else {
                        amp - (2 * amp * (phase - half) as i64) / tail as i64
                    };
                    out.push(clamp16(value));
                }
            }
            out
        }
        "two_square" => {
            let period1: usize = spec[1].parse().unwrap();
            let amp1: i64 = spec[2].parse().unwrap();
            let period2: usize = spec[3].parse().unwrap();
            let amp2: i64 = spec[4].parse().unwrap();
            let channel_offset: usize = spec[5].parse().unwrap();
            let mut out = Vec::with_capacity(frames * channels);
            for i in 0..frames {
                for channel in 0..channels {
                    let phase = i + channel * channel_offset;
                    let one = if phase % period1 < (period1 / 2).max(1) { amp1 } else { -amp1 };
                    let two = if phase % period2 < (period2 / 2).max(1) { amp2 } else { -amp2 };
                    out.push(clamp16(one + two));
                }
            }
            out
        }
        "dds_square" => {
            let inc0 = parse_u32_auto(spec[1]);
            let inc1 = parse_u32_auto(spec[2]);
            let amp: i16 = spec[3].parse().unwrap();
            let seed = parse_u32_auto(spec[4]);
            let mut phases: Vec<u32> = (0..channels)
                .map(|c| seed.wrapping_add((c as u32).wrapping_mul(0x1357_9BDF)))
                .collect();
            let denominator = frames.saturating_sub(1).max(1) as i64;
            let mut out = Vec::with_capacity(frames * channels);
            for i in 0..frames {
                let delta = inc1 as i64 - inc0 as i64;
                let increment = inc0 as i64 + delta * i as i64 / denominator;
                for (channel, phase) in phases.iter_mut().enumerate() {
                    *phase = phase
                        .wrapping_add(increment as u32)
                        .wrapping_add((channel as u32).wrapping_mul(97));
                    out.push(if *phase & 0x8000_0000 != 0 { amp } else { -amp });
                }
            }
            out
        }
        "dc" => {
            let base: i64 = spec[1].parse().unwrap();
            let step: i64 = spec[2].parse().unwrap();
            let mut out = Vec::with_capacity(frames * channels);
            for _ in 0..frames {
                for channel in 0..channels {
                    let mut value = base + channel as i64 * step;
                    if channel & 1 == 1 {
                        value = -value;
                    }
                    out.push(clamp16(value));
                }
            }
            out
        }
        "impulse" => {
            let position: usize = spec[1].parse().unwrap();
            let stride: usize = spec[2].parse().unwrap();
            let amp: i64 = spec[3].parse().unwrap();
            let mut out = vec![0; frames * channels];
            for channel in 0..channels {
                let frame = position + channel * stride;
                if frame < frames {
                    out[frame * channels + channel] = clamp16(amp - channel as i64 * 777);
                }
            }
            out
        }
        "impulse_train" => {
            let period: usize = spec[1].parse().unwrap();
            let amp: i16 = spec[2].parse().unwrap();
            let channel_offset: usize = spec[3].parse().unwrap();
            let mut out = vec![0; frames * channels];
            for channel in 0..channels {
                let mut frame = channel * channel_offset;
                let mut index = 0usize;
                while frame < frames {
                    out[frame * channels + channel] = if index & 1 == 0 { amp } else { -amp };
                    index += 1;
                    frame += period;
                }
            }
            out
        }
        "saw" => {
            let period: usize = spec[1].parse().unwrap();
            let amp: i64 = spec[2].parse().unwrap();
            let channel_offset: usize = spec[3].parse().unwrap();
            let denominator = period.saturating_sub(1).max(1) as i64;
            let mut out = Vec::with_capacity(frames * channels);
            for i in 0..frames {
                for channel in 0..channels {
                    let phase = (i + channel * channel_offset) % period;
                    out.push(clamp16(-amp + 2 * amp * phase as i64 / denominator));
                }
            }
            out
        }
        other => panic!("unknown i16 stress signal {other}"),
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
            let value = (((*state >> 16) & 0xffff) as i32) - 32768;
            out.push((value as f32 / 32768.0) * gain);
        }
    }
    out
}

fn fnv64(peaks: &[SpectralPeak]) -> u64 {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for peak in peaks {
        for byte in peak.code().to_le_bytes() {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    hash
}

#[test]
fn reaper779_broad_fresh_process_spectral_stress_is_exact() {
    let mut cases = 0usize;
    let mut levels = 0usize;
    let mut codes = 0usize;

    for line in include_str!("data/spectral_stress_oracle.tsv").lines() {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let cols: Vec<&str> = line.split('\t').collect();
        assert_eq!(cols.len(), 9, "bad oracle row: {line}");

        let name = cols[0];
        let sample_rate: u32 = cols[1].parse().unwrap();
        let channels: usize = cols[2].parse().unwrap();
        let sample_type = cols[3];
        let frames: usize = cols[4].parse().unwrap();
        let divisions: Vec<u32> = cols[5].split(',').map(|x| x.parse().unwrap()).collect();
        let expected_counts: Vec<usize> = cols[6].split(',').map(|x| x.parse().unwrap()).collect();
        let expected_hashes: Vec<u64> = cols[7]
            .split(',')
            .map(|x| u64::from_str_radix(x, 16).unwrap())
            .collect();
        let spec: Vec<&str> = cols[8].split(',').collect();

        let options = GenerateOptions {
            sample_rate,
            channels,
            divisions: divisions.clone(),
            source_mtime_low32: 0,
            source_size_low32: 0,
            spectral: true,
        };
        let bytes = match sample_type {
            "i16" => generate_pcm16(&pcm_i16(channels, frames, &spec), &options).unwrap(),
            "f32" => generate_f32(&pcm_f32(channels, frames, &spec), &options, true).unwrap(),
            other => panic!("unknown sample type {other}"),
        };
        let parsed = ReaPeaks::parse(bytes).unwrap();
        assert_eq!(
            parsed.spectral_layers.len(),
            expected_counts.len(),
            "spectral layer count mismatch for {name}"
        );

        for (level_index, layer) in parsed.spectral_layers.iter().enumerate() {
            assert_eq!(
                layer.mirrored_division,
                divisions[level_index],
                "spectral division mismatch for {name} level {level_index}"
            );
            let expected_code_count = expected_counts[level_index] * channels;
            assert_eq!(
                layer.peaks.len(),
                expected_code_count,
                "spectral code count mismatch for {name} level {level_index}"
            );
            assert_eq!(
                fnv64(&layer.peaks),
                expected_hashes[level_index],
                "spectral payload mismatch for {name} level {level_index}"
            );
            levels += 1;
            codes += expected_code_count;
        }
        cases += 1;
    }

    assert_eq!(cases, 30);
    assert_eq!(levels, 90);
    assert_eq!(codes, 100_283);
    eprintln!("SPECTRAL_STRESS exact_cases={cases} exact_levels={levels} exact_codes={codes}");
}
