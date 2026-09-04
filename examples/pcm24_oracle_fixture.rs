use reapeaks::{
    default_divisions, generate_pcm24_i32_reaper, generate_pcm24_reaper, GenerateOptions,
    ReaperPeakMode, SourceStamp,
};
use std::env;
use std::fs;
use std::path::PathBuf;

fn parse_mode(value: &str) -> Result<ReaperPeakMode, Box<dyn std::error::Error>> {
    match value {
        "waveform" => Ok(ReaperPeakMode::Waveform),
        "spectral" => Ok(ReaperPeakMode::Spectral),
        "spectrogram" => Ok(ReaperPeakMode::Spectrogram),
        _ => Err(format!("unknown REAPER peak mode: {value}").into()),
    }
}

fn decode_pcm24le(raw: &[u8]) -> Result<Vec<i32>, Box<dyn std::error::Error>> {
    let (chunks, remainder) = raw.as_chunks::<3>();
    let mut out = Vec::with_capacity(raw.len() / 3);
    for chunk in chunks {
        let sign = if chunk[2] & 0x80 != 0 { 0xff } else { 0x00 };
        out.push(i32::from_le_bytes([chunk[0], chunk[1], chunk[2], sign]));
    }
    if !remainder.is_empty() {
        return Err("PCM24LE fixture byte length is not divisible by three".into());
    }
    Ok(out)
}

fn options_for(source: &PathBuf) -> Result<GenerateOptions, Box<dyn std::error::Error>> {
    const SAMPLE_RATE: u32 = 48_000;
    const CHANNELS: usize = 2;
    let stamp = SourceStamp::from_path(source)?;
    Ok(GenerateOptions {
        sample_rate: SAMPLE_RATE,
        channels: CHANNELS,
        divisions: default_divisions(SAMPLE_RATE, 300).to_vec(),
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: false,
    }
    .with_source_stamp(stamp))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args_os().skip(1);
    let source24 = PathBuf::from(args.next().ok_or("missing 24-bit source WAV path")?);
    let source32 = PathBuf::from(args.next().ok_or("missing 32-bit source WAV path")?);
    let pcm_path = PathBuf::from(args.next().ok_or("missing PCM24LE path")?);
    let packed_output = PathBuf::from(args.next().ok_or("missing packed output path")?);
    let i32_output = PathBuf::from(args.next().ok_or("missing i32 output path")?);
    let mode_arg = args.next().ok_or("missing REAPER peak mode")?;
    let mode = parse_mode(&mode_arg.to_string_lossy())?;
    if args.next().is_some() {
        return Err("unexpected extra arguments".into());
    }

    let raw = fs::read(&pcm_path)?;
    let pcm_i32 = decode_pcm24le(&raw)?;
    const CHANNELS: usize = 2;
    if pcm_i32.len() % CHANNELS != 0 {
        return Err("PCM24LE fixture does not contain complete stereo frames".into());
    }

    let packed = generate_pcm24_reaper(&raw, &options_for(&source24)?, mode)?;
    let i32_cache = generate_pcm24_i32_reaper(&pcm_i32, &options_for(&source32)?, mode)?;
    fs::write(packed_output, packed)?;
    fs::write(i32_output, i32_cache)?;
    Ok(())
}
