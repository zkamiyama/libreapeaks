//! REAPER 7.79 strict compatibility shim for spectral generation.
//!
//! REAPER has a non-DSP quirk for source rates <= 22.05 kHz: it still emits
//! spectral mipmap structures, but every spectral code is zero. Keep that
//! behavior out of the normal implementation and intercept it only when the
//! `strict-wdl` feature selects this module.
//!
//! High-rate strict mode derives its fine-record count from the actual WDL
//! 22.05 kHz analysis stream and then applies the recovered spectral scheduler.
//! This avoids fitting EOF behavior to a source-length half-window formula.

use crate::error::{ReaPeaksError, Result};
use crate::format::{GeneratedLayer, LayerHeader, SpectralPeak, TOKEN_SPECTRAL};

const REAPER_ZERO_SPECTRAL_MAX_RATE: u32 = 22_050;
const REAPER_ANALYSIS_RATE: f64 = 22_050.0;
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

#[cfg(test)]
#[inline]
fn legacy_high_rate_fine_count(frames: usize, source_rate: u32, division: u32) -> usize {
    if division == 0 || source_rate == 0 {
        return 0;
    }
    let source_span = frames as u128 * 22_050u128;
    let margin = REAPER_SPECTRAL_HALF_WINDOW as u128 * source_rate as u128;
    if source_span <= margin {
        return 0;
    }
    let denominator = division as u128 * 22_050u128;
    ((source_span - margin + denominator / 2) / denominator) as usize
}

fn wdl_analysis_frames(frames: usize, channels: usize, source_rate: u32) -> Result<usize> {
    unsafe extern "C" {
        fn rpk_wdl_resample_count(
            input_frames: i64,
            channels: i32,
            input_rate: f64,
            output_rate: f64,
        ) -> i64;
    }

    let input_frames = i64::try_from(frames)
        .map_err(|_| ReaPeaksError::InvalidArgument("frame count exceeds strict WDL range"))?;
    let channels_i32 = i32::try_from(channels)
        .map_err(|_| ReaPeaksError::InvalidArgument("channel count exceeds strict WDL range"))?;
    let got = unsafe {
        rpk_wdl_resample_count(
            input_frames,
            channels_i32,
            source_rate as f64,
            REAPER_ANALYSIS_RATE,
        )
    };
    if got < 0 {
        return Err(ReaPeaksError::Unsupported(
            "strict WDL resampler frame-count probe failed",
        ));
    }
    usize::try_from(got)
        .map_err(|_| ReaPeaksError::InvalidArgument("strict WDL output count overflow"))
}

#[inline]
fn fine_count_from_analysis_frames(
    analysis_frames: usize,
    source_rate: u32,
    division: u32,
) -> usize {
    if analysis_frames == 0 || division == 0 || source_rate == 0 {
        return 0;
    }
    let hop = division as f64 * REAPER_ANALYSIS_RATE / source_rate as f64;
    let rounded = (hop + 0.5).floor() as i32;
    let mut phase = if rounded <= 1023 {
        (rounded - 1024) as f64 * 0.5
    } else {
        0.0
    };
    let mut count = 0usize;
    for _ in 0..analysis_frames {
        phase += 1.0;
        while phase >= hop {
            count = count.saturating_add(1);
            phase -= hop;
        }
    }
    count
}

