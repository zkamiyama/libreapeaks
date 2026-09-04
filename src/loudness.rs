use crate::error::{ReaPeaksError, Result};
use crate::format::{GeneratedLayer, LayerHeader, LoudnessPeak, TOKEN_LOUDNESS};
use crate::sample_source::F32SampleSource;
use std::f64::consts::PI;

const MOMENTARY_BLOCKS_25MS: usize = 16;
const SHORT_TERM_BLOCKS_25MS: usize = 120;

#[derive(Debug, Clone, Copy)]
struct FilterCoefficients {
    b: [f64; 5],
    a: [f64; 5],
}

#[derive(Debug, Clone, Copy)]
struct BiquadCoefficients {
    b0: f64,
    b1: f64,
    b2: f64,
    a1: f64,
    a2: f64,
}

#[derive(Debug, Clone, Copy)]
struct KWeightingFilter {
    coefficients: FilterCoefficients,
    state: [f64; 5],
}

impl KWeightingFilter {
    fn new(sample_rate: u32) -> Result<Self> {
        Ok(Self {
            coefficients: k_weighting_coefficients(sample_rate)?,
            state: [0.0; 5],
        })
    }

    #[inline]
    fn process(&mut self, input: f64) -> f64 {
        // REAPER 7.79's mode-3 raw loudness payload matches libebur128's
        // convolved fourth-order Direct Form II filter, not two separately
        // rounded Direct Form I biquads. Keep the operation order explicit:
        // differences below one ulp survive the long DC tail and are visible
        // in the raw f32 momentary-energy records.
        let c = self.coefficients;
        let v0 = input
            - c.a[1] * self.state[1]
            - c.a[2] * self.state[2]
            - c.a[3] * self.state[3]
            - c.a[4] * self.state[4];
        let output = c.b[0] * v0
            + c.b[1] * self.state[1]
            + c.b[2] * self.state[2]
            + c.b[3] * self.state[3]
            + c.b[4] * self.state[4];
        self.state[4] = self.state[3];
        self.state[3] = self.state[2];
        self.state[2] = self.state[1];
        self.state[1] = v0;
        output
    }
}

#[derive(Debug, Clone)]
struct BlockEnergyRing {
    values: Vec<f64>,
    next: usize,
    sum: f64,
}

impl BlockEnergyRing {
    fn new(blocks: usize) -> Result<Self> {
        if blocks == 0 {
            return Err(ReaPeaksError::InvalidArgument(
                "loudness block ring has zero blocks",
            ));
        }
        let bytes = blocks
            .checked_mul(std::mem::size_of::<f64>())
            .filter(|&size| size <= isize::MAX as usize)
            .ok_or(ReaPeaksError::InvalidArgument(
                "loudness block ring is too large",
            ))?;
        let mut values = Vec::new();
        values
            .try_reserve_exact(bytes / std::mem::size_of::<f64>())
            .map_err(|_| ReaPeaksError::InvalidArgument("loudness block allocation failed"))?;
        values.resize(blocks, 0.0);
        Ok(Self {
            values,
            next: 0,
            sum: 0.0,
        })
    }

    #[inline]
    fn push(&mut self, energy: f64) {
        let old = self.values[self.next];
        // The ordering is intentional. REAPER's raw -'r' payload differs in
        // the subnormal DC tail if this is written as (sum - old) + energy.
        self.sum = (self.sum + energy) - old;
        self.values[self.next] = energy;
        self.next += 1;
        if self.next == self.values.len() {
            self.next = 0;
        }
    }

    #[inline]
    fn normalized(&self, normalization_frames: usize) -> f64 {
        self.sum / normalization_frames as f64
    }
}

