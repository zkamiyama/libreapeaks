use crate::error::{ReaPeaksError, Result};
use crate::format::{GeneratedLayer, LayerHeader, TOKEN_SPECTROGRAM};
use crate::spectrogram::{
    encode_spectrogram_frame, SpectrogramFrame, SPECTROGRAM_BINS,
    SPECTROGRAM_BYTES_PER_CHANNEL_FRAME, SPECTROGRAM_WORDS_PER_CHANNEL_FRAME,
};
use std::f64::consts::PI;

const FFT_SIZE: usize = 256;
const FFT_BINS: usize = FFT_SIZE / 2 + 1;
const BH_A0: f64 = 0.35875;
const BH_A1: f64 = 0.48829;
const BH_A2: f64 = 0.14128;
const BH_A3: f64 = 0.01168;
const PCM_SCALE_F32: f32 = 1.0 / 32768.0;
// REAPER quantizes squared FFT magnitude directly.  Because the WDL real FFT
// is 2x the conventional DFT scale and the Blackman-Harris coefficients are
// normalized to unit sum, an exact-bin sine of amplitude A produces power A^2.
const POWER_LOG_SCALE: f64 = 88.92179516969081;
const CODE_BIAS: f64 = 4095.5;
const CODE_MAX: u16 = 4095;

#[derive(Debug, Clone, Copy, Default)]
struct C64 {
    re: f64,
    im: f64,
}

#[cfg(not(feature = "strict-wdl"))]
fn real_fft_256(input: &[f64; FFT_SIZE]) -> [C64; FFT_BINS] {
    let mut values = [C64::default(); FFT_SIZE];
    for (dst, &sample) in values.iter_mut().zip(input.iter()) {
        dst.re = sample;
    }

    let mut j = 0usize;
    for i in 1..FFT_SIZE {
        let mut bit = FFT_SIZE >> 1;
        while j & bit != 0 {
            j ^= bit;
            bit >>= 1;
        }
        j ^= bit;
        if i < j {
            values.swap(i, j);
        }
    }

    let mut len = 2usize;
    while len <= FFT_SIZE {
        let theta = -2.0 * PI / len as f64;
        let wlen = C64 {
            re: theta.cos(),
            im: theta.sin(),
        };
        for base in (0..FFT_SIZE).step_by(len) {
            let mut w = C64 { re: 1.0, im: 0.0 };
            for k in 0..len / 2 {
                let u = values[base + k];
                let q = values[base + k + len / 2];
                let v = C64 {
                    re: q.re * w.re - q.im * w.im,
                    im: q.re * w.im + q.im * w.re,
                };
                values[base + k] = C64 {
                    re: u.re + v.re,
                    im: u.im + v.im,
                };
                values[base + k + len / 2] = C64 {
                    re: u.re - v.re,
                    im: u.im - v.im,
                };
                w = C64 {
                    re: w.re * wlen.re - w.im * wlen.im,
                    im: w.re * wlen.im + w.im * wlen.re,
                };
            }
        }
        len <<= 1;
    }

    let mut out = [C64::default(); FFT_BINS];
    for bin in 0..FFT_BINS {
        out[bin] = C64 {
            re: values[bin].re * 2.0,
            im: values[bin].im * 2.0,
        };
    }
    out
}

#[cfg(feature = "strict-wdl")]
fn real_fft_256(input: &[f64; FFT_SIZE]) -> [C64; FFT_BINS] {
    unsafe extern "C" {
        fn rpk_wdl_real_fft_256(input: *const f64, out_re: *mut f64, out_im: *mut f64) -> i32;
    }

    let mut re = [0.0f64; FFT_BINS];
    let mut im = [0.0f64; FFT_BINS];
    let rc = unsafe { rpk_wdl_real_fft_256(input.as_ptr(), re.as_mut_ptr(), im.as_mut_ptr()) };
    assert_eq!(rc, 0, "WDL 256-point FFT bridge failed");

    let mut out = [C64::default(); FFT_BINS];
    for bin in 0..FFT_BINS {
        out[bin] = C64 {
            re: re[bin],
            im: im[bin],
        };
    }
    out
}

