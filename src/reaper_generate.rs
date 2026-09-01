use crate::error::{ReaPeaksError, Result};
use crate::generate::{
    generate_f32, generate_f32_mode3, generate_pcm16, generate_pcm16_mode3,
    generate_pcm16_mode3_with_spectrogram, GenerateOptions,
};

/// REAPER 7.79 peak-display/cache generation modes observed by the live oracle.
///
/// These are deliberately modes rather than independent layer bits. With
/// `peakcachegenmode=3`, REAPER 7.79 generated exactly these cache shapes in a
/// 71-configuration `showpeaks` sweep:
///
/// - `Waveform`: positive waveform mipmaps only;
/// - `Spectral`: waveform + mirrored `-'s'` spectral + `-'r'` loudness;
/// - `Spectrogram`: waveform + mirrored `-'s'` + mirrored `-'g'` + `-'r'`.
///
/// No `-'s'`-only, `-'g'`-only, or `-'r'`-only cache was observed, so this API
/// intentionally does not expose arbitrary independent layer toggles.
#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReaperPeakMode {
    Waveform = 0,
    Spectral = 1,
    Spectrogram = 2,
}

impl TryFrom<u8> for ReaperPeakMode {
    type Error = ReaPeaksError;

    fn try_from(value: u8) -> Result<Self> {
        match value {
            0 => Ok(Self::Waveform),
            1 => Ok(Self::Spectral),
            2 => Ok(Self::Spectrogram),
            _ => Err(ReaPeaksError::InvalidArgument("unknown REAPER peak mode")),
        }
    }
}

fn with_spectral(options: &GenerateOptions, spectral: bool) -> GenerateOptions {
    let mut out = options.clone();
    out.spectral = spectral;
    out
}

/// Generate one of the REAPER-native PCM16 peak-cache modes in a single call.
pub fn generate_pcm16_reaper(
    pcm: &[i16],
    options: &GenerateOptions,
    mode: ReaperPeakMode,
) -> Result<Vec<u8>> {
    match mode {
        ReaperPeakMode::Waveform => generate_pcm16(pcm, &with_spectral(options, false)),
        ReaperPeakMode::Spectral => generate_pcm16_mode3(pcm, &with_spectral(options, true)),
        ReaperPeakMode::Spectrogram => {
            generate_pcm16_mode3_with_spectrogram(pcm, &with_spectral(options, true))
        }
    }
}

/// Generate one of the REAPER-native float32 peak-cache modes in a single call.
///
/// `Waveform` and `Spectral` are supported for RPKN/RPKL output. Exact `-'g'`
/// generation is currently implemented only for PCM16, so `Spectrogram`
/// returns `Unsupported` instead of silently changing sample representation.
pub fn generate_f32_reaper(
    pcm: &[f32],
    options: &GenerateOptions,
    large_range: bool,
    mode: ReaperPeakMode,
) -> Result<Vec<u8>> {
    match mode {
        ReaperPeakMode::Waveform => generate_f32(pcm, &with_spectral(options, false), large_range),
        ReaperPeakMode::Spectral => {
            generate_f32_mode3(pcm, &with_spectral(options, true), large_range)
        }
        ReaperPeakMode::Spectrogram => Err(ReaPeaksError::Unsupported(
            "float32 REAPER spectrogram generation",
        )),
    }
}
