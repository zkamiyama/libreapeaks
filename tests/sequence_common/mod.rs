#![allow(dead_code)]

use reapeaks::{generate_pcm16, GenerateOptions, ReaPeaks};

pub const TARGET_HASH: u64 = 0x826b_dbbc_e8b3_9588;

pub fn fnv64(codes: impl IntoIterator<Item = u32>) -> u64 {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for code in codes {
        for byte in code.to_le_bytes() {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    hash
}

pub fn generate_i16(pcm: &[i16], sample_rate: u32, channels: usize, divisions: &[u32]) -> Vec<u32> {
    let options = GenerateOptions {
        sample_rate,
        channels,
        divisions: divisions.to_vec(),
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: true,
    };
    let parsed = ReaPeaks::parse(generate_pcm16(pcm, &options).unwrap()).unwrap();
    parsed.spectral_layers[0]
        .peaks
        .iter()
        .map(|peak| peak.code())
        .collect()
}

pub fn target_pcm() -> Vec<i16> {
    let frames = 160_031usize;
    let channels = 2usize;
    let mut out = Vec::with_capacity(frames * channels);
    for i in 0..frames {
        for channel in 0..channels {
            let phase = i + channel * 5;
            let one = if phase % 31 < 15 { 18_000 } else { -18_000 };
            let two = if phase % 47 < 23 { 9_000 } else { -9_000 };
            out.push((one + two) as i16);
        }
    }
    out
}

pub fn n22051_pcm() -> Vec<i16> {
    let frames = 110_274usize;
    let mut state = 305_419_896u32 ^ 0x9E37_79B9;
    let mut out = Vec::with_capacity(frames);
    for _ in 0..frames {
        state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
        let value = ((((state >> 16) & 0xffff) as i32) - 32768) / 8;
        out.push(value as i16);
    }
    out
}

pub fn sq22051_pcm() -> Vec<i16> {
    let frames = 110_278usize;
    let channels = 2usize;
    let mut out = Vec::with_capacity(frames * channels);
    for i in 0..frames {
        for channel in 0..channels {
            let phase = (i + channel * 3) % 23;
            out.push(if phase < 11 { 25_000 } else { -25_000 });
        }
    }
    out
}

pub fn tri32000_pcm() -> Vec<i16> {
    let frames = 160_029usize;
    let period = 37usize;
    let half = period / 2;
    let tail = period - half;
    let amp = 28_000i64;
    let mut out = Vec::with_capacity(frames);
    for i in 0..frames {
        let phase = i % period;
        let value = if phase < half {
            -amp + (2 * amp * phase as i64) / half as i64
        } else {
            amp - (2 * amp * (phase - half) as i64) / tail as i64
        };
        out.push(value as i16);
    }
    out
}

pub fn assert_target_exact() {
    let target = target_pcm();
    let codes = generate_i16(&target, 32_000, 2, &[106, 1696, 32224]);
    assert_eq!(codes.len(), 3006);
    assert_eq!(fnv64(codes), TARGET_HASH);
}
