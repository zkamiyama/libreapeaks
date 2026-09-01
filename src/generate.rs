use crate::error::{ReaPeaksError, Result};
use crate::format::{encode, GeneratedLayer, Version};
use crate::loudness::{build_loudness_layers_f32, build_loudness_layers_pcm16};
use crate::spectral::{build_spectral_layers, build_spectral_layers_f32};
use crate::spectrogram_generate::build_spectrogram_layers_pcm16;
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

fn validate(options: &GenerateOptions, sample_len: usize, loudness: bool) -> Result<usize> {
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
    if loudness && !options.spectral {
        return Err(ReaPeaksError::InvalidArgument(
            "mode-3 generation requires spectral=true",
        ));
    }
    if loudness && options.divisions.len() < 2 {
        return Err(ReaPeaksError::InvalidArgument(
            "mode-3 generation requires at least two divisions",
        ));
    }

    let spectral_count = if options.spectral {
        options.divisions.len()
    } else {
        0
    };
    let loudness_count = if loudness {
        options.divisions.len() - 1
    } else {
        0
    };
    let layer_count = options
        .divisions
        .len()
        .checked_add(spectral_count)
        .and_then(|count| count.checked_add(loudness_count))
        .ok_or(ReaPeaksError::InvalidArgument("too many layers"))?;
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

fn validate_spectrogram_layer_count(options: &GenerateOptions) -> Result<()> {
    let layer_count = options
        .divisions
        .len()
        .checked_mul(4)
        .and_then(|count| count.checked_sub(2))
        .ok_or(ReaPeaksError::InvalidArgument("too many layers"))?;
    if layer_count > u8::MAX as usize {
        return Err(ReaPeaksError::InvalidArgument("too many layers"));
    }
    Ok(())
}

fn encode_generated(
    version: Version,
    options: &GenerateOptions,
    layers: &[GeneratedLayer],
) -> Result<Vec<u8>> {
    encode(
        version,
        options.channels as u8,
        options.sample_rate,
        options.source_mtime_low32,
        options.source_size_low32,
        layers,
    )
}

fn generate_pcm16_impl(pcm: &[i16], options: &GenerateOptions, loudness: bool) -> Result<Vec<u8>> {
    let frames = validate(options, pcm.len(), loudness)?;
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
    if loudness {
        layers.extend(build_loudness_layers_pcm16(
            pcm,
            frames,
            options.channels,
            options.sample_rate,
            &options.divisions,
        )?);
    }
    encode_generated(Version::Rpkn, options, &layers)
}

/// Generate RPKN v1.1 from interleaved decoded PCM16.
///
/// Waveform generation is byte-exact against the REAPER 7.79 Linux oracle for
/// the validated corpus. Spectral generation is closest with `strict-wdl`.
///
/// This legacy entry point writes waveform and optional spectral layers only.
/// Use [`generate_pcm16_mode3`] for REAPER mode-3 waveform, spectral and
/// loudness output.
pub fn generate_pcm16(pcm: &[i16], options: &GenerateOptions) -> Result<Vec<u8>> {
    generate_pcm16_impl(pcm, options, false)
}

/// Generate a complete REAPER peak-cache mode-3 RPKN file.
///
/// `options.spectral` must be `true`. The resulting layer order matches REAPER
/// 7.79: waveform layers, mirrored `-'s'` spectral layers, then `-'r'`
/// loudness layers for every waveform division except the finest.
pub fn generate_pcm16_mode3(pcm: &[i16], options: &GenerateOptions) -> Result<Vec<u8>> {
    generate_pcm16_impl(pcm, options, true)
}

/// Generate REAPER peak-cache mode-3 RPKN with `-'g'` spectrogram layers.
///
/// This is intentionally separate from [`generate_pcm16_mode3`] so enabling
/// spectrogram generation cannot change the byte-exact legacy mode-3 path.
/// `options.spectral` must be `true`, and waveform divisions must be nested
/// integer multiples. The resulting layer order matches REAPER 7.79:
/// waveform, mirrored `-'s'`, mirrored `-'g'`, then `-'r'` layers.
pub fn generate_pcm16_mode3_with_spectrogram(
    pcm: &[i16],
    options: &GenerateOptions,
) -> Result<Vec<u8>> {
    let frames = validate(options, pcm.len(), true)?;
    validate_spectrogram_layer_count(options)?;

    let mut layers = build_wave_layers(pcm, frames, options.channels, &options.divisions)?;
    layers.extend(build_spectral_layers(
        pcm,
        frames,
        options.channels,
        options.sample_rate,
        &options.divisions,
    )?);
    layers.extend(build_spectrogram_layers_pcm16(
        pcm,
        frames,
        options.channels,
        &options.divisions,
    )?);
    layers.extend(build_loudness_layers_pcm16(
        pcm,
        frames,
        options.channels,
        options.sample_rate,
        &options.divisions,
    )?);
    encode_generated(Version::Rpkn, options, &layers)
}

fn generate_f32_impl(
    pcm: &[f32],
    options: &GenerateOptions,
    large_range: bool,
    loudness: bool,
) -> Result<Vec<u8>> {
    let frames = validate(options, pcm.len(), loudness)?;
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
    if loudness {
        layers.extend(build_loudness_layers_f32(
            pcm,
            frames,
            options.channels,
            options.sample_rate,
            &options.divisions,
        )?);
    }
    encode_generated(version, options, &layers)
}

/// Generate from decoded float32 samples.
///
/// `large_range=true` writes RPKL v1.2 (the format REAPER uses for floating
/// media and, on REAPER 7.79 Linux, MP3/Vorbis/Opus sources). `false` writes
/// RPKN and is useful for decoded integer PCM such as 24/32-bit WAV or FLAC.
///
/// This legacy entry point writes waveform and optional spectral layers only.
/// Use [`generate_f32_mode3`] for complete mode-3 output.
pub fn generate_f32(pcm: &[f32], options: &GenerateOptions, large_range: bool) -> Result<Vec<u8>> {
    generate_f32_impl(pcm, options, large_range, false)
}

/// Generate complete REAPER mode-3 output from decoded float32 samples.
///
/// `options.spectral` must be `true`. `large_range` selects RPKL versus RPKN
/// waveform encoding exactly as in [`generate_f32`].
pub fn generate_f32_mode3(
    pcm: &[f32],
    options: &GenerateOptions,
    large_range: bool,
) -> Result<Vec<u8>> {
    generate_f32_impl(pcm, options, large_range, true)
}