fn blackman_harris_window(length: usize) -> [f32; FFT_SIZE] {
    debug_assert!((1..=FFT_SIZE).contains(&length));

    // REAPER 7.79 builds this cache in a mixed double/single-precision path:
    // phase/cosines and the unnormalized sum are double, each raw coefficient
    // is stored as float, 1/sum is converted to float, then normalization is
    // a float multiply.  The phase is advanced by repeated addition rather
    // than recomputed from n.  This exact sequence is visible in the pinned
    // binary at 0x94a890..0x94aa25 and is required for coherent-tone bins.
    let mut window = [0.0f32; FFT_SIZE];
    let mut sum = 0.0f64;
    let step = if length == 1 {
        0.0
    } else {
        2.0 * PI / (length - 1) as f64
    };
    let mut phase = 0.0f64;
    for value in &mut window[..length] {
        let cos1 = phase.cos();
        let cos2 = (phase + phase).cos();
        let cos3 = (3.0 * phase).cos();
        let mut raw = BH_A0 - BH_A1 * cos1;
        raw += BH_A2 * cos2;
        raw -= BH_A3 * cos3;
        sum += raw;
        *value = raw as f32;
        phase += step;
    }

    let normalize = (1.0 / sum) as f32;
    for value in &mut window[..length] {
        *value *= normalize;
    }
    window
}

fn quantize_power(power: f64) -> u16 {
    if !power.is_finite() || power <= 0.0 {
        return 0;
    }
    if power >= 1.0 {
        return CODE_MAX;
    }

    // REAPER calls log(), multiplies by a precomputed natural-log scale, adds
    // 4095.5, and converts with truncation (cvttsd2si).  Operating on power
    // avoids the sqrt/hypot rounding that otherwise changes low sidelobes.
    let raw = power.ln() * POWER_LOG_SCALE + CODE_BIAS;
    if raw <= 0.0 {
        0
    } else {
        raw.trunc() as u16
    }
}

fn analysis_shift(fine_division: u32) -> i64 {
    let fine_division = i64::from(fine_division);
    if fine_division >= FFT_SIZE as i64 {
        // Once the base division is at least one FFT window, REAPER right-aligns
        // the 256-sample analysis window to the base boundary. At 96 kHz the
        // recovered 320-sample division therefore starts at +64 and ends at
        // sample 320. This phase is required for off-bin low-frequency tones.
        fine_division - FFT_SIZE as i64
    } else {
        // For overlapping base windows REAPER centers the analysis window on
        // the division boundary; negative integer division truncates toward 0,
        // matching the 44.1/32/48 kHz oracle boundaries.
        (fine_division - FFT_SIZE as i64) / 2
    }
}

fn base_frame_count(frames: usize, fine_division: u32) -> Result<usize> {
    if fine_division == 0 {
        return Err(ReaPeaksError::InvalidArgument(
            "spectrogram fine division=0",
        ));
    }
    let frames = i64::try_from(frames)
        .map_err(|_| ReaPeaksError::InvalidArgument("frame count exceeds i64"))?;
    let first_end = analysis_shift(fine_division) + FFT_SIZE as i64;
    if frames < first_end {
        return Ok(0);
    }
    let count = 1 + (frames - first_end) / i64::from(fine_division);
    usize::try_from(count)
        .map_err(|_| ReaPeaksError::InvalidArgument("spectrogram frame count overflow"))
}

fn validate_divisions(divisions: &[u32]) -> Result<Vec<usize>> {
    if divisions.len() < 2 {
        return Err(ReaPeaksError::InvalidArgument(
            "spectrogram generation requires at least two divisions",
        ));
    }
    if divisions[0] == 0 {
        return Err(ReaPeaksError::InvalidArgument("division=0"));
    }
    let mut ratios = Vec::with_capacity(divisions.len() - 1);
    for pair in divisions.windows(2) {
        if pair[1] == 0 || pair[1] % pair[0] != 0 {
            return Err(ReaPeaksError::InvalidArgument(
                "spectrogram divisions must be nested integer multiples",
            ));
        }
        ratios.push((pair[1] / pair[0]) as usize);
    }
    Ok(ratios)
}

fn time_counts(frames: usize, divisions: &[u32]) -> Result<Vec<usize>> {
    let ratios = validate_divisions(divisions)?;
    let base_count = base_frame_count(frames, divisions[0])?;
    let first_count = if base_count == 0 {
        0
    } else {
        1 + (base_count - 1) / ratios[0]
    };
    let mut counts = Vec::with_capacity(ratios.len());
    counts.push(first_count);
    let mut previous = first_count;
    for &ratio in ratios.iter().skip(1) {
        previous /= ratio;
        counts.push(previous);
    }
    Ok(counts)
}

