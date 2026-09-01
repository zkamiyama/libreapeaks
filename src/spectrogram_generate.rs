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
const CODE_MAX: f64 = 4095.0;
const CODES_PER_DECADE: f64 = 409.5;

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
    out.copy_from_slice(&values[..FFT_BINS]);
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

fn blackman_harris_window() -> ([f64; FFT_SIZE], f64) {
    let mut window = [0.0f64; FFT_SIZE];
    let mut sum = 0.0;
    for (n, value) in window.iter_mut().enumerate() {
        let phase = 2.0 * PI * n as f64 / (FFT_SIZE - 1) as f64;
        *value =
            BH_A0 - BH_A1 * phase.cos() + BH_A2 * (2.0 * phase).cos() - BH_A3 * (3.0 * phase).cos();
        sum += *value;
    }
    (window, sum)
}

fn quantize_amplitude(amplitude: f64) -> u16 {
    if !amplitude.is_finite() || amplitude <= 0.0 {
        return 0;
    }
    let raw = CODE_MAX + CODES_PER_DECADE * amplitude.log10();
    raw.round().clamp(0.0, CODE_MAX) as u16
}

fn analysis_shift(fine_division: u32) -> i64 {
    (i64::from(fine_division) - FFT_SIZE as i64) / 2
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
    window: &[f64; FFT_SIZE],
    window_sum: f64,
) -> SpectrogramFrame {
    let start = base_index as i64 * i64::from(fine_division) + analysis_shift(fine_division);
    let mut input = [0.0f64; FFT_SIZE];
    for n in 0..FFT_SIZE {
        let source_frame = start + n as i64;
        if source_frame >= 0 && source_frame < frames as i64 {
            let sample_index = source_frame as usize * channels + channel;
            input[n] = f64::from(pcm[sample_index]) * (1.0 / 32768.0) * window[n];
        }
    }

    let spectrum = real_fft_256(&input);
    let mut bins = [0u16; SPECTROGRAM_BINS];
    for stored_bin in 0..SPECTROGRAM_BINS {
        let value = spectrum[stored_bin + 1];
        let amplitude = 2.0 * value.re.hypot(value.im) / window_sum;
        bins[stored_bin] = quantize_amplitude(amplitude);
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
    let (window, window_sum) = blackman_harris_window();

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
                    window_sum,
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
            assert_eq!(quantize_amplitude(amplitude), expected);
        }
        assert_eq!(quantize_amplitude(0.0), 0);
        assert_eq!(quantize_amplitude(1.0e-12), 0);
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
