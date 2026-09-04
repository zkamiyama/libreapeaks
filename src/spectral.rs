use crate::error::{ReaPeaksError, Result};
use crate::format::{GeneratedLayer, LayerHeader, SpectralPeak, TOKEN_SPECTRAL};
use crate::sample_source::F32SampleSource;
use std::f64::consts::PI;

const ANALYSIS_RATE: f64 = 22_050.0;
const FFT_N: usize = 1024;
const HALF_BINS: usize = 512;

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
    Exact(usize),
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

#[inline]
fn analysis_domain_fine_count(
    analysis_frames: usize,
    source_rate: u32,
    division: u32,
) -> usize {
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
fn real_fft_1024(input: &[f64; FFT_N]) -> [C64; HALF_BINS + 1] {
    unsafe extern "C" {
        fn rpk_wdl_real_fft_1024(input: *const f64, out_re: *mut f64, out_im: *mut f64) -> i32;
    }
    let mut re = [0.0f64; HALF_BINS + 1];
    let mut im = [0.0f64; HALF_BINS + 1];
    let rc = unsafe { rpk_wdl_real_fft_1024(input.as_ptr(), re.as_mut_ptr(), im.as_mut_ptr()) };
    assert_eq!(rc, 0, "WDL FFT bridge failed");
    let mut out = [C64::default(); HALF_BINS + 1];
    for k in 0..=HALF_BINS {
        out[k] = C64 {
            re: re[k],
            im: im[k],
        };
    }
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

fn analyze_channel(
    ring: &[f32],
    write_pos: usize,
    channels: usize,
    channel: usize,
    window: &[f32],
    previous: &[C32; HALF_BINS + 1],
    elapsed: usize,
) -> (SpectralPeak, [C32; HALF_BINS + 1]) {
    let nwin = window.len();
    let mut fft_in = [0.0f64; FFT_N];
    for i in 0..nwin {
        let rf = (write_pos + i) % nwin;
        let sample = ring[rf * channels + channel];
        // REAPER 7.79 disassembly uses a scalar f32 multiply here, then
        // promotes the result to double before accumulating the FFT input.
        let product = sample * window[i];
        fft_in[i & (FFT_N - 1)] += product as f64;
    }
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
    // REAPER's ordered floating-point branch proceeds only when total > 0.
    // This rejects NaN as well as zero/negative totals.  With very large but
    // finite f32 media, the f32 Hann multiply can produce Inf*0 -> NaN; REAPER
    // emits a zero spectral peak for those frames instead of a Nyquist/zero-
    // density placeholder.
    if total.partial_cmp(&0.0) != Some(std::cmp::Ordering::Greater) {
        return (SpectralPeak::default(), next);
    }

    // REAPER initializes the candidate to Nyquist and scans bins 1..511.
    // DC participates in density but not dominant-frequency selection.
    let mut kmax = HALF_BINS;
    let mut mmax = mags[HALF_BINS];
    for k in 1..HALF_BINS {
        if mags[k] > mmax {
            mmax = mags[k];
            kmax = k;
        }
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

    // Portable mode retains the historical source-domain rule. Strict mode can
    // either accept an explicitly supplied oracle count or derive the count from
    // the real WDL analysis stream produced below. Deriving it from that stream
    // avoids the former zero-signal WDL resampler prepass without changing any
    // DSP operation or EOF scheduler rule.
    let expected_before_resample = match expected_mode {
        ExpectedCount::SourceDomain => {
            if frames <= 1024 {
                return Ok(Vec::new());
            }
            Some((frames - 1024) / division as usize)
        }
        ExpectedCount::Exact(expected) => Some(expected),
        ExpectedCount::AnalysisDomain => None,
    };
    if expected_before_resample == Some(0) {
        return Ok(Vec::new());
    }

    let resampled = resample_to_analysis(source, frames, channels, source_rate);
    let out_frames = resampled.len() / channels;
    let expected = match expected_mode {
        ExpectedCount::AnalysisDomain => {
            analysis_domain_fine_count(out_frames, source_rate, division)
        }
        ExpectedCount::SourceDomain | ExpectedCount::Exact(_) => {
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
    let source = source_from_i16(pcm, frames, channels)?;
    build_fine_spectral_f64_impl(
        &source,
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::AnalysisDomain,
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
    let source = source_from_f32(pcm, frames, channels)?;
    build_fine_spectral_f64_impl(
        &source,
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::AnalysisDomain,
    )
}

#[cfg(feature = "strict-wdl")]
pub(crate) fn build_fine_spectral_f32_source_analysis_counted<
    S: F32SampleSource + ?Sized,
>(
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
        ExpectedCount::AnalysisDomain,
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
    let source = source_from_i16(pcm, frames, channels)?;
    build_fine_spectral_f64_impl(
        &source,
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::Exact(expected),
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
    let source = source_from_f32(pcm, frames, channels)?;
    build_fine_spectral_f64_impl(
        &source,
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::Exact(expected),
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
    let source = source_from_f32_source(pcm, frames, channels)?;
    build_fine_spectral_f64_impl(
        &source,
        frames,
        channels,
        source_rate,
        division,
        ExpectedCount::Exact(expected),
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
    let fine_count = if channels == 0 {
        0
    } else {
        fine.len() / channels
    };
    let mut out = Vec::with_capacity(divisions.len());

    for (li, &div) in divisions.iter().enumerate() {
        if div == 0 || div % fine_div != 0 {
            return Err(ReaPeaksError::Unsupported(
                "spectral divisions must be nonzero multiples of fine division",
            ));
        }
        let expected = frames.saturating_sub(1024) / div as usize;
        let peaks = if li == 0 {
            fine[..expected.min(fine_count) * channels].to_vec()
        } else {
            aggregate_spectral_from_fine(fine, channels, (div / fine_div) as usize, expected)
        };
        let mut bytes = Vec::with_capacity(peaks.len() * 4);
        for p in &peaks {
            bytes.extend_from_slice(&p.code().to_le_bytes());
        }
        out.push(GeneratedLayer {
            header: LayerHeader {
                division: TOKEN_SPECTRAL,
                peak_count: (peaks.len() / channels) as u32,
            },
            bytes,
        });
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