fn k_weighting_sections(sample_rate: u32) -> Result<(BiquadCoefficients, BiquadCoefficients)> {
    if sample_rate < 16 {
        return Err(ReaPeaksError::InvalidArgument(
            "sample rate is too low for loudness filtering",
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

fn k_weighting_coefficients(sample_rate: u32) -> Result<FilterCoefficients> {
    let (first, second) = k_weighting_sections(sample_rate)?;
    // Spell out libebur128's two quadratic convolutions rather than running
    // two independent biquads. The exact expression tree is part of the
    // REAPER-compatible result at tiny residual energies.
    Ok(FilterCoefficients {
        b: [
            first.b0 * second.b0,
            first.b0 * second.b1 + first.b1 * second.b0,
            first.b0 * second.b2 + first.b1 * second.b1 + first.b2 * second.b0,
            first.b1 * second.b2 + first.b2 * second.b1,
            first.b2 * second.b2,
        ],
        a: [
            1.0,
            second.a1 + first.a1,
            second.a2 + first.a1 * second.a1 + first.a2,
            first.a1 * second.a2 + first.a2 * second.a1,
            first.a2 * second.a2,
        ],
    })
}

fn loudness_windows(sample_rate: u32) -> Result<(usize, usize)> {
    let rate = u64::from(sample_rate);
    // REAPER 7.79/libebur128 derives the normalization spans directly from
    // the source rate. Do not round a nominal 100 ms frame count first: for
    // odd rates that changes the 400 ms denominator by up to two samples and
    // the 3 s denominator by up to fifteen samples, which is visible in the
    // raw f32 -'r' payload.
    let momentary = rate
        .checked_mul(2)
        .map(|value| value / 5)
        .and_then(|value| usize::try_from(value).ok())
        .ok_or(ReaPeaksError::InvalidArgument(
            "momentary loudness window is too large",
        ))?;
    let short_term = rate
        .checked_mul(3)
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

fn loudness_block_frames(sample_rate: u32) -> Result<usize> {
    let frames = u64::from(sample_rate) / 40;
    usize::try_from(frames.max(1))
        .map_err(|_| ReaPeaksError::InvalidArgument("loudness block is too large"))
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
    let block_frames = loudness_block_frames(sample_rate)?;
    let (momentary_window, short_term_window) = loudness_windows(sample_rate)?;

    for channel in 0..channels {
        let mut filter = KWeightingFilter::new(sample_rate)?;
        let mut record_index = 0usize;

        let mut momentary = BlockEnergyRing::new(MOMENTARY_BLOCKS_25MS)?;
        let mut short_term = BlockEnergyRing::new(SHORT_TERM_BLOCKS_25MS)?;
        let mut block_energy = 0.0f64;
        let mut block_fill = 0usize;

        for frame in 0..frames {
            let filtered = filter.process(sample(frame * channels + channel));
            block_energy += filtered * filtered;
            block_fill += 1;

            let completed_frame = frame + 1;
            if block_fill == block_frames {
                momentary.push(block_energy);
                short_term.push(block_energy);
                block_energy = 0.0;
                block_fill = 0;
            }

            if completed_frame % base_division == 0 || completed_frame == frames {
                base_records[record_index * channels + channel] = LoudnessPeak {
                    momentary_energy: momentary.normalized(momentary_window) as f32,
                    short_term_energy: short_term.normalized(short_term_window) as f32,
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
        let record_count = base_record_count / group;
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
            let end = start
                .checked_add(group)
                .ok_or(ReaPeaksError::InvalidArgument(
                    "loudness group end overflow",
                ))?;
            debug_assert!(end <= base_record_count);
            let divisor = group as f64;
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
    build_loudness_layers_f32_source(pcm, frames, channels, sample_rate, divisions)
}

pub(crate) fn build_loudness_layers_f32_source<S: F32SampleSource + ?Sized>(
    pcm: &S,
    frames: usize,
    channels: usize,
    sample_rate: u32,
    divisions: &[u32],
) -> Result<Vec<GeneratedLayer>> {
    let required = frames
        .checked_mul(channels)
        .ok_or(ReaPeaksError::InvalidArgument("frames*channels overflow"))?;
    if pcm.sample_len() < required {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }
    build_loudness_layers(frames, channels, sample_rate, divisions, |index| {
        f64::from(pcm.sample_f32(index))
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

    fn assert_reaper779_loudness_hashes(pcm: &[i16], base_hash: u64, coarse_hash: u64) {
        let frames = pcm.len() / 2;
        let layers = build_loudness_layers_pcm16(pcm, frames, 2, 48_000, &[160, 2_400, 48_000])
            .expect("loudness generation");
        assert_eq!(layers.len(), 2);
        assert_eq!(layers[0].header.peak_count, (frames / 2_400 * 2) as u32);
        assert_eq!(layers[0].bytes.len(), frames / 2_400 * 2 * 8);
        assert_eq!(fnv64(&layers[0].bytes), base_hash);
        assert_eq!(layers[1].header.peak_count, (frames / 48_000 * 2) as u32);
        assert_eq!(layers[1].bytes.len(), frames / 48_000 * 2 * 8);
        assert_eq!(fnv64(&layers[1].bytes), coarse_hash);
    }

    #[test]
    fn odd_rate_loudness_windows_match_reaper779_normalization() {
        assert_eq!(loudness_windows(11_025).unwrap(), (4_410, 33_075));
        assert_eq!(loudness_windows(22_051).unwrap(), (8_820, 66_153));
        assert_eq!(loudness_windows(76_799).unwrap(), (30_719, 230_397));
        assert_eq!(loudness_windows(76_800).unwrap(), (30_720, 230_400));
        assert_eq!(loudness_windows(76_801).unwrap(), (30_720, 230_403));
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
        assert_reaper779_loudness_hashes(&pcm, 0x0db3_2799_c4f9_f411, 0xbd0f_e30e_9f89_cef0);
    }

    #[test]
    fn reaper779_step_loudness_payload_is_byte_exact() {
        let sample_rate = 48_000usize;
        let frames = sample_rate * 3;
        let mut pcm = Vec::with_capacity(frames * 2);
        for frame in 0..frames {
            let value = if frame < sample_rate {
                0
            } else if frame < sample_rate * 2 {
                8_192
            } else {
                16_384
            };
            pcm.extend_from_slice(&[value, value]);
        }
        assert_reaper779_loudness_hashes(&pcm, 0xc4a9_8514_c210_05f9, 0xbc65_0b74_acfe_bb49);
    }

    #[test]
    fn reaper779_impulse_loudness_payload_is_byte_exact() {
        let sample_rate = 48_000usize;
        let frames = sample_rate * 3;
        let mut pcm = vec![0i16; frames * 2];
        for frame in [0usize, 1_200, 38_400, 96_000] {
            pcm[frame * 2] = 32_767;
            pcm[frame * 2 + 1] = 32_767;
        }
        assert_reaper779_loudness_hashes(&pcm, 0x34a7_949b_1e62_c755, 0x2b54_3cf0_b767_c4e9);
    }

    #[test]
    fn rejects_non_nested_loudness_divisions() {
        let pcm = vec![0i16; 48_000];
        assert!(
            build_loudness_layers_pcm16(&pcm, 48_000, 1, 48_000, &[160, 2_400, 47_999],).is_err()
        );
    }
}