fn analyze_base_frame(
    pcm: &[i16],
    frames: usize,
    channels: usize,
    channel: usize,
    base_index: usize,
    fine_division: u32,
    full_window: &[f32; FFT_SIZE],
) -> SpectrogramFrame {
    let start = base_index as i64 * i64::from(fine_division) + analysis_shift(fine_division);
    let mut input = [0.0f64; FFT_SIZE];

    // REAPER does not synthesize leading zero samples for the first analysis
    // window. At 48 kHz the recovered start is -48, so the first available
    // 208 samples receive a symmetric 208-point Blackman-Harris window and are
    // then zero-padded to the 256-point FFT.
    if start < 0 {
        let available = usize::try_from(FFT_SIZE as i64 + start)
            .expect("negative spectrogram leading-window length");
        debug_assert!(available <= frames);
        let leading_window = blackman_harris_window(available);
        for n in 0..available {
            let sample_index = n * channels + channel;
            let sample = f32::from(pcm[sample_index]) * PCM_SCALE_F32;
            input[n] = f64::from(sample * leading_window[n]);
        }
    } else {
        for n in 0..FFT_SIZE {
            let source_frame = start + n as i64;
            if source_frame < frames as i64 {
                let sample_index = source_frame as usize * channels + channel;
                let sample = f32::from(pcm[sample_index]) * PCM_SCALE_F32;
                input[n] = f64::from(sample * full_window[n]);
            }
        }
    }

    let spectrum = real_fft_256(&input);
    let mut bins = [0u16; SPECTROGRAM_BINS];
    for stored_bin in 0..SPECTROGRAM_BINS {
        let value = spectrum[stored_bin + 1];
        let power = value.re * value.re + value.im * value.im;
        bins[stored_bin] = quantize_power(power);
    }
    SpectrogramFrame { bins }
}

fn average_frames<'a, I>(frames: I, count: usize) -> SpectrogramFrame
where
    I: IntoIterator<Item = &'a SpectrogramFrame>,
{
    let mut sums = [0u64; SPECTROGRAM_BINS];
    for frame in frames {
        for bin in 0..SPECTROGRAM_BINS {
            sums[bin] += u64::from(frame.bins[bin]);
        }
    }
    let mut bins = [0u16; SPECTROGRAM_BINS];
    for bin in 0..SPECTROGRAM_BINS {
        bins[bin] = (sums[bin] / count as u64) as u16;
    }
    SpectrogramFrame { bins }
}

fn encode_layer(frames: &[SpectrogramFrame], channels: usize) -> Result<GeneratedLayer> {
    if channels == 0 || frames.len() % channels != 0 {
        return Err(ReaPeaksError::InvalidArgument(
            "invalid spectrogram channel layout",
        ));
    }
    let time_frames = frames.len() / channels;
    let peak_count = time_frames
        .checked_mul(SPECTROGRAM_WORDS_PER_CHANNEL_FRAME)
        .and_then(|value| u32::try_from(value).ok())
        .ok_or(ReaPeaksError::InvalidArgument(
            "spectrogram word count exceeds u32",
        ))?;
    let capacity = frames
        .len()
        .checked_mul(SPECTROGRAM_BYTES_PER_CHANNEL_FRAME)
        .filter(|&size| size <= isize::MAX as usize)
        .ok_or(ReaPeaksError::InvalidArgument(
            "spectrogram payload is too large",
        ))?;
    let mut bytes = Vec::with_capacity(capacity);
    for frame in frames {
        bytes.extend_from_slice(&encode_spectrogram_frame(frame)?);
    }
    Ok(GeneratedLayer {
        header: LayerHeader {
            division: TOKEN_SPECTROGRAM,
            peak_count,
        },
        bytes,
    })
}

