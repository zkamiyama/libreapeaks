use crate::error::{ReaPeaksError, Result};
use crate::format::{encode, GeneratedLayer, Version};
use crate::loudness::{
    build_loudness_layers_f32_source, build_loudness_layers_pcm16,
};
use crate::sample_source::F32SampleSource;
use crate::spectral::{build_spectral_layers, build_spectral_layers_f32_source};
use crate::spectrogram_generate::build_spectrogram_layers_pcm16;
use crate::spectrogram_generate_f32::build_spectrogram_layers_f32_source;
use crate::wave::{build_wave_layers, build_wave_layers_f32_source, WaveEncoding};

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

pub fn generate_pcm16(pcm: &[i16], options: &GenerateOptions) -> Result<Vec<u8>> {
    generate_pcm16_impl(pcm, options, false)
}

pub fn generate_pcm16_mode3(pcm: &[i16], options: &GenerateOptions) -> Result<Vec<u8>> {
    generate_pcm16_impl(pcm, options, true)
}

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

pub(crate) fn generate_f32_source<S: F32SampleSource + ?Sized>(
    pcm: &S,
    options: &GenerateOptions,
    large_range: bool,
) -> Result<Vec<u8>> {
    generate_f32_source_impl(pcm, options, large_range, false)
}

pub(crate) fn generate_f32_source_mode3<S: F32SampleSource + ?Sized>(
    pcm: &S,
    options: &GenerateOptions,
    large_range: bool,
) -> Result<Vec<u8>> {
    generate_f32_source_impl(pcm, options, large_range, true)
}

fn generate_f32_source_impl<S: F32SampleSource + ?Sized>(
    pcm: &S,
    options: &GenerateOptions,
    large_range: bool,
    loudness: bool,
) -> Result<Vec<u8>> {
    let frames = validate(options, pcm.sample_len(), loudness)?;
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
        build_wave_layers_f32_source(pcm, frames, options.channels, &options.divisions, encoding)?;
    if options.spectral {
        layers.extend(build_spectral_layers_f32_source(
            pcm,
            frames,
            options.channels,
            options.sample_rate,
            &options.divisions,
        )?);
    }
    if loudness {
        layers.extend(build_loudness_layers_f32_source(
            pcm,
            frames,
            options.channels,
            options.sample_rate,
            &options.divisions,
        )?);
    }
    encode_generated(version, options, &layers)
}

pub fn generate_f32(pcm: &[f32], options: &GenerateOptions, large_range: bool) -> Result<Vec<u8>> {
    generate_f32_source(pcm, options, large_range)
}

pub fn generate_f32_mode3(
    pcm: &[f32],
    options: &GenerateOptions,
    large_range: bool,
) -> Result<Vec<u8>> {
    generate_f32_source_mode3(pcm, options, large_range)
}

/// Generate float32 REAPER-shaped mode-3 output including `-'g'` spectrogram layers.
///
/// The waveform encoding is RPKL when `large_range=true` and RPKN otherwise.
/// Against the pinned REAPER 7.79 Linux x86_64 oracle, the float32/RPKL `-'g'`
/// layer path is byte-exact for the permanent 128-case adversarial matrix:
/// decoded 128-bin frames and packed payload bytes both match exactly. This is
/// a `-'g'` compatibility claim, not a claim about every RPKL waveform rounding
/// edge or arbitrary NaN/Inf/subnormal behavior. Non-finite source samples are
/// sanitized to zero for spectrogram analysis so hostile float media cannot
/// poison FFT output or panic generation.
pub fn generate_f32_mode3_with_spectrogram(
    pcm: &[f32],
    options: &GenerateOptions,
    large_range: bool,
) -> Result<Vec<u8>> {
    generate_f32_source_mode3_with_spectrogram(pcm, options, large_range)
}

pub(crate) fn generate_f32_source_mode3_with_spectrogram<S: F32SampleSource + ?Sized>(
    pcm: &S,
    options: &GenerateOptions,
    large_range: bool,
) -> Result<Vec<u8>> {
    let frames = validate(options, pcm.sample_len(), true)?;
    validate_spectrogram_layer_count(options)?;
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
        build_wave_layers_f32_source(pcm, frames, options.channels, &options.divisions, encoding)?;
    layers.extend(build_spectral_layers_f32_source(
        pcm,
        frames,
        options.channels,
        options.sample_rate,
        &options.divisions,
    )?);
    layers.extend(build_spectrogram_layers_f32_source(
        pcm,
        frames,
        options.channels,
        &options.divisions,
    )?);
    layers.extend(build_loudness_layers_f32_source(
        pcm,
        frames,
        options.channels,
        options.sample_rate,
        &options.divisions,
    )?);
    encode_generated(version, options, &layers)
}
