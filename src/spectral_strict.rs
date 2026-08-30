//! REAPER 7.79 strict compatibility shim for spectral generation.
//!
//! REAPER has a non-DSP quirk for source rates <= 22.05 kHz: it still emits
//! spectral mipmap structures, but every spectral code is zero. Keep that
//! behavior out of the normal implementation and intercept it only when the
//! `strict-wdl` feature selects this module.
//!
//! REAPER's fine spectral scheduler is centered on the 22.05 kHz analysis
//! domain with a 512-analysis-sample half-window. Strict mode overrides only
//! the expected output count while leaving the original media length passed to
//! WDL_Resampler untouched; declaring padded source frames changes WDL's edge
//! response and therefore does not match REAPER.

use crate::error::{ReaPeaksError, Result};
use crate::format::{GeneratedLayer, LayerHeader, SpectralPeak, TOKEN_SPECTRAL};

const REAPER_ZERO_SPECTRAL_MAX_RATE: u32 = 22_050;
const REAPER_ANALYSIS_RATE: u128 = 22_050;
const REAPER_SPECTRAL_HALF_WINDOW: usize = 512;

#[inline]
fn low_rate_fine_count(frames: usize, division: u32) -> usize {
    if division == 0 || frames <= REAPER_SPECTRAL_HALF_WINDOW {
        return 0;
    }
    let d = division as usize;
    // Fresh-process REAPER 7.79 probes at 8, 11.025, 16 and 22.05 kHz match
    // round-half-up((frames - 512) / division) exactly.
    (frames - REAPER_SPECTRAL_HALF_WINDOW + d / 2) / d
}

#[inline]
fn high_rate_fine_count(frames: usize, source_rate: u32, division: u32) -> usize {
    if division == 0 || source_rate == 0 {
        return 0;
    }
    let source_span = frames as u128 * REAPER_ANALYSIS_RATE;
    let margin = REAPER_SPECTRAL_HALF_WINDOW as u128 * source_rate as u128;
    if source_span <= margin {
        return 0;
    }
    let denominator = division as u128 * REAPER_ANALYSIS_RATE;
    ((source_span - margin + denominator / 2) / denominator) as usize
}

#[inline]
fn reaper_fine_count(frames: usize, source_rate: u32, division: u32) -> usize {
    if source_rate <= REAPER_ZERO_SPECTRAL_MAX_RATE {
        low_rate_fine_count(frames, division)
    } else {
        high_rate_fine_count(frames, source_rate, division)
    }
}

