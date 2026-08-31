use crate::error::{ReaPeaksError, Result};
use crate::format::{GeneratedLayer, LayerHeader, LoudnessPeak, TOKEN_LOUDNESS};
use std::f64::consts::PI;

const MOMENTARY_BLOCKS_100MS: usize = 4;
const SHORT_TERM_BLOCKS_100MS: usize = 30;

#[derive(Debug, Clone, Copy)]
struct BiquadCoefficients {
    b0: f64,
    b1: f64,
    b2: f64,
    a1: f64,
    a2: f64,
}

#[derive(Debug, Clone, Copy)]
struct Biquad {
    coefficients: BiquadCoefficients,
    x1: f64,
    x2: f64,
    y1: f64,
    y2: f64,
}

impl Biquad {
    fn new(coefficients: BiquadCoefficients) -> Self {
        Self {
            coefficients,
            x1: 0.0,
            x2: 0.0,
            y1: 0.0,
            y2: 0.0,
        }
    }

    #[inline]
    fn process(&mut self, input: f64) -> f64 {
        let c = self.coefficients;
        let output =
            c.b0 * input + c.b1 * self.x1 + c.b2 * self.x2 - c.a1 * self.y1 - c.a2 * self.y2;
        self.x2 = self.x1;
        self.x1 = input;
        self.y2 = self.y1;
        self.y1 = output;
        output
    }
}

#[derive(Debug, Clone, Copy)]
struct KWeightingFilter {
    pre_filter: Biquad,
    rlb_filter: Biquad,
}

impl KWeightingFilter {
    fn new(sample_rate: u32) -> Result<Self> {
        let (pre_filter, rlb_filter) = k_weighting_coefficients(sample_rate)?;
        Ok(Self {
            pre_filter: Biquad::new(pre_filter),
            rlb_filter: Biquad::new(rlb_filter),
        })
    }

    #[inline]
    fn process(&mut self, input: f64) -> f64 {
        let pre_filtered = self.pre_filter.process(input);
        self.rlb_filter.process(pre_filtered)
    }
}

#[derive(Debug, Clone)]
struct SlidingEnergy {
    values: Vec<f64>,
    next: usize,
    filled: usize,
    sum: f64,
}

impl SlidingEnergy {
    fn new(window_frames: usize) -> Result<Self> {
        if window_frames == 0 {
            return Err(ReaPeaksError::InvalidArgument(
                "loudness window has zero frames",
            ));
        }
        let bytes = window_frames
            .checked_mul(std::mem::size_of::<f64>())
            .filter(|&size| size <= isize::MAX as usize)
            .ok_or(ReaPeaksError::InvalidArgument(
                "loudness window is too large",
            ))?;
        let mut values = Vec::new();
        values
            .try_reserve_exact(bytes / std::mem::size_of::<f64>())
            .map_err(|_| ReaPeaksError::InvalidArgument("loudness window allocation failed"))?;
        values.resize(window_frames, 0.0);
        Ok(Self {
            values,
            next: 0,
            filled: 0,
            sum: 0.0,
        })
    }

    #[inline]
    fn push(&mut self, energy: f64) {
        if self.filled == self.values.len() {
            self.sum -= self.values[self.next];
        } else {
            self.filled += 1;
        }
        self.values[self.next] = energy;
        self.next += 1;
        if self.next == self.values.len() {
            self.next = 0;
        }
        self.sum += energy;
    }

    #[inline]
    fn normalized(&self) -> f64 {
        self.sum / self.values.len() as f64
    }
}

