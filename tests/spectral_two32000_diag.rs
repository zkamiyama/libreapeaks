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

#[test]
fn two32000_s2_input_and_first_codes_match_reaper_oracle() {
    let pcm = pcm();
    assert_eq!(pcm_fnv64(&pcm), 0xed63_043a_f310_9f1d, "input PCM differs from oracle WAV data chunk");

    let options = GenerateOptions {
        sample_rate: 32_000,
        channels: 2,
        divisions: vec![106, 1696, 32224],
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: true,
    };
    let parsed = ReaPeaks::parse(generate_pcm16(&pcm, &options).unwrap()).unwrap();
    let got = &parsed.spectral_layers[0].peaks;
    assert!(got.len() >= EXPECTED_FIRST64.len());

    for (index, (&expected, peak)) in EXPECTED_FIRST64.iter().zip(got.iter()).enumerate() {
        let actual = peak.code();
        assert_eq!(
            actual,
            expected,
            "first mismatch index={index} frame={} channel={} actual=0x{actual:08x} freq={} density={} expected=0x{expected:08x} freq={} density={}",
            index / 2,
            index % 2,
            peak.frequency_hz,
            peak.density,
            expected & 0x7fff,
            (expected >> 15) & 0x3fff,
        );
    }
}
