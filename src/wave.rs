use crate::error::{ReaPeaksError, Result};
use crate::format::{GeneratedLayer, LayerHeader, Version};

#[repr(C)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct PeakPair {
    pub max: i16,
    pub min: i16,
}

#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WaveEncoding {
    Rpkn = 1,
    Rpkl = 2,
}

impl WaveEncoding {
    pub fn from_version(version: Version) -> Self {
        match version {
            Version::Rpkl => Self::Rpkl,
            Version::Rpkm | Version::Rpkn => Self::Rpkn,
        }
    }
}

/// Empirically exact RPKN PCM16 peak quantization for REAPER 7.79 Linux.
///
/// Validated against an exhaustive source containing every PCM16 value
/// (-32768..32767) as its own 147-frame bucket, plus 122,516 waveform buckets
/// from the broader REAPER oracle corpus.  All compared buckets are byte-exact.
///
/// REAPER's effective normalized mapping is asymmetric at full scale:
///   x >= 0: round_half_up(x * 32767)
///   x <  0: -round_half_up(-x * 32768)
/// PCM16 decoder values are x=v/32768, which gives this integer shortcut.
#[inline]
pub fn quantize_pcm16_peak(v: i16) -> i16 {
    if v < 0 {
        v
    } else {
        let a = v as i32;
        ((a * 32767 + 16384) / 32768) as i16
    }
}

/// RPKN normalized-float quantizer.  This is the rule measured with a
/// deterministic 24-bit WAV containing 50,000 independently selected source
/// values; all 50,000 constant buckets matched REAPER 7.79 byte-for-byte.
#[inline]
pub fn quantize_rpkn_f32(v: f32) -> i16 {
    if v.is_nan() {
        return 0;
    }
    let x = v.clamp(-1.0, 1.0) as f64;
    if x >= 0.0 {
        (x.mul_add(32767.0, 0.5).floor() as i32).clamp(0, 32767) as i16
    } else {
        -((-x).mul_add(32768.0, 0.5).floor() as i32).clamp(0, 32768) as i16
    }
}

/// Encode one floating-point amplitude into an RPKL v1.2 peak code.
///
/// REAPER 7.79 was probed with 43,857 finite float values and additional
/// high-range values through +/-512.  The measured encoder is the official
/// transform with round-half-up quantization:
///   |x| <= 1: code_mag = round_half_up(|x| * 24576)
///   |x| >  1: code_mag = round_half_up(24576 + 1024*log2(|x|))
/// Positive values saturate at +32767 and negative values at -32768.
/// Consequently the representable negative endpoint is exactly -256 while the
/// largest positive code is slightly below +256.
#[inline]
pub fn quantize_rpkl_f32(v: f32) -> i16 {
    if v.is_nan() {
        return 0;
    }
    if v == 0.0 {
        return 0;
    }
    let neg = v.is_sign_negative();
    let a = (v as f64).abs();
    let code = if a <= 1.0 {
        (a * 24576.0 + 0.5).floor() as i32
    } else if a.is_infinite() {
        if neg {
            32768
        } else {
            32767
        }
    } else {
        (24576.0 + 1024.0 * a.log2() + 0.5).floor() as i32
    };
    if neg {
        let m = code.clamp(0, 32768);
        if m == 32768 {
            i16::MIN
        } else {
            -(m as i16)
        }
    } else {
        code.clamp(0, 32767) as i16
    }
}

#[inline]
pub fn decode_rpkn_code(code: i16) -> f32 {
    if code < 0 {
        code as f32 / 32768.0
    } else {
        code as f32 / 32767.0
    }
}

#[inline]
pub fn decode_rpkl_code(code: i16) -> f32 {
    let c = code as i32;
    if c >= 0 {
        if c <= 24576 {
            c as f32 / 24576.0
        } else {
            (2.0f64).powf((c - 24576) as f64 / 1024.0) as f32
        }
    } else {
        let m = -c;
        if m <= 24576 {
            -(m as f32 / 24576.0)
        } else {
            -(2.0f64).powf((m - 24576) as f64 / 1024.0) as f32
        }
    }
}

#[inline]
pub fn decode_peak_code(encoding: WaveEncoding, code: i16) -> f32 {
    match encoding {
        WaveEncoding::Rpkn => decode_rpkn_code(code),
        WaveEncoding::Rpkl => decode_rpkl_code(code),
    }
}

