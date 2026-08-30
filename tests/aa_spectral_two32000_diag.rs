#![cfg(feature = "strict-wdl")]

use reapeaks::{generate_pcm16, GenerateOptions, ReaPeaks};

const EXPECTED_FIRST64: [u32; 64] = [
    399410175,392791045,398427145,392627209,396166153,391676937,392496137,389547017,
    386958345,385647625,380765192,380961801,377160712,377914376,376538120,377291784,
    375981064,376210440,375358472,374998024,375391240,374440968,375620616,374670344,
    376341512,375227400,376833032,375981064,377127944,376439816,377422856,377193480,
    377881608,377816072,378340360,377979912,377848840,378012680,377390088,377685000,
    377029640,377553928,376570888,377029640,375686152,376079368,375129096,374965256,
    375358472,374440968,375751688,374834184,376636424,375620616,377029640,376603656,
    377193480,377226248,377324552,377979912,377619464,378242056,377750536,378078216,
];

const EXPECTED_CHUNK_FNV64: [u64; 47] = [
    0x1ca780af163c25ab, 0x522ec181ae2dbd6f, 0xec4dfdbb01b34ea4, 0xa5971f64af61a888,
    0xf9373eb33d13fbb2, 0x7ca47b71917f4433, 0xd95f9e8d4f52440e, 0xf52cd00fb1fc9cd7,
    0x0d67096ad828079c, 0x61f35bca2b8aaf2c, 0x860d9a703bebd821, 0x70fcdd546aa1dc1a,
    0x22a41f62b191f655, 0x05a4ed497280e5ea, 0xed90dd3f4e0549ce, 0x67797caef14ab3a3,
    0xf472d7f1db932136, 0x9433e0df92dd8f19, 0x2210154c6f7c2ff8, 0x4b54d26499a1b9cd,
    0x7ba66845ccb33101, 0x6064f6f2cf143f71, 0x016c2f9889df2222, 0x955e4e130ab16f4a,
    0x72f09e5955a36483, 0x4d2872fcf2a6da7a, 0x36cbf58166878c3a, 0x7d31305bdb9a8239,
    0x1ac6b6239f34b770, 0xa4f25ccdac6c8a3c, 0xa55b42b348d1efe8, 0x4ddd80f0f515911d,
    0x79804842171bf7e5, 0x25424a62e4d693c8, 0x84a561d492617a2d, 0xc8b0ca0a8ed76a28,
    0x9159cba04017dfd9, 0x4488fcf43efb3338, 0x2357b7842924212b, 0x72be0fd3d0e29fc8,
    0xcfaab5542ec84e88, 0xb2be9ae0660f181a, 0xbc446dea181b7f08, 0xdde07b17e3365239,
    0x0f5a437a6d0e176f, 0xc50777d48ebf7f95, 0x81f2928504b4a523,
];

fn pcm() -> Vec<i16> {
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

fn pcm_fnv64(samples: &[i16]) -> u64 {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for sample in samples {
        for byte in sample.to_le_bytes() {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    hash
}

fn code_fnv64(codes: impl IntoIterator<Item = u32>) -> u64 {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for code in codes {
        for byte in code.to_le_bytes() {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    hash
}

fn generate_codes(pcm: &[i16]) -> Vec<u32> {
    let options = GenerateOptions {
        sample_rate: 32_000,
        channels: 2,
        divisions: vec![106, 1696, 32224],
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

#[test]
fn two32000_s2_input_and_first_codes_match_reaper_oracle() {
    let pcm = pcm();
    assert_eq!(pcm_fnv64(&pcm), 0xed63_043a_f310_9f1d, "input PCM differs from oracle WAV data chunk");
    let got = generate_codes(&pcm);
    assert_eq!(got.len(), 3006);
    assert_eq!(
        code_fnv64(got.iter().copied()),
        0x826b_dbbc_e8b3_9588,
        "whole-target spectral FNV differs from the verified oracle sequence",
    );

    for (index, (&expected, &actual)) in EXPECTED_FIRST64.iter().zip(got.iter()).enumerate() {
        assert_eq!(
            actual,
            expected,
            "first mismatch index={index} frame={} channel={} actual=0x{actual:08x} freq={} density={} expected=0x{expected:08x} freq={} density={}",
            index / 2,
            index % 2,
            actual & 0x7fff,
            (actual >> 15) & 0x3fff,
            expected & 0x7fff,
            (expected >> 15) & 0x3fff,
        );
    }

    for (chunk_index, &expected_hash) in EXPECTED_CHUNK_FNV64.iter().enumerate() {
        let start = chunk_index * 64;
        let end = (start + 64).min(got.len());
        let actual_hash = code_fnv64(got[start..end].iter().copied());
        assert_eq!(
            actual_hash,
            expected_hash,
            "first mismatching chunk={chunk_index} code_range={start}..{end} frame_range={}..{}",
            start / 2,
            (end.saturating_sub(1)) / 2,
        );
    }
}

#[test]
fn two32000_s2_is_repeatable_within_one_process() {
    let pcm = pcm();
    let first = generate_codes(&pcm);
    let second = generate_codes(&pcm);
    assert_eq!(first, second, "strict spectral generation leaked state across identical calls");
}