fn validate_source_len<T>(
    pcm: &[T],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<()> {
    if channels == 0 {
        return Err(ReaPeaksError::InvalidArgument("channels=0"));
    }
    if division == 0 || source_rate == 0 {
        return Err(ReaPeaksError::InvalidArgument("zero rate/division"));
    }
    if pcm.len() < frames.saturating_mul(channels) {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }
    Ok(())
}

fn zero_fine(frames: usize, channels: usize, division: u32) -> Vec<SpectralPeak> {
    vec![SpectralPeak::default(); low_rate_fine_count(frames, division) * channels]
}

fn build_high_rate_i16(
    pcm: &[i16],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<Vec<SpectralPeak>> {
    validate_source_len(pcm, frames, channels, source_rate, division)?;
    let target = reaper_fine_count(frames, source_rate, division);
    crate::spectral_base::build_fine_spectral_with_expected(
        pcm,
        frames,
        channels,
        source_rate,
        division,
        target,
    )
}

fn build_high_rate_f32(
    pcm: &[f32],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<Vec<SpectralPeak>> {
    validate_source_len(pcm, frames, channels, source_rate, division)?;
    let target = reaper_fine_count(frames, source_rate, division);
    crate::spectral_base::build_fine_spectral_f32_with_expected(
        pcm,
        frames,
        channels,
        source_rate,
        division,
        target,
    )
}

fn zero_layers(
    frames: usize,
    channels: usize,
    divisions: &[u32],
) -> Result<Vec<GeneratedLayer>> {
    if divisions.is_empty() {
        return Ok(Vec::new());
    }
    let fine_div = divisions[0];
    if fine_div == 0 {
        return Err(ReaPeaksError::InvalidArgument("zero rate/division"));
    }
    let fine_count = low_rate_fine_count(frames, fine_div);
    let mut out = Vec::with_capacity(divisions.len());

    for (li, &div) in divisions.iter().enumerate() {
        if div == 0 || div % fine_div != 0 {
            return Err(ReaPeaksError::Unsupported(
                "spectral divisions must be nonzero multiples of fine division",
            ));
        }
        let ratio = (div / fine_div) as usize;
        // REAPER's coarser low-rate spectral levels are derived from the fine
        // level; their counts are floor(fine_count / ratio).
        let count = if li == 0 {
            fine_count
        } else {
            fine_count / ratio
        };
        out.push(GeneratedLayer {
            header: LayerHeader {
                division: TOKEN_SPECTRAL,
                peak_count: count as u32,
            },
            bytes: vec![0u8; count.saturating_mul(channels).saturating_mul(4)],
        });
    }
    Ok(out)
}

pub fn build_fine_spectral(
    pcm: &[i16],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<Vec<SpectralPeak>> {
    if source_rate > REAPER_ZERO_SPECTRAL_MAX_RATE {
        return build_high_rate_i16(pcm, frames, channels, source_rate, division);
    }
    validate_source_len(pcm, frames, channels, source_rate, division)?;
    Ok(zero_fine(frames, channels, division))
}

pub fn build_fine_spectral_f32(
    pcm: &[f32],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<Vec<SpectralPeak>> {
    if source_rate > REAPER_ZERO_SPECTRAL_MAX_RATE {
        return build_high_rate_f32(pcm, frames, channels, source_rate, division);
    }
    validate_source_len(pcm, frames, channels, source_rate, division)?;
    Ok(zero_fine(frames, channels, division))
}

pub fn aggregate_spectral_from_fine(
    fine: &[SpectralPeak],
    channels: usize,
    ratio: usize,
    output_count: usize,
) -> Vec<SpectralPeak> {
    crate::spectral_base::aggregate_spectral_from_fine(fine, channels, ratio, output_count)
}

pub fn build_spectral_layers(
    pcm: &[i16],
    frames: usize,
    channels: usize,
    source_rate: u32,
    divisions: &[u32],
) -> Result<Vec<GeneratedLayer>> {
    if divisions.is_empty() {
        return Ok(Vec::new());
    }
    if source_rate > REAPER_ZERO_SPECTRAL_MAX_RATE {
        // Fine-level exactness is gated independently. The high-rate coarse
        // assembler is kept on the historical path until its own full-mipmap
        // fresh-process oracle is added.
        return crate::spectral_base::build_spectral_layers(
            pcm,
            frames,
            channels,
            source_rate,
            divisions,
        );
    }
    validate_source_len(pcm, frames, channels, source_rate, divisions[0])?;
    zero_layers(frames, channels, divisions)
}

pub fn build_spectral_layers_f32(
    pcm: &[f32],
    frames: usize,
    channels: usize,
    source_rate: u32,
    divisions: &[u32],
) -> Result<Vec<GeneratedLayer>> {
    if divisions.is_empty() {
        return Ok(Vec::new());
    }
    if source_rate > REAPER_ZERO_SPECTRAL_MAX_RATE {
        return crate::spectral_base::build_spectral_layers_f32(
            pcm,
            frames,
            channels,
            source_rate,
            divisions,
        );
    }
    validate_source_len(pcm, frames, channels, source_rate, divisions[0])?;
    zero_layers(frames, channels, divisions)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn low_rate_count_matches_fresh_reaper779_probes() {
        let cases = [
            (600usize, 73u32, 1usize),
            (700, 73, 3),
            (4096, 73, 49),
            (4096, 53, 68),
            (4096, 36, 100),
            (4096, 26, 138),
            (5000, 73, 61),
        ];
        for (frames, division, expected) in cases {
            assert_eq!(low_rate_fine_count(frames, division), expected);
        }
    }

    #[test]
    fn high_rate_count_matches_expanded_reaper779_probes() {
        let cases = [
            (5000usize, 22_051u32, 73u32, 61usize),
            (5000, 24_000, 80, 56),
            (5000, 32_000, 106, 40),
            (5000, 40_000, 133, 31),
            (5000, 44_100, 147, 27),
            (5000, 48_000, 160, 24),
            (5000, 96_000, 320, 9),
            (5000, 192_000, 640, 1),
        ];
        for (frames, rate, division, expected) in cases {
            assert_eq!(high_rate_fine_count(frames, rate, division), expected);
            assert_eq!(reaper_fine_count(frames, rate, division), expected);
        }
    }

    #[test]
    fn low_rate_payload_is_zero_and_coarse_counts_derive_from_fine() {
        let pcm = vec![1234i16; 4096];
        let fine = build_fine_spectral(&pcm, 4096, 1, 22_050, 73).unwrap();
        assert_eq!(fine.len(), 49);
        assert!(fine.iter().all(|p| p.code() == 0));

        let layers = build_spectral_layers(&pcm, 4096, 1, 22_050, &[73, 1168, 22192])
            .unwrap();
        assert_eq!(layers[0].header.peak_count, 49);
        assert_eq!(layers[1].header.peak_count, 3);
        assert_eq!(layers[2].header.peak_count, 0);
        assert!(layers.iter().all(|l| l.bytes.iter().all(|&b| b == 0)));
    }
}