fn k_weighting_coefficients(sample_rate: u32) -> Result<(BiquadCoefficients, BiquadCoefficients)> {
    if sample_rate < 16 {
        return Err(ReaPeaksError::InvalidArgument(
            "sample rate is too low for loudness filtering",
        ));
    }

    // Preserve the exact BS.1770 coefficients used by the REAPER 7.79
    // 48 kHz oracle. The formula below produces the same coefficients within
    // floating-point rounding, but spelling these constants explicitly keeps
    // the byte-exact path independent of platform libm details.
    if sample_rate == 48_000 {
        return Ok((
            BiquadCoefficients {
                b0: 1.535_124_859_586_97,
                b1: -2.691_696_189_406_38,
                b2: 1.198_392_810_852_85,
                a1: -1.690_659_293_182_41,
                a2: 0.732_480_774_215_85,
            },
            BiquadCoefficients {
                b0: 1.0,
                b1: -2.0,
                b2: 1.0,
                a1: -1.990_047_454_833_98,
                a2: 0.990_072_250_366_21,
            },
        ));
    }

    let rate = f64::from(sample_rate);

    let shelf_frequency = 1_681.974_450_955_533;
    let shelf_gain_db = 3.999_843_853_973_347;
    let shelf_q = 0.707_175_236_955_419_6;
    let shelf_k = (PI * shelf_frequency / rate).tan();
    let shelf_vh = 10.0_f64.powf(shelf_gain_db / 20.0);
    let shelf_vb = shelf_vh.powf(0.499_666_774_154_541_6);
    let shelf_a0 = 1.0 + shelf_k / shelf_q + shelf_k * shelf_k;
    let pre_filter = BiquadCoefficients {
        b0: (shelf_vh + shelf_vb * shelf_k / shelf_q + shelf_k * shelf_k) / shelf_a0,
        b1: 2.0 * (shelf_k * shelf_k - shelf_vh) / shelf_a0,
        b2: (shelf_vh - shelf_vb * shelf_k / shelf_q + shelf_k * shelf_k) / shelf_a0,
        a1: 2.0 * (shelf_k * shelf_k - 1.0) / shelf_a0,
        a2: (1.0 - shelf_k / shelf_q + shelf_k * shelf_k) / shelf_a0,
    };

    let high_pass_frequency = 38.135_470_876_024_44;
    let high_pass_q = 0.500_327_037_323_877_3;
    let high_pass_k = (PI * high_pass_frequency / rate).tan();
    let high_pass_a0 = 1.0 + high_pass_k / high_pass_q + high_pass_k * high_pass_k;
    let rlb_filter = BiquadCoefficients {
        b0: 1.0,
        b1: -2.0,
        b2: 1.0,
        a1: 2.0 * (high_pass_k * high_pass_k - 1.0) / high_pass_a0,
        a2: (1.0 - high_pass_k / high_pass_q + high_pass_k * high_pass_k) / high_pass_a0,
    };

    Ok((pre_filter, rlb_filter))
}

fn loudness_windows(sample_rate: u32) -> Result<(usize, usize)> {
    let samples_in_100ms = (u64::from(sample_rate) + 5) / 10;
    let momentary = samples_in_100ms
        .checked_mul(MOMENTARY_BLOCKS_100MS as u64)
        .and_then(|value| usize::try_from(value).ok())
        .ok_or(ReaPeaksError::InvalidArgument(
            "momentary loudness window is too large",
        ))?;
    let short_term = samples_in_100ms
        .checked_mul(SHORT_TERM_BLOCKS_100MS as u64)
        .and_then(|value| usize::try_from(value).ok())
        .ok_or(ReaPeaksError::InvalidArgument(
            "short-term loudness window is too large",
        ))?;
    if momentary == 0 || short_term == 0 {
        return Err(ReaPeaksError::InvalidArgument(
            "loudness window has zero frames",
        ));
    }
    Ok((momentary, short_term))
}