pub fn default_divisions(sample_rate: u32, fine_peaks_per_second: u32) -> [u32; 3] {
    fn ceil_div(numerator: u64, denominator: u64) -> u64 {
        numerator / denominator + u64::from(numerator % denominator != 0)
    }

    fn to_u32_saturating(value: u64) -> u32 {
        value.min(u64::from(u32::MAX)) as u32
    }

    let sample_rate = u64::from(sample_rate.max(1));
    let peak_rate = u64::from(fine_peaks_per_second.max(1));
    let fine = (sample_rate / peak_rate).max(1);

    // REAPER first chooses the finest integer frame division, then builds
    // nested integer multiples that are no denser than 20 Hz and 1 Hz. Using
    // ceiling factors is observable whenever the sample rate is not exactly
    // divisible by the configured peak rate.
    let mid_denominator = fine.saturating_mul(20).max(1);
    let mid = fine.saturating_mul(ceil_div(sample_rate, mid_denominator).max(1));
    let coarse = mid.saturating_mul(ceil_div(sample_rate, mid).max(1));

    [
        to_u32_saturating(fine),
        to_u32_saturating(mid),
        to_u32_saturating(coarse),
    ]
}

pub fn build_wave_layers(
    pcm: &[i16],
    frames: usize,
    channels: usize,
    divisions: &[u32],
) -> Result<Vec<GeneratedLayer>> {
    if channels == 0 {
        return Err(ReaPeaksError::InvalidArgument("channels=0"));
    }
    let required = frames
        .checked_mul(channels)
        .ok_or(ReaPeaksError::InvalidArgument("frames*channels overflow"))?;
    if pcm.len() < required {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }
    let mut layers = Vec::with_capacity(divisions.len());
    for &div in divisions {
        if div == 0 {
            return Err(ReaPeaksError::InvalidArgument("division=0"));
        }
        if div > i32::MAX as u32 {
            return Err(ReaPeaksError::InvalidArgument(
                "division exceeds signed .ReaPeaks range",
            ));
        }
        let d = div as usize;
        let count = frames.div_ceil(d);
        let peak_count = u32::try_from(count)
            .map_err(|_| ReaPeaksError::InvalidArgument("wave peak count exceeds u32"))?;
        let capacity = count
            .checked_mul(channels)
            .and_then(|x| x.checked_mul(4))
            .filter(|&x| x <= isize::MAX as usize)
            .ok_or(ReaPeaksError::InvalidArgument("wave payload too large"))?;
        let mut bytes = Vec::with_capacity(capacity);
        for peak in 0..count {
            let s0 = peak * d;
            let s1 = s0.saturating_add(d).min(frames);
            for c in 0..channels {
                let mut mx = i16::MIN;
                let mut mn = i16::MAX;
                for f in s0..s1 {
                    let v = quantize_pcm16_peak(pcm[f * channels + c]);
                    mx = mx.max(v);
                    mn = mn.min(v);
                }
                if s0 == s1 {
                    mx = 0;
                    mn = 0;
                }
                bytes.extend_from_slice(&mx.to_le_bytes());
                bytes.extend_from_slice(&mn.to_le_bytes());
            }
        }
        layers.push(GeneratedLayer {
            header: LayerHeader {
                division: div as i32,
                peak_count,
            },
            bytes,
        });
    }
    Ok(layers)
}

/// Build RPKN or RPKL waveform layers from decoded float32 audio.
///
/// RPKL has a subtle REAPER behavior: bucket extrema begin at max=-1.0 and
/// min=+1.0. Therefore a bucket whose every sample exceeds +1 retains a min of
/// +1, and a bucket wholly below -1 retains a max of -1.  The 43,857-value
/// RPKL map matched this rule exactly.
pub fn build_wave_layers_f32(
    pcm: &[f32],
    frames: usize,
    channels: usize,
    divisions: &[u32],
    encoding: WaveEncoding,
) -> Result<Vec<GeneratedLayer>> {
    if channels == 0 {
        return Err(ReaPeaksError::InvalidArgument("channels=0"));
    }
    let required = frames
        .checked_mul(channels)
        .ok_or(ReaPeaksError::InvalidArgument("frames*channels overflow"))?;
    if pcm.len() < required {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }
    let mut layers = Vec::with_capacity(divisions.len());
    for &div in divisions {
        if div == 0 {
            return Err(ReaPeaksError::InvalidArgument("division=0"));
        }
        if div > i32::MAX as u32 {
            return Err(ReaPeaksError::InvalidArgument(
                "division exceeds signed .ReaPeaks range",
            ));
        }
        let d = div as usize;
        let count = frames.div_ceil(d);
        let peak_count = u32::try_from(count)
            .map_err(|_| ReaPeaksError::InvalidArgument("wave peak count exceeds u32"))?;
        let capacity = count
            .checked_mul(channels)
            .and_then(|x| x.checked_mul(4))
            .filter(|&x| x <= isize::MAX as usize)
            .ok_or(ReaPeaksError::InvalidArgument("wave payload too large"))?;
        let mut bytes = Vec::with_capacity(capacity);
        for peak in 0..count {
            let s0 = peak * d;
            let s1 = s0.saturating_add(d).min(frames);
            for c in 0..channels {
                let (mut mx, mut mn) = match encoding {
                    WaveEncoding::Rpkn | WaveEncoding::Rpkl => (-1.0f32, 1.0f32),
                };
                for f in s0..s1 {
                    let v = pcm[f * channels + c];
                    if v.is_nan() {
                        continue;
                    }
                    if v > mx {
                        mx = v;
                    }
                    if v < mn {
                        mn = v;
                    }
                }
                if s0 == s1 {
                    mx = 0.0;
                    mn = 0.0;
                }
                let enc = |v: f32| match encoding {
                    WaveEncoding::Rpkn => quantize_rpkn_f32(v),
                    WaveEncoding::Rpkl => quantize_rpkl_f32(v),
                };
                bytes.extend_from_slice(&enc(mx).to_le_bytes());
                bytes.extend_from_slice(&enc(mn).to_le_bytes());
            }
        }
        layers.push(GeneratedLayer {
            header: LayerHeader {
                division: div as i32,
                peak_count,
            },
            bytes,
        });
    }
    Ok(layers)
}

