use crate::error::{ReaPeaksError, Result};
use crate::format::{GeneratedLayer, LayerHeader, SpectralPeak, TOKEN_SPECTRAL};
use crate::sample_source::F32SampleSource;
use std::f64::consts::PI;
#[cfg(feature = "strict-wdl")]
use std::ffi::c_void;

const ANALYSIS_RATE: f64 = 22_050.0;
const FFT_N: usize = 1024;
const HALF_BINS: usize = 512;

#[repr(C)]
#[derive(Debug, Clone, Copy, Default)]
struct C64 {
    re: f64,
    im: f64,
}

#[derive(Debug, Clone, Copy, Default)]
struct C32 {
    re: f32,
    im: f32,
}

#[derive(Debug, Clone, Copy)]
enum ExpectedCount {
    SourceDomain,
    #[cfg(feature = "strict-wdl")]
    Exact(usize),
    #[cfg(feature = "strict-wdl")]
    AnalysisDomain,
}

#[inline]
fn wrap_phase(mut x: f64) -> f64 {
    x %= 2.0;
    if x <= -1.0 {
        x += 2.0;
    } else if x > 1.0 {
        x -= 2.0;
    }
    x
}

#[cfg(feature = "strict-wdl")]
#[inline]
fn analysis_domain_fine_count(analysis_frames: usize, source_rate: u32, division: u32) -> usize {
    if analysis_frames == 0 || division == 0 || source_rate == 0 {
        return 0;
    }
    let hop = division as f64 * ANALYSIS_RATE / source_rate as f64;
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

#[cfg(not(feature = "strict-wdl"))]
fn fft_radix2(input: &[f64; FFT_N]) -> [C64; FFT_N] {
    let mut a = [C64::default(); FFT_N];
    for (dst, &x) in a.iter_mut().zip(input.iter()) {
        dst.re = x;
    }

    let mut j = 0usize;
    for i in 1..FFT_N {
        let mut bit = FFT_N >> 1;
        while j & bit != 0 {
            j ^= bit;
            bit >>= 1;
        }
        j ^= bit;
        if i < j {
            a.swap(i, j);
        }
    }

    let mut len = 2usize;
    while len <= FFT_N {
        let theta = -2.0 * PI / len as f64;
        let wlen = C64 {
            re: theta.cos(),
            im: theta.sin(),
        };
        for base in (0..FFT_N).step_by(len) {
            let mut w = C64 { re: 1.0, im: 0.0 };
            for k in 0..len / 2 {
                let u = a[base + k];
                let q = a[base + k + len / 2];
                let v = C64 {
                    re: q.re * w.re - q.im * w.im,
                    im: q.re * w.im + q.im * w.re,
                };
                a[base + k] = C64 {
                    re: u.re + v.re,
                    im: u.im + v.im,
                };
                a[base + k + len / 2] = C64 {
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
    a
}

#[cfg(feature = "strict-wdl")]
fn real_fft_1024(input: &mut [f64; FFT_N]) -> [C64; HALF_BINS + 1] {
    unsafe extern "C" {
        fn rpk_wdl_real_fft_1024_inplace(input: *mut f64, output: *mut f64) -> i32;
    }
    let mut out = [C64::default(); HALF_BINS + 1];
    let rc = unsafe {
        rpk_wdl_real_fft_1024_inplace(input.as_mut_ptr(), out.as_mut_ptr().cast::<f64>())
    };
    assert_eq!(rc, 0, "WDL FFT bridge failed");
    out
}

#[cfg(not(feature = "strict-wdl"))]
fn real_fft_1024(input: &[f64; FFT_N]) -> [C64; HALF_BINS + 1] {
    let full = fft_radix2(input);
    let mut out = [C64::default(); HALF_BINS + 1];
    out.copy_from_slice(&full[..=HALF_BINS]);
    out
}

#[cfg(not(feature = "strict-wdl"))]
fn apply_wdl_iir(interleaved: &mut [f64], frames: usize, channels: usize, ratio: f64) {
    if ratio <= 1.0 || frames == 0 {
        return;
    }
    // WDL_Resampler defaults are float constants, even when audio samples are
    // double. Preserve that precision here.
    let fpos2 = 0.693f32 as f64;
    let q = 0.707f32 as f64;
    let fpos = (1.0 / ratio) * fpos2;
    let pos = fpos * PI;
    let cpos = pos.cos();
    let spos = pos.sin();
    let alpha = spos / (2.0 * q);
    let sc = 1.0 / (1.0 + alpha);
    let b1 = (1.0 - cpos) * sc;
    let b0 = b1 * 0.5;
    let b2 = b0;
    let a1 = -2.0 * cpos * sc;
    let a2 = (1.0 - alpha) * sc;

    for c in 0..channels {
        let (mut h0, mut h1, mut h2, mut h3) = (0.0, 0.0, 0.0, 0.0);
        for f in 0..frames {
            let idx = f * channels + c;
            let x = interleaved[idx];
            let y = x * b0 + h0 * b1 + h1 * b2 - h2 * a1 - h3 * a2;
            h1 = h0;
            h0 = x;
            h3 = h2;
            h2 = y;
            interleaved[idx] = y;
        }
    }
}

#[cfg(feature = "strict-wdl")]
fn resample_to_analysis(
    input: &[f64],
    frames: usize,
    channels: usize,
    source_rate: u32,
) -> Vec<f64> {
    unsafe extern "C" {
        fn rpk_wdl_resample_all(
            input: *const f64,
            input_frames: i64,
            channels: i32,
            input_rate: f64,
            output_rate: f64,
            output: *mut f64,
            output_capacity_frames: i64,
        ) -> i64;
    }
    let cap_frames =
        ((frames as f64 * ANALYSIS_RATE / source_rate as f64).ceil() as usize).saturating_add(4096);
    let mut out = vec![0.0f64; cap_frames.saturating_mul(channels)];
    let got = unsafe {
        rpk_wdl_resample_all(
            input.as_ptr(),
            frames as i64,
            channels as i32,
            source_rate as f64,
            ANALYSIS_RATE,
            out.as_mut_ptr(),
            cap_frames as i64,
        )
    };
    if got <= 0 {
        return Vec::new();
    }
    out.truncate(got as usize * channels);
    out
}

#[cfg(feature = "strict-wdl")]
fn resample_samples_to_analysis<F>(
    frames: usize,
    channels: usize,
    source_rate: u32,
    mut sample: F,
) -> Vec<f64>
where
    F: FnMut(usize) -> f64,
{
    unsafe extern "C" {
        fn rpk_wdl_resampler_create(
            channels: i32,
            input_rate: f64,
            output_rate: f64,
        ) -> *mut c_void;
        fn rpk_wdl_resampler_destroy(state: *mut c_void);
        fn rpk_wdl_resampler_prepare(
            state: *mut c_void,
            request_frames: i32,
            input_buffer: *mut *mut f64,
        ) -> i32;
        fn rpk_wdl_resampler_out(
            state: *mut c_void,
            output: *mut f64,
            input_frames: i32,
            output_capacity_frames: i32,
        ) -> i32;
    }

    struct ResamplerGuard(*mut c_void);
    impl Drop for ResamplerGuard {
        fn drop(&mut self) {
            unsafe { rpk_wdl_resampler_destroy(self.0) };
        }
    }

    if frames == 0 || channels == 0 {
        return Vec::new();
    }
    let state =
        unsafe { rpk_wdl_resampler_create(channels as i32, source_rate as f64, ANALYSIS_RATE) };
    if state.is_null() {
        return Vec::new();
    }
    let _guard = ResamplerGuard(state);

    let cap_frames =
        ((frames as f64 * ANALYSIS_RATE / source_rate as f64).ceil() as usize).saturating_add(4096);
    let mut out = vec![0.0f64; cap_frames.saturating_mul(channels)];
    let block_frames = (2048 / channels).max(1);
    let mut in_pos = 0usize;
    let mut out_pos = 0usize;

    while in_pos < frames && out_pos < cap_frames {
        let request_frames = block_frames.min(frames - in_pos);
        let mut inbuf = std::ptr::null_mut();
        let wanted = unsafe { rpk_wdl_resampler_prepare(state, request_frames as i32, &mut inbuf) };
        if wanted <= 0 || inbuf.is_null() {
            return Vec::new();
        }
        let avail = (wanted as usize).min(frames - in_pos);
        if avail == 0 {
            return Vec::new();
        }

        let first_sample = in_pos.saturating_mul(channels);
        let sample_count = avail.saturating_mul(channels);
        for offset in 0..sample_count {
            unsafe {
                *inbuf.add(offset) = sample(first_sample + offset);
            }
        }

        let out_cap = block_frames.min(cap_frames - out_pos);
        if out_cap == 0 {
            return Vec::new();
        }
        let output_offset = out_pos.saturating_mul(channels);
        let got = unsafe {
            rpk_wdl_resampler_out(
                state,
                out.as_mut_ptr().add(output_offset),
                avail as i32,
                out_cap as i32,
            )
        };
        if got < 0 || got as usize > out_cap {
            return Vec::new();
        }

        in_pos += avail;
        out_pos += got as usize;
        if avail < wanted as usize {
            break;
        }
    }

    out.truncate(out_pos.saturating_mul(channels));
    out
}

#[cfg(feature = "strict-wdl")]
fn resample_i16_to_analysis(
    pcm: &[i16],
    frames: usize,
    channels: usize,
    source_rate: u32,
) -> Vec<f64> {
    resample_samples_to_analysis(frames, channels, source_rate, |index| {
        pcm[index] as f64 / 32768.0
    })
}

#[cfg(feature = "strict-wdl")]
fn resample_f32_to_analysis(
    pcm: &[f32],
    frames: usize,
    channels: usize,
    source_rate: u32,
) -> Vec<f64> {
    resample_samples_to_analysis(frames, channels, source_rate, |index| f64::from(pcm[index]))
}

#[cfg(feature = "strict-wdl")]
fn resample_f32_source_to_analysis<S: F32SampleSource + ?Sized>(
    pcm: &S,
    frames: usize,
    channels: usize,
    source_rate: u32,
) -> Vec<f64> {
    resample_samples_to_analysis(frames, channels, source_rate, |index| {
        f64::from(pcm.sample_f32(index))
    })
}

#[cfg(feature = "strict-wdl")]
struct StreamingSpectralAnalyzer {
    channels: usize,
    hop: f64,
    phase: f64,
    window: Vec<f32>,
    ring: Vec<f32>,
    write_pos: usize,
    elapsed: usize,
    previous: Vec<[C32; HALF_BINS + 1]>,
    out: Vec<SpectralPeak>,
    expected: Option<usize>,
}

#[cfg(feature = "strict-wdl")]
impl StreamingSpectralAnalyzer {
    fn new(channels: usize, source_rate: u32, division: u32, expected: Option<usize>) -> Self {
        let hop = division as f64 * ANALYSIS_RATE / source_rate as f64;
        let rounded = (hop + 0.5).floor() as i32;
        let nwin = rounded.max(1024) as usize;
        let phase = if rounded <= 1023 {
            (rounded - 1024) as f64 * 0.5
        } else {
            0.0
        };
        let window: Vec<f32> = if nwin <= 1 {
            vec![1.0]
        } else {
            let half = nwin / 2;
            let step = 2.0 * PI / (nwin - 1) as f64;
            let mut table = Vec::with_capacity(half + 1);
            let mut angle = 0.0f64;
            for _ in 0..=half {
                table.push((0.5 - 0.5 * angle.cos()) as f32);
                angle += step;
            }
            (0..nwin)
                .map(|i| {
                    let j = if i <= half { i } else { nwin - i };
                    table[j]
                })
                .collect()
        };
        let output_capacity = expected
            .and_then(|count| count.checked_mul(channels))
            .unwrap_or(0);
        Self {
            channels,
            hop,
            phase,
            window,
            ring: vec![0.0f32; nwin * channels],
            write_pos: 0,
            elapsed: 0,
            previous: vec![[C32::default(); HALF_BINS + 1]; channels],
            out: Vec::with_capacity(output_capacity),
            expected,
        }
    }

    fn push_interleaved(&mut self, samples: &[f64]) -> bool {
        debug_assert_eq!(samples.len() % self.channels, 0);
        for frame in samples.chunks_exact(self.channels) {
            for (channel, &sample) in frame.iter().enumerate() {
                self.ring[self.write_pos * self.channels + channel] = sample as f32;
            }
            self.write_pos += 1;
            if self.write_pos == self.window.len() {
                self.write_pos = 0;
            }
            self.phase += 1.0;
            self.elapsed += 1;
            while self.phase >= self.hop {
                for channel in 0..self.channels {
                    let (peak, next) = analyze_channel(
                        &self.ring,
                        self.write_pos,
                        self.channels,
                        channel,
                        &self.window,
                        &self.previous[channel],
                        self.elapsed,
                    );
                    self.out.push(peak);
                    self.previous[channel] = next;
                }
                self.elapsed = 0;
                self.phase -= self.hop;
                if self
                    .expected
                    .is_some_and(|expected| self.out.len() / self.channels >= expected)
                {
                    return true;
                }
            }
        }
        false
    }

    fn into_output(mut self) -> Vec<SpectralPeak> {
        if let Some(expected) = self.expected {
            self.out.truncate(expected.saturating_mul(self.channels));
        }
        self.out
    }
}

#[cfg(feature = "strict-wdl")]
fn analyze_streaming_samples<F>(
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
    expected_mode: ExpectedCount,
    mut sample: F,
) -> Result<Vec<SpectralPeak>>
where
    F: FnMut(usize) -> f64,
{
    unsafe extern "C" {
        fn rpk_wdl_resampler_create(
            channels: i32,
            input_rate: f64,
            output_rate: f64,
        ) -> *mut c_void;
        fn rpk_wdl_resampler_destroy(state: *mut c_void);
        fn rpk_wdl_resampler_prepare(
            state: *mut c_void,
            request_frames: i32,
            input_buffer: *mut *mut f64,
        ) -> i32;
        fn rpk_wdl_resampler_out(
            state: *mut c_void,
            output: *mut f64,
            input_frames: i32,
            output_capacity_frames: i32,
        ) -> i32;
    }

    struct ResamplerGuard(*mut c_void);
    impl Drop for ResamplerGuard {
        fn drop(&mut self) {
            unsafe { rpk_wdl_resampler_destroy(self.0) };
        }
    }

    let expected = match expected_mode {
        ExpectedCount::SourceDomain => {
            if frames <= 1024 {
                return Ok(Vec::new());
            }
            Some((frames - 1024) / division as usize)
        }
        ExpectedCount::Exact(expected) => Some(expected),
        ExpectedCount::AnalysisDomain => None,
    };
    if expected == Some(0) || frames == 0 || channels == 0 {
        return Ok(Vec::new());
    }

    let state =
        unsafe { rpk_wdl_resampler_create(channels as i32, source_rate as f64, ANALYSIS_RATE) };
    if state.is_null() {
        return Ok(Vec::new());
    }
    let _guard = ResamplerGuard(state);

    let cap_frames =
        ((frames as f64 * ANALYSIS_RATE / source_rate as f64).ceil() as usize).saturating_add(4096);
    let block_frames = (2048 / channels).max(1);
    let mut scratch = vec![0.0f64; block_frames.saturating_mul(channels)];
    let mut analyzer = StreamingSpectralAnalyzer::new(channels, source_rate, division, expected);
    let mut in_pos = 0usize;
    let mut out_pos = 0usize;

    while in_pos < frames && out_pos < cap_frames {
        let request_frames = block_frames.min(frames - in_pos);
        let mut inbuf = std::ptr::null_mut();
        let wanted = unsafe { rpk_wdl_resampler_prepare(state, request_frames as i32, &mut inbuf) };
        if wanted <= 0 || inbuf.is_null() {
            return Ok(Vec::new());
        }
        let avail = (wanted as usize).min(frames - in_pos);
        if avail == 0 {
            return Ok(Vec::new());
        }

        let first_sample = in_pos.saturating_mul(channels);
        let sample_count = avail.saturating_mul(channels);
        for offset in 0..sample_count {
            unsafe {
                *inbuf.add(offset) = sample(first_sample + offset);
            }
        }

        let out_cap = block_frames.min(cap_frames - out_pos);
        if out_cap == 0 {
            return Ok(Vec::new());
        }
        let got = unsafe {
            rpk_wdl_resampler_out(state, scratch.as_mut_ptr(), avail as i32, out_cap as i32)
        };
        if got < 0 || got as usize > out_cap {
            return Ok(Vec::new());
        }

        let got = got as usize;
        if analyzer.push_interleaved(&scratch[..got.saturating_mul(channels)]) {
            break;
        }
        in_pos += avail;
        out_pos += got;
        if avail < wanted as usize {
            break;
        }
    }

    Ok(analyzer.into_output())
}

#[cfg(not(feature = "strict-wdl"))]
fn resample_to_analysis(
    input: &[f64],
    frames: usize,
    channels: usize,
    source_rate: u32,
) -> Vec<f64> {
    let ratio = source_rate as f64 / ANALYSIS_RATE;
    let mut src = input[..frames * channels].to_vec();
    apply_wdl_iir(&mut src, frames, channels, ratio);
    if (ratio - 1.0).abs() < f64::EPSILON {
        return src;
    }

    let mut out = Vec::with_capacity(((frames as f64 / ratio) as usize + 4) * channels);
    let mut p = 0.0f64;
    loop {
        let i = p as usize;
        if i + 1 >= frames {
            break;
        }
        let frac = p - i as f64;
        for c in 0..channels {
            let a = src[i * channels + c];
            let b = src[(i + 1) * channels + c];
            out.push(a * (1.0 - frac) + b * frac);
        }
        p += ratio;
    }
    out
}

#[inline]
fn fill_fft_input(
    ring: &[f32],
    write_pos: usize,
    channels: usize,
    channel: usize,
    window: &[f32],
) -> [f64; FFT_N] {
    let nwin = window.len();
    let mut fft_in = [0.0f64; FFT_N];
    if nwin == FFT_N {
        let mut i = 0usize;
        for rf in write_pos..FFT_N {
            let sample = ring[rf * channels + channel];
            // Preserve REAPER's scalar f32 multiply before promotion.
            let product = sample * window[i];
            fft_in[i] += product as f64;
            i += 1;
        }
        for rf in 0..write_pos {
            let sample = ring[rf * channels + channel];
            let product = sample * window[i];
            fft_in[i] += product as f64;
            i += 1;
        }
    } else {
        for i in 0..nwin {
            let rf = (write_pos + i) % nwin;
            let sample = ring[rf * channels + channel];
            let product = sample * window[i];
            fft_in[i & (FFT_N - 1)] += product as f64;
        }
    }
    fft_in
}

#[inline]
fn summarize_spectrum_magnitudes(
    spec: &[C64; HALF_BINS + 1],
) -> (f64, [f32; HALF_BINS + 1], usize) {
    let mut total = 0.0f64;
    let mut mags_f32 = [0.0f32; HALF_BINS + 1];
    let mut interior_kmax = 1usize;
    let mut interior_mmax = f64::NEG_INFINITY;
    let mut nyquist_magnitude = 0.0f64;

    for k in 0..=HALF_BINS {
        let m = if k == 0 || k == HALF_BINS {
            spec[k].re.abs()
        } else {
            (spec[k].re * spec[k].re + spec[k].im * spec[k].im).sqrt()
        };
        total += m;
        mags_f32[k] = m as f32;
        if k == HALF_BINS {
            nyquist_magnitude = m;
        } else if k != 0 && m > interior_mmax {
            interior_mmax = m;
            interior_kmax = k;
        }
    }

    let kmax = if interior_mmax > nyquist_magnitude {
        interior_kmax
    } else {
        HALF_BINS
    };
    (total, mags_f32, kmax)
}

fn analyze_channel(
    ring: &[f32],
    write_pos: usize,
    channels: usize,
    channel: usize,
    window: &[f32],
    previous: &[C32; HALF_BINS + 1],
    elapsed: usize,
) -> (SpectralPeak, [C32; HALF_BINS + 1]) {
    let mut fft_in = fill_fft_input(ring, write_pos, channels, channel, window);
    #[cfg(feature = "strict-wdl")]
    let spec = real_fft_1024(&mut fft_in);
    #[cfg(not(feature = "strict-wdl"))]
    let spec = real_fft_1024(&fft_in);

    // REAPER stores the current complex spectrum to its f32 phase-history
    // buffer before checking whether the magnitude sum is zero. Preserve that
    // ordering so a zero frame still resets the next frame's phase reference.
    let mut next = [C32::default(); HALF_BINS + 1];
    for k in 0..=HALF_BINS {
        next[k] = C32 {
            re: spec[k].re as f32,
            im: spec[k].im as f32,
        };
    }

    let (total, mags_f32, kmax) = summarize_spectrum_magnitudes(&spec);
    // REAPER's ordered floating-point branch proceeds only when total > 0.
    // This rejects NaN as well as zero/negative totals.  With very large but
    // finite f32 media, the f32 Hann multiply can produce Inf*0 -> NaN; REAPER
    // emits a zero spectral peak for those frames instead of a Nyquist/zero-
    // density placeholder.
    if total.partial_cmp(&0.0) != Some(std::cmp::Ordering::Greater) {
        return (SpectralPeak::default(), next);
    }

    let best_bin = if kmax == HALF_BINS || elapsed == 0 {
        HALF_BINS as f64
    } else {
        let cur = spec[kmax];
        let prev = previous[kmax];
        let phase_cur = cur.im.atan2(cur.re);
        let phase_prev = (prev.im as f64).atan2(prev.re as f64);
        let residual = wrap_phase(
            (phase_cur - phase_prev) / PI - 2.0 * (elapsed as f64 / FFT_N as f64) * kmax as f64,
        );
        kmax as f64 + HALF_BINS as f64 / elapsed as f64 * residual
    };

    let frequency_hz = (0.5 + best_bin * ANALYSIS_RATE / FFT_N as f64)
        .trunc()
        .clamp(0.0, 32767.0) as u16;

    // Exact formula recovered from REAPER 7.79 x86_64 disassembly. The total
    // magnitude remains f64, while each magnitude in the second moment is
    // explicitly rounded to f32 first.
    let mut spread = 0.0f64;
    for k in 0..=HALF_BINS {
        let d = k as f64 - best_bin;
        spread += mags_f32[k] as f64 * d * d;
    }
    let density = (0.5 + 16383.0 * (1.0 - 4.0 * spread / (total * 262144.0)))
        .trunc()
        .clamp(0.0, 16383.0) as u16;

    (
        SpectralPeak {
            frequency_hz,
            density,
        },
        next,
    )
}

fn analyze_resampled_spectral(
    resampled: &[f64],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
    expected_mode: ExpectedCount,
) -> Result<Vec<SpectralPeak>> {
    let expected_before_resample = match expected_mode {
        ExpectedCount::SourceDomain => {
            if frames <= 1024 {
                return Ok(Vec::new());
            }
            Some((frames - 1024) / division as usize)
        }
        #[cfg(feature = "strict-wdl")]
        ExpectedCount::Exact(expected) => Some(expected),
        #[cfg(feature = "strict-wdl")]
        ExpectedCount::AnalysisDomain => None,
    };
    if expected_before_resample == Some(0) {
        return Ok(Vec::new());
    }

    let out_frames = resampled.len() / channels;
    let expected = match expected_mode {
        #[cfg(feature = "strict-wdl")]
        ExpectedCount::AnalysisDomain => {
            analysis_domain_fine_count(out_frames, source_rate, division)
        }
        ExpectedCount::SourceDomain => {
            expected_before_resample.expect("expected count resolved before resampling")
        }
        #[cfg(feature = "strict-wdl")]
        ExpectedCount::Exact(_) => {
            expected_before_resample.expect("expected count resolved before resampling")
        }
    };
    if expected == 0 {
        return Ok(Vec::new());
    }

    let hop = division as f64 * ANALYSIS_RATE / source_rate as f64;
    let rounded = (hop + 0.5).floor() as i32;
    let nwin = rounded.max(1024) as usize;
    let mut phase = if rounded <= 1023 {
        (rounded - 1024) as f64 * 0.5
    } else {
        0.0
    };
    let window: Vec<f32> = if nwin <= 1 {
        vec![1.0]
    } else {
        // REAPER 7.79 stores only floor(N/2)+1 Hann coefficients. It
        // computes the angle increment once, advances the f64 phase with
        // repeated addition, converts each coefficient to f32, then reuses
        // the half table in reverse for the second half. This is subtly
        // different from evaluating cos(2*pi*i/(N-1)) independently.
        let half = nwin / 2;
        let step = 2.0 * PI / (nwin - 1) as f64;
        let mut table = Vec::with_capacity(half + 1);
        let mut angle = 0.0f64;
        for _ in 0..=half {
            table.push((0.5 - 0.5 * angle.cos()) as f32);
            angle += step;
        }
        (0..nwin)
            .map(|i| {
                let j = if i <= half { i } else { nwin - i };
                table[j]
            })
            .collect()
    };
    let mut ring = vec![0.0f32; nwin * channels];
    let mut write_pos = 0usize;
    let mut elapsed = 0usize;
    let mut previous = vec![[C32::default(); HALF_BINS + 1]; channels];
    let mut out = Vec::with_capacity(expected * channels);

    'samples: for f in 0..out_frames {
        for c in 0..channels {
            ring[write_pos * channels + c] = resampled[f * channels + c] as f32;
        }
        write_pos += 1;
        if write_pos == nwin {
            write_pos = 0;
        }
        phase += 1.0;
        elapsed += 1;
        while phase >= hop {
            for c in 0..channels {
                let (p, next) = analyze_channel(
                    &ring,
                    write_pos,
                    channels,
                    c,
                    &window,
                    &previous[c],
                    elapsed,
                );
                out.push(p);
                previous[c] = next;
            }
            elapsed = 0;
            phase -= hop;
            if out.len() / channels >= expected {
                break 'samples;
            }
        }
    }
    out.truncate(expected * channels);
    Ok(out)
}

fn build_fine_spectral_f64_impl(
    source: &[f64],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
    expected_mode: ExpectedCount,
) -> Result<Vec<SpectralPeak>> {
    if channels == 0 {
        return Err(ReaPeaksError::InvalidArgument("channels=0"));
    }
    if division == 0 || source_rate == 0 {
        return Err(ReaPeaksError::InvalidArgument("zero rate/division"));
    }
    if source.len() < frames.saturating_mul(channels) {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }

    if matches!(expected_mode, ExpectedCount::SourceDomain) && frames <= 1024 {
        return Ok(Vec::new());
    }
    #[cfg(feature = "strict-wdl")]
    if matches!(expected_mode, ExpectedCount::Exact(0)) {
        return Ok(Vec::new());
    }

    let resampled = resample_to_analysis(source, frames, channels, source_rate);
    analyze_resampled_spectral(
        &resampled,
        frames,
        channels,
        source_rate,
        division,
        expected_mode,
    )
}

fn source_from_i16(pcm: &[i16], frames: usize, channels: usize) -> Result<Vec<f64>> {
    Ok(pcm
        .get(..frames.saturating_mul(channels))
        .ok_or(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ))?
        .iter()
        .map(|&v| v as f64 / 32768.0)
        .collect())
}

fn source_from_f32(pcm: &[f32], frames: usize, channels: usize) -> Result<Vec<f64>> {
    source_from_f32_source(pcm, frames, channels)
}

fn source_from_f32_source<S: F32SampleSource + ?Sized>(
    pcm: &S,
    frames: usize,
    channels: usize,
) -> Result<Vec<f64>> {
    let required = frames
        .checked_mul(channels)
        .ok_or(ReaPeaksError::InvalidArgument("frames*channels overflow"))?;
    if pcm.sample_len() < required {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }
    Ok((0..required)
        .map(|index| f64::from(pcm.sample_f32(index)))
        .collect())
}

pub fn build_fine_spectral(
    pcm: &[i16],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<Vec<SpectralPeak>> {
    let source = source_from_i16(pcm, frames, channels)?;
    build_fine_spectral_f64_impl(
        &source,
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::SourceDomain,
    )
}

pub fn build_fine_spectral_f32(
    pcm: &[f32],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<Vec<SpectralPeak>> {
    let source = source_from_f32(pcm, frames, channels)?;
    build_fine_spectral_f64_impl(
        &source,
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::SourceDomain,
    )
}

pub(crate) fn build_fine_spectral_f32_source<S: F32SampleSource + ?Sized>(
    pcm: &S,
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<Vec<SpectralPeak>> {
    let source = source_from_f32_source(pcm, frames, channels)?;
    build_fine_spectral_f64_impl(
        &source,
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::SourceDomain,
    )
}

#[cfg(feature = "strict-wdl")]
pub(crate) fn build_fine_spectral_analysis_counted(
    pcm: &[i16],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<Vec<SpectralPeak>> {
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
    analyze_streaming_samples(
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::AnalysisDomain,
        |index| pcm[index] as f64 / 32768.0,
    )
}

#[cfg(feature = "strict-wdl")]
pub(crate) fn build_fine_spectral_f32_analysis_counted(
    pcm: &[f32],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<Vec<SpectralPeak>> {
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
    analyze_streaming_samples(
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::AnalysisDomain,
        |index| f64::from(pcm[index]),
    )
}

#[cfg(feature = "strict-wdl")]
pub(crate) fn build_fine_spectral_f32_source_analysis_counted<S: F32SampleSource + ?Sized>(
    pcm: &S,
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
) -> Result<Vec<SpectralPeak>> {
    let required = frames
        .checked_mul(channels)
        .ok_or(ReaPeaksError::InvalidArgument("frames*channels overflow"))?;
    if channels == 0 {
        return Err(ReaPeaksError::InvalidArgument("channels=0"));
    }
    if division == 0 || source_rate == 0 {
        return Err(ReaPeaksError::InvalidArgument("zero rate/division"));
    }
    if pcm.sample_len() < required {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }
    analyze_streaming_samples(
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::AnalysisDomain,
        |index| f64::from(pcm.sample_f32(index)),
    )
}

#[cfg(feature = "strict-wdl")]
pub(crate) fn build_fine_spectral_with_expected(
    pcm: &[i16],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
    expected: usize,
) -> Result<Vec<SpectralPeak>> {
    if expected == 0 {
        return Ok(Vec::new());
    }
    if channels == 0 {
        return Err(ReaPeaksError::InvalidArgument("channels=0"));
    }
    if pcm.len() < frames.saturating_mul(channels) {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }
    if division == 0 || source_rate == 0 {
        return Err(ReaPeaksError::InvalidArgument("zero rate/division"));
    }
    analyze_streaming_samples(
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::Exact(expected),
        |index| pcm[index] as f64 / 32768.0,
    )
}

#[cfg(feature = "strict-wdl")]
pub(crate) fn build_fine_spectral_f32_with_expected(
    pcm: &[f32],
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
    expected: usize,
) -> Result<Vec<SpectralPeak>> {
    if expected == 0 {
        return Ok(Vec::new());
    }
    if channels == 0 {
        return Err(ReaPeaksError::InvalidArgument("channels=0"));
    }
    if pcm.len() < frames.saturating_mul(channels) {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }
    if division == 0 || source_rate == 0 {
        return Err(ReaPeaksError::InvalidArgument("zero rate/division"));
    }
    analyze_streaming_samples(
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::Exact(expected),
        |index| f64::from(pcm[index]),
    )
}

#[cfg(feature = "strict-wdl")]
pub(crate) fn build_fine_spectral_f32_source_with_expected<S: F32SampleSource + ?Sized>(
    pcm: &S,
    frames: usize,
    channels: usize,
    source_rate: u32,
    division: u32,
    expected: usize,
) -> Result<Vec<SpectralPeak>> {
    if expected == 0 {
        return Ok(Vec::new());
    }
    let required = frames
        .checked_mul(channels)
        .ok_or(ReaPeaksError::InvalidArgument("frames*channels overflow"))?;
    if channels == 0 {
        return Err(ReaPeaksError::InvalidArgument("channels=0"));
    }
    if pcm.sample_len() < required {
        return Err(ReaPeaksError::InvalidArgument(
            "PCM buffer shorter than frames*channels",
        ));
    }
    if division == 0 || source_rate == 0 {
        return Err(ReaPeaksError::InvalidArgument("zero rate/division"));
    }
    analyze_streaming_samples(
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::Exact(expected),
        |index| f64::from(pcm.sample_f32(index)),
    )
}

/// REAPER 7.79 aggregation observed for spectral mipmaps: each coarser output
/// is formed directly from the fine spectral level. density=floor(mean), while
/// frequency is taken from the fine peak maximizing density*(32768-frequency).
/// This matched every tested mid/coarse point in the research corpus.
pub fn aggregate_spectral_from_fine(
    fine: &[SpectralPeak],
    channels: usize,
    ratio: usize,
    output_count: usize,
) -> Vec<SpectralPeak> {
    if channels == 0 || ratio == 0 {
        return Vec::new();
    }
    let fine_n = fine.len() / channels;
    let n = output_count.min(fine_n / ratio);
    let mut out = Vec::with_capacity(n * channels);
    for p in 0..n {
        let a = p * ratio;
        let b = a + ratio;
        for c in 0..channels {
            let mut sum_density = 0u64;
            let mut best = SpectralPeak::default();
            let mut best_score = 0u64;
            for i in a..b {
                let q = fine[i * channels + c];
                sum_density += q.density as u64;
                let score = q.density as u64 * (32768u64 - q.frequency_hz as u64);
                if score > best_score {
                    best_score = score;
                    best = q;
                }
            }
            out.push(SpectralPeak {
                frequency_hz: best.frequency_hz,
                density: (sum_density / ratio as u64).min(16383) as u16,
            });
        }
    }
    out
}

pub(crate) fn encode_spectral_layer_from_fine(
    fine: &[SpectralPeak],
    channels: usize,
    ratio: usize,
    output_count: usize,
) -> GeneratedLayer {
    if channels == 0 || ratio == 0 {
        return GeneratedLayer {
            header: LayerHeader {
                division: TOKEN_SPECTRAL,
                peak_count: 0,
            },
            bytes: Vec::new(),
        };
    }
    let fine_count = fine.len() / channels;
    let count = output_count.min(fine_count / ratio);
    let mut bytes = Vec::with_capacity(
        count
            .saturating_mul(channels)
            .saturating_mul(std::mem::size_of::<u32>()),
    );

    if ratio == 1 {
        for peak in &fine[..count * channels] {
            bytes.extend_from_slice(&peak.code().to_le_bytes());
        }
    } else {
        for output_index in 0..count {
            let first = output_index * ratio;
            let last = first + ratio;
            for channel in 0..channels {
                let mut sum_density = 0u64;
                let mut best = SpectralPeak::default();
                let mut best_score = 0u64;
                for fine_index in first..last {
                    let peak = fine[fine_index * channels + channel];
                    sum_density += u64::from(peak.density);
                    let score = u64::from(peak.density) * (32768u64 - u64::from(peak.frequency_hz));
                    if score > best_score {
                        best_score = score;
                        best = peak;
                    }
                }
                let peak = SpectralPeak {
                    frequency_hz: best.frequency_hz,
                    density: (sum_density / ratio as u64).min(16383) as u16,
                };
                bytes.extend_from_slice(&peak.code().to_le_bytes());
            }
        }
    }

    GeneratedLayer {
        header: LayerHeader {
            division: TOKEN_SPECTRAL,
            peak_count: count as u32,
        },
        bytes,
    }
}

fn assemble_spectral_layers(
    fine: &[SpectralPeak],
    frames: usize,
    channels: usize,
    divisions: &[u32],
) -> Result<Vec<GeneratedLayer>> {
    if divisions.is_empty() {
        return Ok(Vec::new());
    }
    let fine_div = divisions[0];
    let mut out = Vec::with_capacity(divisions.len());

    for &div in divisions {
        if div == 0 || div % fine_div != 0 {
            return Err(ReaPeaksError::Unsupported(
                "spectral divisions must be nonzero multiples of fine division",
            ));
        }
        let ratio = (div / fine_div) as usize;
        let expected = frames.saturating_sub(1024) / div as usize;
        out.push(encode_spectral_layer_from_fine(
            fine, channels, ratio, expected,
        ));
    }
    Ok(out)
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
    let fine = build_fine_spectral(pcm, frames, channels, source_rate, divisions[0])?;
    assemble_spectral_layers(&fine, frames, channels, divisions)
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
    let fine = build_fine_spectral_f32(pcm, frames, channels, source_rate, divisions[0])?;
    assemble_spectral_layers(&fine, frames, channels, divisions)
}

pub(crate) fn build_spectral_layers_f32_source<S: F32SampleSource + ?Sized>(
    pcm: &S,
    frames: usize,
    channels: usize,
    source_rate: u32,
    divisions: &[u32],
) -> Result<Vec<GeneratedLayer>> {
    if divisions.is_empty() {
        return Ok(Vec::new());
    }
    let fine = build_fine_spectral_f32_source(pcm, frames, channels, source_rate, divisions[0])?;
    assemble_spectral_layers(&fine, frames, channels, divisions)
}

#[cfg(all(test, feature = "strict-wdl"))]
mod strict_streaming_resample_tests {
    use super::*;

    fn assert_same_f64_stream(left: &[f64], right: &[f64]) {
        assert_eq!(left.len(), right.len());
        for (index, (&a, &b)) in left.iter().zip(right).enumerate() {
            assert_eq!(a.to_bits(), b.to_bits(), "resampled sample {index}");
        }
    }

    #[test]
    fn direct_i16_feed_matches_whole_file_f64_staging() {
        for &(rate, channels) in &[(44_100u32, 1usize), (48_000, 2), (96_000, 6)] {
            let frames = 5_137usize;
            let pcm: Vec<i16> = (0..frames * channels)
                .map(|i| ((i as i32 * 7_919 + 12_347) as i16).wrapping_sub(8_123))
                .collect();
            let staged_source = source_from_i16(&pcm, frames, channels).unwrap();
            let staged = resample_to_analysis(&staged_source, frames, channels, rate);
            let direct = resample_i16_to_analysis(&pcm, frames, channels, rate);
            assert_same_f64_stream(&staged, &direct);
        }
    }

    #[test]
    fn direct_f32_source_feed_matches_whole_file_f64_staging() {
        for &(rate, channels) in &[(44_100u32, 2usize), (88_200, 1), (192_000, 4)] {
            let frames = 4_321usize;
            let pcm: Vec<f32> = (0..frames * channels)
                .map(|i| (((i * 37) % 20_003) as f32 - 10_001.0) / 10_003.0)
                .collect();
            let staged_source = source_from_f32(&pcm, frames, channels).unwrap();
            let staged = resample_to_analysis(&staged_source, frames, channels, rate);
            let direct = resample_f32_source_to_analysis(&pcm[..], frames, channels, rate);
            assert_same_f64_stream(&staged, &direct);
        }
    }
}

#[cfg(all(test, feature = "strict-wdl"))]
mod strict_streaming_analysis_tests {
    use super::*;

    #[test]
    fn streaming_analysis_matches_staged_analysis() {
        for &(rate, channels, frames) in &[
            (44_100u32, 1usize, 9_731usize),
            (48_000, 2, 12_345),
            (96_000, 6, 8_123),
        ] {
            let division = (rate / 300).max(1);
            let pcm: Vec<i16> = (0..frames * channels)
                .map(|i| ((i as i32 * 7_919 + 12_347) as i16).wrapping_sub(8_123))
                .collect();
            let staged_resampled = resample_i16_to_analysis(&pcm, frames, channels, rate);
            let staged = analyze_resampled_spectral(
                &staged_resampled,
                frames,
                channels,
                rate,
                division,
                ExpectedCount::AnalysisDomain,
            )
            .unwrap();
            let streamed = analyze_streaming_samples(
                frames,
                channels,
                rate,
                division,
                ExpectedCount::AnalysisDomain,
                |index| pcm[index] as f64 / 32768.0,
            )
            .unwrap();
            assert_eq!(streamed, staged, "rate={rate} channels={channels}");

            let exact = (staged.len() / channels).saturating_sub(1);
            let staged_exact = analyze_resampled_spectral(
                &staged_resampled,
                frames,
                channels,
                rate,
                division,
                ExpectedCount::Exact(exact),
            )
            .unwrap();
            let streamed_exact = analyze_streaming_samples(
                frames,
                channels,
                rate,
                division,
                ExpectedCount::Exact(exact),
                |index| pcm[index] as f64 / 32768.0,
            )
            .unwrap();
            assert_eq!(streamed_exact, staged_exact);
        }
    }
}

#[cfg(test)]
mod spectral_fft_input_fast_path_tests {
    use super::*;

    fn reference_fft_input(
        ring: &[f32],
        write_pos: usize,
        channels: usize,
        channel: usize,
        window: &[f32],
    ) -> [f64; FFT_N] {
        let nwin = window.len();
        let mut fft_in = [0.0f64; FFT_N];
        for i in 0..nwin {
            let rf = (write_pos + i) % nwin;
            let sample = ring[rf * channels + channel];
            let product = sample * window[i];
            fft_in[i & (FFT_N - 1)] += product as f64;
        }
        fft_in
    }

    #[test]
    fn modulo_free_1024_gather_is_bit_identical() {
        let channels = 6usize;
        let ring: Vec<f32> = (0..FFT_N * channels)
            .map(|i| (((i * 37) % 20_003) as f32 - 10_001.0) / 10_003.0)
            .collect();
        let window: Vec<f32> = (0..FFT_N)
            .map(|i| (((i * 53) % 4_093) as f32 + 1.0) / 4_096.0)
            .collect();
        for write_pos in [0usize, 1, 127, 511, 512, 777, 1023] {
            for channel in 0..channels {
                let fast = fill_fft_input(&ring, write_pos, channels, channel, &window);
                let reference = reference_fft_input(&ring, write_pos, channels, channel, &window);
                for (index, (&actual, &expected)) in fast.iter().zip(reference.iter()).enumerate() {
                    assert_eq!(
                        actual.to_bits(),
                        expected.to_bits(),
                        "write_pos={write_pos} channel={channel} bin={index}"
                    );
                }
            }
        }
    }
}

#[cfg(test)]
mod spectral_magnitude_summary_tests {
    use super::*;

    fn reference_summary(spec: &[C64; HALF_BINS + 1]) -> (f64, [f32; HALF_BINS + 1], usize) {
        let mut mags = [0.0f64; HALF_BINS + 1];
        let mut mags_f32 = [0.0f32; HALF_BINS + 1];
        for k in 0..=HALF_BINS {
            let m = if k == 0 || k == HALF_BINS {
                spec[k].re.abs()
            } else {
                (spec[k].re * spec[k].re + spec[k].im * spec[k].im).sqrt()
            };
            mags[k] = m;
            mags_f32[k] = m as f32;
        }
        let total: f64 = mags.iter().sum();
        let mut kmax = HALF_BINS;
        let mut mmax = mags[HALF_BINS];
        for k in 1..HALF_BINS {
            if mags[k] > mmax {
                mmax = mags[k];
                kmax = k;
            }
        }
        (total, mags_f32, kmax)
    }

    fn assert_same(spec: &[C64; HALF_BINS + 1]) {
        let (actual_total, actual_f32, actual_kmax) = summarize_spectrum_magnitudes(spec);
        let (expected_total, expected_f32, expected_kmax) = reference_summary(spec);
        assert_eq!(actual_total.to_bits(), expected_total.to_bits());
        assert_eq!(actual_kmax, expected_kmax);
        for (index, (&actual, &expected)) in actual_f32.iter().zip(expected_f32.iter()).enumerate()
        {
            assert_eq!(
                actual.to_bits(),
                expected.to_bits(),
                "magnitude bin={index}"
            );
        }
    }

    #[test]
    fn compact_summary_matches_reference_bits() {
        let mut spec = [C64::default(); HALF_BINS + 1];
        for (k, value) in spec.iter_mut().enumerate() {
            value.re = ((k * 37 + 11) as f64).sin() * (1.0 + k as f64 / 17.0);
            value.im = ((k * 53 + 7) as f64).cos() * (0.5 + k as f64 / 31.0);
        }
        assert_same(&spec);

        spec.fill(C64::default());
        spec[1].re = 3.0;
        spec[2].re = 3.0;
        spec[HALF_BINS].re = 2.0;
        assert_same(&spec);

        spec[17].re = f64::NAN;
        spec[31].im = f64::INFINITY;
        assert_same(&spec);
    }
}

#[cfg(test)]
mod spectral_direct_encode_tests {
    use super::*;

    fn reference_layer(
        fine: &[SpectralPeak],
        channels: usize,
        ratio: usize,
        output_count: usize,
    ) -> GeneratedLayer {
        let fine_count = fine.len() / channels;
        let count = output_count.min(fine_count / ratio);
        let peaks = if ratio == 1 {
            fine[..count * channels].to_vec()
        } else {
            aggregate_spectral_from_fine(fine, channels, ratio, count)
        };
        let mut bytes = Vec::with_capacity(peaks.len() * 4);
        for peak in &peaks {
            bytes.extend_from_slice(&peak.code().to_le_bytes());
        }
        GeneratedLayer {
            header: LayerHeader {
                division: TOKEN_SPECTRAL,
                peak_count: (peaks.len() / channels) as u32,
            },
            bytes,
        }
    }

    #[test]
    fn direct_encode_matches_aggregate_then_encode() {
        let channels = 2usize;
        let frames = 60usize;
        let fine: Vec<SpectralPeak> = (0..frames * channels)
            .map(|index| SpectralPeak {
                frequency_hz: ((index * 977 + 31) % 20_000) as u16,
                density: ((index * 613 + 17) % 16_384) as u16,
            })
            .collect();
        for &(ratio, output_count) in &[(1usize, 57usize), (3, 19), (5, 11), (15, 4)] {
            let actual = encode_spectral_layer_from_fine(&fine, channels, ratio, output_count);
            let expected = reference_layer(&fine, channels, ratio, output_count);
            assert_eq!(actual.header.division, expected.header.division);
            assert_eq!(actual.header.peak_count, expected.header.peak_count);
            assert_eq!(actual.bytes, expected.bytes, "ratio={ratio}");
        }
    }
}