fn encode_loudness_layer(records: &[LoudnessPeak], channels: usize) -> Result<GeneratedLayer> {
    if channels == 0 || records.len() % channels != 0 {
        return Err(ReaPeaksError::InvalidArgument(
            "invalid loudness channel layout",
        ));
    }
    let record_count = records.len() / channels;
    let peak_count = record_count
        .checked_mul(2)
        .and_then(|value| u32::try_from(value).ok())
        .ok_or(ReaPeaksError::InvalidArgument(
            "loudness peak count exceeds u32",
        ))?;
    let capacity = records
        .len()
        .checked_mul(8)
        .filter(|&size| size <= isize::MAX as usize)
        .ok_or(ReaPeaksError::InvalidArgument(
            "loudness payload is too large",
        ))?;
    let mut bytes = Vec::with_capacity(capacity);
    for peak in records {
        bytes.extend_from_slice(&peak.momentary_energy.to_le_bytes());
        bytes.extend_from_slice(&peak.short_term_energy.to_le_bytes());
    }
    Ok(GeneratedLayer {
        header: LayerHeader {
            division: TOKEN_LOUDNESS,
            peak_count,
        },
        bytes,
    })
}

fn build_loudness_layers<F>(
    frames: usize,
    channels: usize,
    sample_rate: u32,
    divisions: &[u32],
    sample: F,
) -> Result<Vec<GeneratedLayer>>
where
    F: Fn(usize) -> f64,
{
    if channels == 0 {
        return Err(ReaPeaksError::InvalidArgument("channels=0"));
    }
    if divisions.len() < 2 {
        return Err(ReaPeaksError::InvalidArgument(
            "mode-3 loudness requires at least two waveform divisions",
        ));
    }
    if divisions.iter().any(|&division| division == 0) {
        return Err(ReaPeaksError::InvalidArgument("division=0"));
    }

    let base_division = divisions[1] as usize;
    for pair in divisions[1..].windows(2) {
        if pair[1] <= pair[0] || pair[1] % pair[0] != 0 {
            return Err(ReaPeaksError::InvalidArgument(
                "loudness divisions must be increasing nested multiples",
            ));
        }
    }

    let base_record_count = frames.div_ceil(base_division);
    let base_value_count =
        base_record_count
            .checked_mul(channels)
            .ok_or(ReaPeaksError::InvalidArgument(
                "loudness record count overflow",
            ))?;
    let mut base_records = vec![LoudnessPeak::default(); base_value_count];
    let (momentary_window, short_term_window) = loudness_windows(sample_rate)?;

    for channel in 0..channels {
        let mut filter = KWeightingFilter::new(sample_rate)?;
        let mut momentary = SlidingEnergy::new(momentary_window)?;
        let mut short_term = SlidingEnergy::new(short_term_window)?;
        let mut record_index = 0usize;

        for frame in 0..frames {
            let filtered = filter.process(sample(frame * channels + channel));
            let energy = filtered * filtered;
            momentary.push(energy);
            short_term.push(energy);

            let completed_frame = frame + 1;
            if completed_frame % base_division == 0 || completed_frame == frames {
                base_records[record_index * channels + channel] = LoudnessPeak {
                    momentary_energy: momentary.normalized() as f32,
                    short_term_energy: short_term.normalized() as f32,
                };
                record_index += 1;
            }
        }

        if record_index != base_record_count {
            return Err(ReaPeaksError::InvalidArgument(
                "loudness record count mismatch",
            ));
        }
    }

    let mut layers = Vec::with_capacity(divisions.len() - 1);
    layers.push(encode_loudness_layer(&base_records, channels)?);

    for &division in divisions.iter().skip(2) {
        let group = usize::try_from(division / divisions[1])
            .map_err(|_| ReaPeaksError::InvalidArgument("loudness group is too large"))?;
        if group == 0 || division % divisions[1] != 0 {
            return Err(ReaPeaksError::InvalidArgument(
                "loudness divisions must be nested multiples",
            ));
        }
        let division_usize = usize::try_from(division)
            .map_err(|_| ReaPeaksError::InvalidArgument("division is too large"))?;
        let record_count = frames.div_ceil(division_usize);
        let value_count =
            record_count
                .checked_mul(channels)
                .ok_or(ReaPeaksError::InvalidArgument(
                    "loudness record count overflow",
                ))?;
        let mut records = Vec::with_capacity(value_count);

        for record_index in 0..record_count {
            let start = record_index
                .checked_mul(group)
                .ok_or(ReaPeaksError::InvalidArgument(
                    "loudness group offset overflow",
                ))?;
            let end = start.saturating_add(group).min(base_record_count);
            if start >= end {
                return Err(ReaPeaksError::InvalidArgument(
                    "empty loudness aggregation group",
                ));
            }
            let divisor = (end - start) as f64;
            for channel in 0..channels {
                let mut momentary_sum = 0.0f64;
                let mut short_term_sum = 0.0f64;
                for base_index in start..end {
                    let peak = base_records[base_index * channels + channel];
                    momentary_sum += f64::from(peak.momentary_energy);
                    short_term_sum += f64::from(peak.short_term_energy);
                }
                records.push(LoudnessPeak {
                    momentary_energy: (momentary_sum / divisor) as f32,
                    short_term_energy: (short_term_sum / divisor) as f32,
                });
            }
        }

        layers.push(encode_loudness_layer(&records, channels)?);
    }

    Ok(layers)
}