fn high_rate_wdl_fine_count(
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<usize> {
    let analysis_frames = wdl_analysis_frames(frames, channels, source_rate)?;
    Ok(fine_count_from_analysis_frames(
        analysis_frames,
        source_rate,
        division,
    ))
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
    let target = high_rate_wdl_fine_count(frames, channels, source_rate, division)?;
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
    let target = high_rate_wdl_fine_count(frames, channels, source_rate, division)?;
    crate::spectral_base::build_fine_spectral_f32_with_expected(
        pcm,
        frames,
        channels,
        source_rate,
        division,
        target,
    )
}

fn validate_divisions(divisions: &[u32]) -> Result<u32> {
    if divisions.is_empty() {
        return Err(ReaPeaksError::InvalidArgument("no spectral divisions"));
    }
    let fine_div = divisions[0];
    if fine_div == 0 {
        return Err(ReaPeaksError::InvalidArgument("zero rate/division"));
    }
    for &div in divisions {
        if div == 0 || div % fine_div != 0 {
            return Err(ReaPeaksError::Unsupported(
                "spectral divisions must be nonzero multiples of fine division",
            ));
        }
    }
    Ok(fine_div)
}

fn encode_layer(peaks: &[SpectralPeak], channels: usize) -> GeneratedLayer {
    let mut bytes = Vec::with_capacity(peaks.len() * 4);
    for p in peaks {
        bytes.extend_from_slice(&p.code().to_le_bytes());
    }
    GeneratedLayer {
        header: LayerHeader {
            division: TOKEN_SPECTRAL,
            peak_count: if channels == 0 {
                0
            } else {
                (peaks.len() / channels) as u32
            },
        },
        bytes,
    }
}

fn assemble_high_rate_layers(
    fine: &[SpectralPeak],
    channels: usize,
    divisions: &[u32],
) -> Result<Vec<GeneratedLayer>> {
    let fine_div = validate_divisions(divisions)?;
    let fine_count = fine.len() / channels;
    let mut out = Vec::with_capacity(divisions.len());

    for (li, &div) in divisions.iter().enumerate() {
        let ratio = (div / fine_div) as usize;
        let peaks = if li == 0 {
            fine.to_vec()
        } else {
            // REAPER's mipmaps are assembled directly from the fine spectral
            // stream. Counts are therefore floor(fine_count / ratio), not a
            // second source-domain scheduler calculation.
            let count = fine_count / ratio;
            crate::spectral_base::aggregate_spectral_from_fine(fine, channels, ratio, count)
        };
        out.push(encode_layer(&peaks, channels));
    }
    Ok(out)
}

fn zero_layers(frames: usize, channels: usize, divisions: &[u32]) -> Result<Vec<GeneratedLayer>> {
    let fine_div = validate_divisions(divisions)?;
    let fine_count = low_rate_fine_count(frames, fine_div);
    let mut out = Vec::with_capacity(divisions.len());

    for (li, &div) in divisions.iter().enumerate() {
        let ratio = (div / fine_div) as usize;
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
    validate_source_len(pcm, frames, channels, source_rate, divisions[0])?;
    if source_rate > REAPER_ZERO_SPECTRAL_MAX_RATE {
        let fine = build_high_rate_i16(pcm, frames, channels, source_rate, divisions[0])?;
        return assemble_high_rate_layers(&fine, channels, divisions);
    }
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
    validate_source_len(pcm, frames, channels, source_rate, divisions[0])?;
    if source_rate > REAPER_ZERO_SPECTRAL_MAX_RATE {
        let fine = build_high_rate_f32(pcm, frames, channels, source_rate, divisions[0])?;
        return assemble_high_rate_layers(&fine, channels, divisions);
    }
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
    fn legacy_formula_documents_pre_hardening_reference_points() {
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
            assert_eq!(
                legacy_high_rate_fine_count(frames, rate, division),
                expected
            );
        }
    }

    #[test]
    fn analysis_scheduler_matches_known_edge_counts() {
        let cases = [
            (22_110usize, 48_000u32, 48u32, 979usize),
            (22_111, 48_000, 48, 980),
            (14_513, 76_800, 256, 190),
            (14_514, 76_800, 256, 191),
            (22_065, 192_000, 192, 977),
            (22_066, 192_000, 192, 978),
        ];
        for (analysis_frames, rate, division, expected) in cases {
            assert_eq!(
                fine_count_from_analysis_frames(analysis_frames, rate, division),
                expected
            );
        }
    }

    #[test]
    fn high_rate_full_layers_preserve_exact_fine_scheduler() {
        let pcm = vec![1234i16; 5000];
        let fine = build_fine_spectral(&pcm, 5000, 1, 22_051, 73).unwrap();
        assert_eq!(fine.len(), 61);

        let layers = build_spectral_layers(&pcm, 5000, 1, 22_051, &[73, 1095, 21900]).unwrap();
        assert_eq!(layers[0].header.peak_count, 61);
        assert_eq!(layers[1].header.peak_count, 4);
        assert_eq!(layers[2].header.peak_count, 0);

        let mut fine_bytes = Vec::with_capacity(fine.len() * 4);
        for p in &fine {
            fine_bytes.extend_from_slice(&p.code().to_le_bytes());
        }
        assert_eq!(layers[0].bytes, fine_bytes);
    }

    #[test]
    fn low_rate_payload_is_zero_and_coarse_counts_derive_from_fine() {
        let pcm = vec![1234i16; 4096];
        let fine = build_fine_spectral(&pcm, 4096, 1, 22_050, 73).unwrap();
        assert_eq!(fine.len(), 49);

        let layers = build_spectral_layers(&pcm, 4096, 1, 22_050, &[73, 1168, 22192]).unwrap();
        assert_eq!(layers[0].header.peak_count, 49);
        assert_eq!(layers[1].header.peak_count, 3);
        assert_eq!(layers[2].header.peak_count, 0);
        assert!(layers.iter().all(|l| l.bytes.iter().all(|&b| b == 0)));
    }
}