pub fn aggregate_peaks(input: &[PeakPair], channels: usize, factor: usize) -> Vec<PeakPair> {
    if channels == 0 || factor == 0 {
        return Vec::new();
    }
    let n = input.len() / channels;
    let out_n = n.div_ceil(factor);
    let mut out = Vec::with_capacity(out_n * channels);
    for p in 0..out_n {
        let a = p * factor;
        let b = a.saturating_add(factor).min(n);
        for c in 0..channels {
            let mut mx = i16::MIN;
            let mut mn = i16::MAX;
            for i in a..b {
                let q = input[i * channels + c];
                mx = mx.max(q.max);
                mn = mn.min(q.min);
            }
            out.push(PeakPair { max: mx, min: mn });
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pcm16_quantization_boundaries_match_reaper_map() {
        assert_eq!(quantize_pcm16_peak(-32768), -32768);
        assert_eq!(quantize_pcm16_peak(-1), -1);
        assert_eq!(quantize_pcm16_peak(0), 0);
        assert_eq!(quantize_pcm16_peak(16384), 16384);
        assert_eq!(quantize_pcm16_peak(16385), 16384);
        assert_eq!(quantize_pcm16_peak(32767), 32766);
    }

    #[test]
    fn rpkn_float_asymmetric_full_scale() {
        assert_eq!(quantize_rpkn_f32(-1.0), -32768);
        assert_eq!(quantize_rpkn_f32(1.0), 32767);
        assert_eq!(quantize_rpkn_f32(0.5), 16384);
        assert_eq!(quantize_rpkn_f32(-0.5), -16384);
    }

    #[test]
    fn rpkl_known_codes() {
        assert_eq!(quantize_rpkl_f32(1.0), 24576);
        assert_eq!(quantize_rpkl_f32(2.0), 25600);
        assert_eq!(quantize_rpkl_f32(8.0), 27648);
        assert_eq!(quantize_rpkl_f32(128.0), 31744);
        assert_eq!(quantize_rpkl_f32(256.0), 32767);
        assert_eq!(quantize_rpkl_f32(-256.0), -32768);
        assert_eq!(decode_rpkl_code(25600), 2.0);
        assert_eq!(decode_rpkl_code(-32768), -256.0);
    }

    #[test]
    fn default_divisions_match_reaper779_preference_probe() {
        let cases = [
            (22_051, 100, [220, 1_320, 22_440]),
            (44_100, 100, [441, 2_205, 44_100]),
            (48_000, 100, [480, 2_400, 48_000]),
            (22_051, 150, [147, 1_176, 22_344]),
            (44_100, 150, [294, 2_352, 44_688]),
            (48_000, 150, [320, 2_560, 48_640]),
            (22_051, 200, [110, 1_210, 22_990]),
            (44_100, 200, [220, 2_420, 45_980]),
            (48_000, 200, [240, 2_400, 48_000]),
            (22_051, 300, [73, 1_168, 22_192]),
            (44_100, 300, [147, 2_205, 44_100]),
            (48_000, 300, [160, 2_400, 48_000]),
            (22_051, 500, [44, 1_144, 22_880]),
            (44_100, 500, [88, 2_288, 45_760]),
            (48_000, 500, [96, 2_400, 48_000]),
            (22_051, 1_000, [22, 1_122, 22_440]),
            (44_100, 1_000, [44, 2_244, 44_880]),
            (48_000, 1_000, [48, 2_400, 48_000]),
        ];

        for (sample_rate, peak_rate, expected) in cases {
            assert_eq!(
                default_divisions(sample_rate, peak_rate),
                expected,
                "sample_rate={sample_rate} peak_rate={peak_rate}",
            );
        }
    }
}
