use crate::error::{ReaPeaksError, Result};
use crate::generate::{
    generate_f32, generate_f32_mode3, generate_f32_mode3_with_spectrogram, generate_f32_source,
    generate_f32_source_mode3, generate_f32_source_mode3_with_spectrogram, generate_pcm16,
    generate_pcm16_mode3, generate_pcm16_mode3_with_spectrogram, GenerateOptions,
};
use crate::sample_source::{F32SampleSource, Pcm24I32Source, Pcm24LeSource};

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

fn generate_f32_source_reaper<S: F32SampleSource + ?Sized>(
    pcm: &S,
    options: &GenerateOptions,
    large_range: bool,
    mode: ReaperPeakMode,
) -> Result<Vec<u8>> {
    match mode {
        ReaperPeakMode::Waveform => {
            generate_f32_source(pcm, &with_spectral(options, false), large_range)
        }
        ReaperPeakMode::Spectral => {
            generate_f32_source_mode3(pcm, &with_spectral(options, true), large_range)
        }
        ReaperPeakMode::Spectrogram => generate_f32_source_mode3_with_spectrogram(
            pcm,
            &with_spectral(options, true),
            large_range,
        ),
    }
}

/// Generate one of the REAPER-native float32 peak-cache modes in a single call.
///
/// Waveform and spectral modes retain their established paths. Spectrogram mode
/// emits waveform + `-'s'` + `-'g'` + `-'r'` for float media. With RPKL output,
/// the `-'g'` layers are byte-exact against the pinned REAPER 7.79 Linux x86_64
/// executable for the permanent 128-case adversarial oracle: decoded 128-bin
/// frames and packed payload bytes both match exactly. The claim remains scoped
/// to the tested `-'g'` path rather than arbitrary float exceptional values or
/// every RPKL waveform rounding edge.
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
        ReaperPeakMode::Spectrogram => {
            generate_f32_mode3_with_spectrogram(pcm, &with_spectral(options, true), large_range)
        }
    }
}

/// Generate a REAPER-style RPKN cache directly from packed signed PCM24LE.
///
/// Input samples are interleaved three-byte little-endian signed integers. Each
/// sample is normalized on demand to the exact f32 value `sample / 2^23`, so a
/// caller that already caches PCM24 does not need to allocate a whole-file f32
/// copy. The RPKN waveform mapping is the one validated against 50,000 decoded
/// PCM24 REAPER 7.79 oracle buckets.
pub fn generate_pcm24_reaper(
    pcm24le: &[u8],
    options: &GenerateOptions,
    mode: ReaperPeakMode,
) -> Result<Vec<u8>> {
    let source = Pcm24LeSource::new(pcm24le)?;
    generate_f32_source_reaper(&source, options, false, mode)
}

/// Generate a REAPER-style RPKN cache from signed PCM24 values stored in i32.
///
/// Values must be right-justified/sign-extended integers in
/// `-8_388_608..=8_388_607`. Left-aligned S24-in-S32 buffers should be shifted
/// right by eight bits by the caller. Samples are normalized on demand; no
/// whole-file f32 intermediate is materialized.
pub fn generate_pcm24_i32_reaper(
    pcm: &[i32],
    options: &GenerateOptions,
    mode: ReaperPeakMode,
) -> Result<Vec<u8>> {
    let source = Pcm24I32Source::new(pcm)?;
    generate_f32_source_reaper(&source, options, false, mode)
}
