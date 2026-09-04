use reapeaks::{
    default_divisions, generate_f32_reaper, generate_pcm16_reaper, generate_pcm24_reaper,
    GenerateOptions, ReaperPeakMode,
};
use std::hint::black_box;
use std::time::Instant;

#[derive(Clone, Copy)]
enum SampleKind {
    Pcm16,
    Float32,
    Pcm24,
}

#[derive(Clone, Copy)]
struct BenchCase {
    name: &'static str,
    sample_rate: u32,
    channels: usize,
    seconds: usize,
    mode: ReaperPeakMode,
    kind: SampleKind,
}

const CASES: &[BenchCase] = &[
    BenchCase {
        name: "pcm16-wave-48k-stereo-60s",
        sample_rate: 48_000,
        channels: 2,
        seconds: 60,
        mode: ReaperPeakMode::Waveform,
        kind: SampleKind::Pcm16,
    },
    BenchCase {
        name: "pcm16-spectral-48k-stereo-30s",
        sample_rate: 48_000,
        channels: 2,
        seconds: 30,
        mode: ReaperPeakMode::Spectral,
        kind: SampleKind::Pcm16,
    },
    BenchCase {
        name: "pcm16-spectrogram-48k-stereo-20s",
        sample_rate: 48_000,
        channels: 2,
        seconds: 20,
        mode: ReaperPeakMode::Spectrogram,
        kind: SampleKind::Pcm16,
    },
    BenchCase {
        name: "pcm16-spectrogram-96k-6ch-8s",
        sample_rate: 96_000,
        channels: 6,
        seconds: 8,
        mode: ReaperPeakMode::Spectrogram,
        kind: SampleKind::Pcm16,
    },
    BenchCase {
        name: "pcm24-spectral-96k-stereo-15s",
        sample_rate: 96_000,
        channels: 2,
        seconds: 15,
        mode: ReaperPeakMode::Spectral,
        kind: SampleKind::Pcm24,
    },
    BenchCase {
        name: "f32-spectrogram-48k-stereo-15s",
        sample_rate: 48_000,
        channels: 2,
        seconds: 15,
        mode: ReaperPeakMode::Spectrogram,
        kind: SampleKind::Float32,
    },
];

enum BenchInput {
    Pcm16(Vec<i16>),
    Float32(Vec<f32>),
    Pcm24(Vec<u8>),
}

fn next_state(state: &mut u64) -> u64 {
    *state = state
        .wrapping_mul(6_364_136_223_846_793_005)
        .wrapping_add(1_442_695_040_888_963_407);
    *state
}

fn make_input(case: BenchCase) -> BenchInput {
    let frames = case.sample_rate as usize * case.seconds;
    let samples = frames * case.channels;
    let mut state = 0x7a4d_2c91_35e8_b607u64;
    match case.kind {
        SampleKind::Pcm16 => BenchInput::Pcm16(
            (0..samples)
                .map(|_| (next_state(&mut state) >> 48) as i16)
                .collect(),
        ),
        SampleKind::Float32 => BenchInput::Float32(
            (0..samples)
                .map(|_| {
                    let signed = (next_state(&mut state) >> 32) as u32 as i32;
                    (signed as f32) * (1.0 / 2_147_483_648.0)
                })
                .collect(),
        ),
        SampleKind::Pcm24 => {
            let mut bytes = Vec::with_capacity(samples * 3);
            for _ in 0..samples {
                let sample = ((next_state(&mut state) >> 40) as u32) & 0x00ff_ffff;
                bytes.push(sample as u8);
                bytes.push((sample >> 8) as u8);
                bytes.push((sample >> 16) as u8);
            }
            BenchInput::Pcm24(bytes)
        }
    }
}

fn options(case: BenchCase, input_bytes: usize) -> GenerateOptions {
    GenerateOptions {
        sample_rate: case.sample_rate,
        channels: case.channels,
        divisions: default_divisions(case.sample_rate, 300).to_vec(),
        source_mtime_low32: 0,
        source_size_low32: input_bytes as u32,
        spectral: !matches!(case.mode, ReaperPeakMode::Waveform),
    }
}

fn generate(case: BenchCase, input: &BenchInput) -> Vec<u8> {
    match input {
        BenchInput::Pcm16(pcm) => generate_pcm16_reaper(
            black_box(pcm.as_slice()),
            &options(case, pcm.len() * 2),
            case.mode,
        )
        .unwrap(),
        BenchInput::Float32(pcm) => generate_f32_reaper(
            black_box(pcm.as_slice()),
            &options(case, pcm.len() * 4),
            false,
            case.mode,
        )
        .unwrap(),
        BenchInput::Pcm24(pcm) => generate_pcm24_reaper(
            black_box(pcm.as_slice()),
            &options(case, pcm.len()),
            case.mode,
        )
        .unwrap(),
    }
}

fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for &byte in bytes {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

fn median(mut values: Vec<u128>) -> u128 {
    values.sort_unstable();
    values[values.len() / 2]
}

fn main() {
    let mut args = std::env::args().skip(1);
    let case_name = args.next().unwrap_or_else(|| {
        eprintln!("usage: optimization_benchmark CASE [ITERATIONS]");
        std::process::exit(2);
    });
    let iterations = args
        .next()
        .map(|value| value.parse::<usize>().expect("valid iteration count"))
        .unwrap_or(3)
        .max(1);
    let case = CASES
        .iter()
        .copied()
        .find(|case| case.name == case_name)
        .unwrap_or_else(|| {
            eprintln!("unknown case: {case_name}");
            for case in CASES {
                eprintln!("  {}", case.name);
            }
            std::process::exit(2);
        });

    let input = make_input(case);
    let warmup = generate(case, &input);
    black_box(warmup.len());
    let expected_len = warmup.len();
    let expected_hash = fnv1a64(&warmup);
    drop(warmup);

    let mut samples = Vec::with_capacity(iterations);
    for _ in 0..iterations {
        let start = Instant::now();
        let output = generate(case, &input);
        let elapsed = start.elapsed().as_nanos();
        black_box(output.len());
        assert_eq!(
            output.len(),
            expected_len,
            "output length changed between runs"
        );
        assert_eq!(
            fnv1a64(&output),
            expected_hash,
            "output bytes changed between runs"
        );
        samples.push(elapsed);
    }

    let median_ns = median(samples.clone());
    let samples_json = samples
        .iter()
        .map(u128::to_string)
        .collect::<Vec<_>>()
        .join(",");
    println!(
        "{{\"case\":\"{}\",\"median_ns\":{},\"bytes\":{},\"hash\":\"{:016x}\",\"samples_ns\":[{}]}}",
        case.name, median_ns, expected_len, expected_hash, samples_json
    );
}