pub fn build_loudness_layers_pcm16(
    pcm: &[i16],
    frames: usize,
    channels: usize,
    sample_rate: u32,
    divisions: &[u32],
) -> Result<Vec<GeneratedLayer>> {
    let required = frames
        .checked_mul(channels)
        .ok_or(ReaPeaksError::InvalidArgument("frames*channels overflow"))?;
    if pcm.len() < required {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }
    build_loudness_layers(frames, channels, sample_rate, divisions, |index| {
        f64::from(pcm[index]) / 32_768.0
    })
}

pub fn build_loudness_layers_f32(
    pcm: &[f32],
    frames: usize,
    channels: usize,
    sample_rate: u32,
    divisions: &[u32],
) -> Result<Vec<GeneratedLayer>> {
    let required = frames
        .checked_mul(channels)
        .ok_or(ReaPeaksError::InvalidArgument("frames*channels overflow"))?;
    if pcm.len() < required {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }
    build_loudness_layers(frames, channels, sample_rate, divisions, |index| {
        f64::from(pcm[index])
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fnv64(bytes: &[u8]) -> u64 {
        let mut hash = 0xcbf2_9ce4_8422_2325u64;
        for &byte in bytes {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
        hash
    }

    #[test]
    fn reaper779_sine997_loudness_payload_is_byte_exact() {
        let sample_rate = 48_000usize;
        let frames = sample_rate * 3;
        let mut pcm = Vec::with_capacity(frames * 2);
        for frame in 0..frames {
            let phase = 2.0 * PI * 997.0 * frame as f64 / sample_rate as f64;
            pcm.push((0.5 * 32_767.0 * phase.sin()).round() as i16);
            pcm.push((0.25 * 32_767.0 * phase.sin()).round() as i16);
        }

        let layers = build_loudness_layers_pcm16(&pcm, frames, 2, 48_000, &[160, 2_400, 48_000])
            .expect("loudness generation");
        assert_eq!(layers.len(), 2);
        assert_eq!(layers[0].header.peak_count, 120);
        assert_eq!(layers[0].bytes.len(), 960);
        assert_eq!(fnv64(&layers[0].bytes), 0x0db3_2799_c4f9_f411);
        assert_eq!(layers[1].header.peak_count, 6);
        assert_eq!(layers[1].bytes.len(), 48);
        assert_eq!(fnv64(&layers[1].bytes), 0xbd0f_e30e_9f89_cef0);
    }

    #[test]
    fn rejects_non_nested_loudness_divisions() {
        let pcm = vec![0i16; 48_000];
        assert!(
            build_loudness_layers_pcm16(&pcm, 48_000, 1, 48_000, &[160, 2_400, 47_999],).is_err()
        );
    }
}
