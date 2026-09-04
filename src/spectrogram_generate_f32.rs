use crate::error::{ReaPeaksError, Result};
use crate::format::{GeneratedLayer, LayerHeader, TOKEN_SPECTROGRAM};
use crate::sample_source::F32SampleSource;
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
const INTERNAL_PARALLEL_MIN_TASKS: usize = 8;
const INTERNAL_PARALLEL_MAX_WORKERS: usize = 4;
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
        fine_division - FFT_SIZE as i64
    } else {
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

fn finite_sample(value: f32) -> f32 {
    if value.is_finite() {
        value
    } else {
        0.0
    }
}

fn analyze_base_frame<S: F32SampleSource + ?Sized>(
    pcm: &S,
    frames: usize,
    channels: usize,
    channel: usize,
    base_index: usize,
    fine_division: u32,
    full_window: &[f32; FFT_SIZE],
) -> SpectrogramFrame {
    let start = base_index as i64 * i64::from(fine_division) + analysis_shift(fine_division);
    let mut input = [0.0f64; FFT_SIZE];

    if start < 0 {
        let available = usize::try_from(FFT_SIZE as i64 + start)
            .expect("negative spectrogram leading-window length");
        debug_assert!(available <= frames);
        let leading_window = blackman_harris_window(available);
        for n in 0..available {
            let sample_index = n * channels + channel;
            let sample = finite_sample(pcm.sample_f32(sample_index));
            input[n] = f64::from(sample * leading_window[n]);
        }
    } else {
        for n in 0..FFT_SIZE {
            let source_frame = start + n as i64;
            if source_frame < frames as i64 {
                let sample_index = source_frame as usize * channels + channel;
                let sample = finite_sample(pcm.sample_f32(sample_index));
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

#[inline]
fn internal_worker_count(task_count: usize) -> usize {
    if task_count < INTERNAL_PARALLEL_MIN_TASKS {
        return 1;
    }
    std::thread::available_parallelism().map_or(1, |parallelism| {
        parallelism
            .get()
            .min(INTERNAL_PARALLEL_MAX_WORKERS)
            .min(task_count)
    })
}

fn build_first_frame<S: F32SampleSource + ?Sized>(
    pcm: &S,
    frames: usize,
    channels: usize,
    channel: usize,
    time_frame: usize,
    first_ratio: usize,
    base_count: usize,
    fine_division: u32,
    window: &[f32; FFT_SIZE],
) -> SpectrogramFrame {
    let first_base = time_frame * first_ratio;
    let last_base = (first_base + first_ratio).min(base_count);
    let actual_count = last_base - first_base;
    let mut sums = [0u64; SPECTROGRAM_BINS];
    for base_index in first_base..last_base {
        let frame = analyze_base_frame(
            pcm,
            frames,
            channels,
            channel,
            base_index,
            fine_division,
            window,
        );
        for bin in 0..SPECTROGRAM_BINS {
            sums[bin] += u64::from(frame.bins[bin]);
        }
    }
    let mut bins = [0u16; SPECTROGRAM_BINS];
    for bin in 0..SPECTROGRAM_BINS {
        bins[bin] = (sums[bin] / actual_count as u64) as u16;
    }
    SpectrogramFrame { bins }
}

fn fill_first_frames<S: F32SampleSource + ?Sized>(
    output: &mut [SpectrogramFrame],
    pcm: &S,
    frames: usize,
    channels: usize,
    first_ratio: usize,
    base_count: usize,
    fine_division: u32,
    window: &[f32; FFT_SIZE],
) {
    let workers = internal_worker_count(output.len());
    if workers <= 1 {
        for (index, slot) in output.iter_mut().enumerate() {
            *slot = build_first_frame(
                pcm,
                frames,
                channels,
                index % channels,
                index / channels,
                first_ratio,
                base_count,
                fine_division,
                window,
            );
        }
        return;
    }

    let chunk_size = output.len().div_ceil(workers);
    std::thread::scope(|scope| {
        for (chunk_index, chunk) in output.chunks_mut(chunk_size).enumerate() {
            let start_index = chunk_index * chunk_size;
            scope.spawn(move || {
                for (local_index, slot) in chunk.iter_mut().enumerate() {
                    let index = start_index + local_index;
                    *slot = build_first_frame(
                        pcm,
                        frames,
                        channels,
                        index % channels,
                        index / channels,
                        first_ratio,
                        base_count,
                        fine_division,
                        window,
                    );
                }
            });
        }
    });
}

pub(crate) fn build_spectrogram_layers_f32(
    pcm: &[f32],
    frames: usize,
    channels: usize,
    divisions: &[u32],
) -> Result<Vec<GeneratedLayer>> {
    build_spectrogram_layers_f32_source(pcm, frames, channels, divisions)
}

pub(crate) fn build_spectrogram_layers_f32_source<S: F32SampleSource + ?Sized>(
    pcm: &S,
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
    if pcm.sample_len() < required {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }

    let ratios = validate_divisions(divisions)?;
    let base_count = base_frame_count(frames, divisions[0])?;
    let first_count = if base_count == 0 {
        0
    } else {
        1 + (base_count - 1) / ratios[0]
    };
    let window = blackman_harris_window(FFT_SIZE);
    let capacity = first_count
        .checked_mul(channels)
        .ok_or(ReaPeaksError::InvalidArgument(
            "spectrogram frame capacity overflow",
        ))?;
    let mut current_frames = vec![SpectrogramFrame::default(); capacity];
    let first_ratio = ratios[0];
    fill_first_frames(
        &mut current_frames,
        pcm,
        frames,
        channels,
        first_ratio,
        base_count,
        divisions[0],
        &window,
    );

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

    #[test]
    fn non_finite_samples_are_safe() {
        let values = [
            f32::NAN,
            f32::INFINITY,
            f32::NEG_INFINITY,
            f32::from_bits(1),
            -f32::from_bits(1),
            0.5,
            -0.5,
        ];
        let pcm: Vec<f32> = (0..48_000).map(|i| values[i % values.len()]).collect();
        let layers = build_spectrogram_layers_f32(&pcm, pcm.len(), 1, &[160, 2_400, 48_000])
            .expect("float spectrogram generation");
        assert_eq!(layers.len(), 2);
    }
}
