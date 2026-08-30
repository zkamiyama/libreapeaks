#![allow(clippy::chunks_exact_to_as_chunks, clippy::manual_repeat_n)]

use reapeaks::{generate_f32, generate_pcm16, GenerateOptions, ReaPeaks, SpectralPeak};
use std::collections::BTreeMap;
use std::f64::consts::PI;

fn q16(x: f64) -> i16 {
    (x.clamp(-1.0, 1.0) * 32767.0)
        .round()
        .clamp(-32768.0, 32767.0) as i16
}

fn tone_1234_5() -> Vec<i16> {
    let sr = 44_100.0;
    (0..44_100)
        .map(|i| q16(0.8 * (2.0 * PI * 1234.5 * i as f64 / sr).sin()))
        .collect()
}

fn twotone_minus6() -> Vec<i16> {
    let sr = 44_100.0;
    let ratio = 10.0f64.powf(-6.0 / 20.0);
    let scale = 0.8 / (1.0 + ratio);
    (0..44_100)
        .map(|i| {
            let t = i as f64 / sr;
            q16(scale * ((2.0 * PI * 1000.0 * t).sin() + ratio * (2.0 * PI * 3000.0 * t).sin()))
        })
        .collect()
}

fn impulse_1024() -> Vec<i16> {
    let mut x = vec![0i16; 5000];
    x[1024] = q16(0.9);
    x
}

fn hex_decode(s: &str) -> Vec<u8> {
    assert_eq!(s.len() & 1, 0);
    fn nibble(x: u8) -> u8 {
        match x {
            b'0'..=b'9' => x - b'0',
            b'a'..=b'f' => x - b'a' + 10,
            b'A'..=b'F' => x - b'A' + 10,
            _ => panic!("bad hex"),
        }
    }
    s.as_bytes()
        .chunks_exact(2)
        .map(|p| (nibble(p[0]) << 4) | nibble(p[1]))
        .collect()
}

fn golden(text: &str) -> BTreeMap<&str, &str> {
    text.lines()
        .filter(|x| !x.is_empty())
        .map(|line| line.split_once('=').unwrap())
        .collect()
}

fn compare_pcm16_golden(pcm: Vec<i16>, text: &str) {
    let g = golden(text);
    let sr: u32 = g["sample_rate"].parse().unwrap();
    let ch: usize = g["channels"].parse().unwrap();
    let divs: Vec<u32> = g["divisions"].split(',').map(|x| x.parse().unwrap()).collect();
    let opt = GenerateOptions {
        sample_rate: sr,
        channels: ch,
        divisions: divs,
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: true,
    };
    let actual = ReaPeaks::parse(generate_pcm16(&pcm, &opt).unwrap()).unwrap();
    for (i, layer) in actual.wave_layers.iter().enumerate() {
        let mut bytes = Vec::with_capacity(layer.peaks.len() * 4);
        for p in &layer.peaks {
            bytes.extend_from_slice(&p.max.to_le_bytes());
            bytes.extend_from_slice(&p.min.to_le_bytes());
        }
        assert_eq!(bytes, hex_decode(g[format!("wave{i}").as_str()]), "wave layer {i}");
    }

    #[cfg(feature = "strict-wdl")]
    for (i, layer) in actual.spectral_layers.iter().enumerate() {
        let mut bytes = Vec::with_capacity(layer.peaks.len() * 4);
        for p in &layer.peaks {
            bytes.extend_from_slice(&p.code().to_le_bytes());
        }
        assert_eq!(bytes, hex_decode(g[format!("spectral{i}").as_str()]), "spectral layer {i}");
    }
}

#[test]
fn reaper779_tone_1234_5() {
    compare_pcm16_golden(tone_1234_5(), include_str!("fixtures/tone_f1234_5.golden"));
}

#[test]
fn reaper779_two_tone_minus6() {
    compare_pcm16_golden(twotone_minus6(), include_str!("fixtures/twotone_minus6.golden"));
}

#[test]
fn reaper779_impulse_boundary() {
    compare_pcm16_golden(impulse_1024(), include_str!("fixtures/imp_1024.golden"));
}

#[test]
fn reaper779_rpkl_high_range_and_bucket_initialization() {
    let values: [f32; 22] = [
        1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 255.0, 256.0, 512.0,
        -1.0, -2.0, -4.0, -8.0, -16.0, -32.0, -64.0, -128.0, -255.0, -256.0,
        -512.0,
    ];
    let mut pcm = Vec::with_capacity(values.len() * 147);
    for v in values {
        pcm.extend(std::iter::repeat(v).take(147));
    }
    let opt = GenerateOptions {
        sample_rate: 44100,
        channels: 1,
        divisions: vec![147],
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: false,
    };
    let actual = ReaPeaks::parse(generate_f32(&pcm, &opt, true).unwrap()).unwrap();
    let got: Vec<(i16, i16)> = actual.wave_layers[0].peaks.iter().map(|p| (p.max, p.min)).collect();
    let expected = [
        (24576,24576),(25600,24576),(26624,24576),(27648,24576),(28672,24576),
        (29696,24576),(30720,24576),(31744,24576),(32762,24576),(32767,24576),
        (32767,24576),(-24576,-24576),(-24576,-25600),(-24576,-26624),
        (-24576,-27648),(-24576,-28672),(-24576,-29696),(-24576,-30720),
        (-24576,-31744),(-24576,-32762),(-24576,-32768),(-24576,-32768),
    ];
    assert_eq!(got, expected);
}

#[test]
fn spectral_code_roundtrip_bits() {
    for p in [
        SpectralPeak { frequency_hz: 0, density: 0 },
        SpectralPeak { frequency_hz: 1000, density: 16383 },
        SpectralPeak { frequency_hz: 32767, density: 12288 },
    ] {
        assert_eq!(SpectralPeak::from_code(p.code()), p);
    }
}
