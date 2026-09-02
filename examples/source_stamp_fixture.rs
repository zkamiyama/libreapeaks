use reapeaks::{
    default_divisions, generate_pcm16_reaper, GenerateOptions, ReaperPeakMode, SourceStamp,
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

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args_os().skip(1);
    let source = PathBuf::from(args.next().ok_or("missing source path")?);
    let pcm_path = PathBuf::from(args.next().ok_or("missing PCM16LE path")?);
    let output = PathBuf::from(args.next().ok_or("missing output path")?);
    let mode_arg = args.next().ok_or("missing REAPER peak mode")?;
    let mode = parse_mode(&mode_arg.to_string_lossy())?;
    if args.next().is_some() {
        return Err("unexpected extra arguments".into());
    }

    let raw = fs::read(&pcm_path)?;
    if raw.len() % 2 != 0 {
        return Err("PCM16LE fixture has odd byte length".into());
    }
    let pcm: Vec<i16> = raw
        .chunks_exact(2)
        .map(|sample| i16::from_le_bytes([sample[0], sample[1]]))
        .collect();

    let stamp = SourceStamp::from_path(&source)?;
    let options = GenerateOptions {
        sample_rate: 48_000,
        channels: 1,
        divisions: default_divisions(48_000, 300).to_vec(),
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: false,
    }
    .with_source_stamp(stamp);
    let blob = generate_pcm16_reaper(&pcm, &options, mode)?;
    fs::write(output, blob)?;
    Ok(())
}
