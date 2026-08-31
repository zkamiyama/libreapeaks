use crate::error::{ReaPeaksError, Result};
use crate::format::{encode, GeneratedLayer, Version};
use crate::spectral::{build_spectral_layers, build_spectral_layers_f32};
use crate::wave::{build_wave_layers, build_wave_layers_f32, WaveEncoding};

#[derive(Debug, Clone)]
pub struct GenerateOptions {
    pub sample_rate: u32,
    pub channels: usize,
    pub divisions: Vec<u32>,
    pub source_mtime_low32: u32,
    pub source_size_low32: u32,
    pub spectral: bool,
}

fn validate(options: &GenerateOptions, sample_len: usize) -> Result<usize> {
    if options.channels == 0 {
        return Err(ReaPeaksError::InvalidArgument("channels=0"));
    }
    if options.channels > u8::MAX as usize {
        return Err(ReaPeaksError::InvalidArgument(
            "channels exceed .ReaPeaks header capacity",
        ));
    }
    if options.sample_rate == 0 {
        return Err(ReaPeaksError::InvalidArgument("sample_rate=0"));
    }
    if options.divisions.is_empty() {
        return Err(ReaPeaksError::InvalidArgument("no waveform divisions"));
    }
    if options.divisions.iter().any(|&x| x == 0) {
        return Err(ReaPeaksError::InvalidArgument("division=0"));
    }
    if options.divisions.iter().any(|&x| x > i32::MAX as u32) {
        return Err(ReaPeaksError::InvalidArgument(
            "division exceeds signed .ReaPeaks range",
        ));
    }
    let layer_count = if options.spectral {
        options
            .divisions
            .len()
            .checked_mul(2)
            .ok_or(ReaPeaksError::InvalidArgument("too many layers"))?
    } else {
        options.divisions.len()
    };
    if layer_count > u8::MAX as usize {
        return Err(ReaPeaksError::InvalidArgument("too many layers"));
    }
    if sample_len % options.channels != 0 {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM is not whole interleaved frames",
        ));
    }
    Ok(sample_len / options.channels)
}
/// Generate RPKN v1.1 from interleaved decoded PCM16.
///
/// Waveform generation is byte-exact against the REAPER 7.79 Linux oracle for
/// the validated corpus. Spectral generation is closest with `strict-wdl`.
pub fn generate_pcm16(pcm: &[i16], options: &GenerateOptions) -> Result<Vec<u8>> {
    let frames = validate(options, pcm.len())?;
    let mut layers: Vec<GeneratedLayer> =
        build_wave_layers(pcm, frames, options.channels, &options.divisions)?;
    if options.spectral {
        layers.extend(build_spectral_layers(
            pcm,
            frames,
            options.channels,
            options.sample_rate,
            &options.divisions,
        )?);
    }
    encode(
        Version::Rpkn,
        options.channels as u8,
        options.sample_rate,
        options.source_mtime_low32,
        options.source_size_low32,
        &layers,
    )
}

/// Generate from decoded float32 samples.
///
/// `large_range=true` writes RPKL v1.2 (the format REAPER uses for floating
/// media and, on REAPER 7.79 Linux, MP3/Vorbis/Opus sources). `false` writes
/// RPKN and is useful for decoded integer PCM such as 24/32-bit WAV or FLAC.
pub fn generate_f32(pcm: &[f32], options: &GenerateOptions, large_range: bool) -> Result<Vec<u8>> {
    let frames = validate(options, pcm.len())?;
    let encoding = if large_range {
        WaveEncoding::Rpkl
    } else {
        WaveEncoding::Rpkn
    };
    let version = if large_range {
        Version::Rpkl
    } else {
        Version::Rpkn
    };
    let mut layers =
        build_wave_layers_f32(pcm, frames, options.channels, &options.divisions, encoding)?;
    if options.spectral {
        layers.extend(build_spectral_layers_f32(
            pcm,
            frames,
            options.channels,
            options.sample_rate,
            &options.divisions,
        )?);
    }
    encode(
        version,
        options.channels as u8,
        options.sample_rate,
        options.source_mtime_low32,
        options.source_size_low32,
        &layers,
    )
}