pub(crate) fn build_spectrogram_layers_pcm16(
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

    let ratios = validate_divisions(divisions)?;
    let counts = time_counts(frames, divisions)?;
    let base_count = base_frame_count(frames, divisions[0])?;
    let window = blackman_harris_window(FFT_SIZE);

    let first_count = counts[0];
    let first_capacity =
        first_count
            .checked_mul(channels)
            .ok_or(ReaPeaksError::InvalidArgument(
                "spectrogram frame capacity overflow",
            ))?;
    let mut current_frames = Vec::with_capacity(first_capacity);
    let first_ratio = ratios[0];
    for time_frame in 0..first_count {
        let first_base = time_frame * first_ratio;
        let last_base = (first_base + first_ratio).min(base_count);
        let actual_count = last_base - first_base;
        for channel in 0..channels {
            let mut sums = [0u64; SPECTROGRAM_BINS];
            for base_index in first_base..last_base {
                let frame = analyze_base_frame(
                    pcm,
                    frames,
                    channels,
                    channel,
                    base_index,
                    divisions[0],
                    &window,
                );
                for bin in 0..SPECTROGRAM_BINS {
                    sums[bin] += u64::from(frame.bins[bin]);
                }
            }
            let mut bins = [0u16; SPECTROGRAM_BINS];
            for bin in 0..SPECTROGRAM_BINS {
                bins[bin] = (sums[bin] / actual_count as u64) as u16;
            }
            current_frames.push(SpectrogramFrame { bins });
        }
    }

    let mut layers = Vec::with_capacity(divisions.len() - 1);
    layers.push(encode_layer(&current_frames, channels)?);

    for &ratio in ratios.iter().skip(1) {
        let previous_time_frames = current_frames.len() / channels;
        let output_time_frames = previous_time_frames / ratio;
        let capacity =
            output_time_frames
                .checked_mul(channels)
                .ok_or(ReaPeaksError::InvalidArgument(
                    "spectrogram frame capacity overflow",
                ))?;
        let mut next_frames = Vec::with_capacity(capacity);
        for output_frame in 0..output_time_frames {
            let first = output_frame * ratio;
            let last = first + ratio;
            for channel in 0..channels {
                let records = (first..last).map(|time| &current_frames[time * channels + channel]);
                next_frames.push(average_frames(records, ratio));
            }
        }
        layers.push(encode_layer(&next_frames, channels)?);
        current_frames = next_frames;
    }

    Ok(layers)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::spectrogram::decode_spectrogram_frame;

    #[test]
    fn scheduler_matches_reaper779_cross_rate_boundaries() {
        let d48 = [160, 2_400, 48_000];
        assert_eq!(time_counts(207, &d48).unwrap(), vec![0, 0]);
        assert_eq!(time_counts(208, &d48).unwrap(), vec![1, 0]);
        assert_eq!(time_counts(2_607, &d48).unwrap(), vec![1, 0]);
        assert_eq!(time_counts(2_608, &d48).unwrap(), vec![2, 0]);
        assert_eq!(time_counts(5_007, &d48).unwrap(), vec![2, 0]);
        assert_eq!(time_counts(5_008, &d48).unwrap(), vec![3, 0]);
        assert_eq!(time_counts(45_807, &d48).unwrap(), vec![19, 0]);
        assert_eq!(time_counts(45_808, &d48).unwrap(), vec![20, 1]);

        let d441 = [147, 2_205, 44_100];
        assert_eq!(time_counts(201, &d441).unwrap(), vec![0, 0]);
        assert_eq!(time_counts(202, &d441).unwrap(), vec![1, 0]);
        assert_eq!(time_counts(2_406, &d441).unwrap(), vec![1, 0]);
        assert_eq!(time_counts(2_407, &d441).unwrap(), vec![2, 0]);
        assert_eq!(time_counts(42_096, &d441).unwrap(), vec![19, 0]);
        assert_eq!(time_counts(42_097, &d441).unwrap(), vec![20, 1]);

        let d32 = [106, 1_696, 32_224];
        assert_eq!(time_counts(180, &d32).unwrap(), vec![0, 0]);
        assert_eq!(time_counts(181, &d32).unwrap(), vec![1, 0]);
        assert_eq!(time_counts(1_876, &d32).unwrap(), vec![1, 0]);
        assert_eq!(time_counts(1_877, &d32).unwrap(), vec![2, 0]);
        assert_eq!(time_counts(30_708, &d32).unwrap(), vec![18, 0]);
        assert_eq!(time_counts(30_709, &d32).unwrap(), vec![19, 1]);

        let d96 = [320, 4_800, 96_000];
        assert_eq!(analysis_shift(d96[0]), 64);
        assert_eq!(time_counts(319, &d96).unwrap(), vec![0, 0]);
        assert_eq!(time_counts(320, &d96).unwrap(), vec![1, 0]);
        assert_eq!(time_counts(4_767, &d96).unwrap(), vec![1, 0]);
        assert_eq!(time_counts(4_768, &d96).unwrap(), vec![1, 0]);
        assert_eq!(time_counts(5_119, &d96).unwrap(), vec![1, 0]);
        assert_eq!(time_counts(5_120, &d96).unwrap(), vec![2, 0]);
        assert_eq!(time_counts(91_519, &d96).unwrap(), vec![19, 0]);
        assert_eq!(time_counts(91_520, &d96).unwrap(), vec![20, 1]);
        assert_eq!(time_counts(288_319, &d96).unwrap(), vec![60, 3]);
        assert_eq!(time_counts(288_320, &d96).unwrap(), vec![61, 3]);
        assert_eq!(time_counts(292_767, &d96).unwrap(), vec![61, 3]);
        assert_eq!(time_counts(292_768, &d96).unwrap(), vec![61, 3]);
    }

    #[test]
    fn quantizer_matches_reaper779_exact_bin_amplitudes() {
        for (amplitude, expected) in [
            (1.0, 4095),
            (0.9, 4076),
            (0.75, 4044),
            (0.6, 4004),
            (0.5, 3972),
            (0.4, 3932),
            (0.3, 3881),
            (0.25, 3848),
            (0.2, 3809),
            (0.125, 3725),
            (0.0625, 3602),
            (0.01, 3276),
        ] {
            assert_eq!(quantize_power(amplitude * amplitude), expected);
        }
        assert_eq!(quantize_power(0.0), 0);
        assert_eq!(quantize_power(1.0e-24), 0);
    }

    #[test]
    fn leading_edge_resizes_window_to_available_samples() {
        let expected = [
            (0usize, 1723u16),
            (1, 1758),
            (16, 2521),
            (32, 2894),
            (47, 3124),
            (48, 3137),
            (64, 3299),
            (96, 3447),
            (127, 3399),
            (159, 3137),
            (191, 2521),
            (207, 1723),
        ];
        for (position, code) in expected {
            let mut pcm = vec![0i16; 208];
            pcm[position] = 32_767;
            let layers =
                build_spectrogram_layers_pcm16(&pcm, 208, 1, &[160, 2_400, 48_000]).unwrap();
            let frame =
                decode_spectrogram_frame(&layers[0].bytes[..SPECTROGRAM_BYTES_PER_CHANNEL_FRAME])
                    .unwrap();
            assert!(frame.bins.iter().all(|&actual| actual == code));
        }
    }

    #[test]
    fn exact_bin_tone_matches_reaper779_codes() {
        let sample_rate = 48_000usize;
        let mut pcm = Vec::with_capacity(sample_rate);
        for frame in 0..sample_rate {
            let phase = 2.0 * PI * 6_000.0 * frame as f64 / sample_rate as f64;
            pcm.push((0.5 * 32_767.0 * phase.sin()).round() as i16);
        }
        let layers =
            build_spectrogram_layers_pcm16(&pcm, sample_rate, 1, &[160, 2_400, 48_000]).unwrap();
        assert_eq!(layers.len(), 2);
        assert_eq!(layers[0].header.peak_count, 20 * 48);
        assert_eq!(layers[1].header.peak_count, 48);
        let offset = 10 * SPECTROGRAM_BYTES_PER_CHANNEL_FRAME;
        let frame = decode_spectrogram_frame(
            &layers[0].bytes[offset..offset + SPECTROGRAM_BYTES_PER_CHANNEL_FRAME],
        )
        .unwrap();
        assert_eq!(frame.bins[30], 3904);
        assert_eq!(frame.bins[31], 3972);
        assert_eq!(frame.bins[32], 3904);
    }

    #[test]
    fn impulse_matches_reaper779_fine_and_coarse_aggregation() {
        let mut pcm = vec![0i16; 48_000];
        pcm[24_000] = 32_767;
        let layers =
            build_spectrogram_layers_pcm16(&pcm, 48_000, 1, &[160, 2_400, 48_000]).unwrap();
        let frame9 = decode_spectrogram_frame(
            &layers[0].bytes
                [9 * SPECTROGRAM_BYTES_PER_CHANNEL_FRAME..10 * SPECTROGRAM_BYTES_PER_CHANNEL_FRAME],
        )
        .unwrap();
        let frame10 = decode_spectrogram_frame(
            &layers[0].bytes[10 * SPECTROGRAM_BYTES_PER_CHANNEL_FRAME
                ..11 * SPECTROGRAM_BYTES_PER_CHANNEL_FRAME],
        )
        .unwrap();
        let coarse =
            decode_spectrogram_frame(&layers[1].bytes[..SPECTROGRAM_BYTES_PER_CHANNEL_FRAME])
                .unwrap();
        assert!(frame9.bins.iter().all(|&code| code == 197));
        assert!(frame10.bins.iter().all(|&code| code == 198));
        assert!(coarse.bins.iter().all(|&code| code == 19));
    }
}
