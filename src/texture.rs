use crate::format::SpectralPeak;
use crate::pyramid::{WavePyramid, WaveTile};
use crate::wave::{decode_peak_code, PeakPair};

#[derive(Debug, Clone)]
pub struct RgbaImage {
    pub width: usize,
    pub height: usize,
    pub stride: usize,
    pub data: Vec<u8>,
}

/// Lossless packed waveform data texture.
///
/// Layout is row-major with one row per channel and one texel per peak:
///   R,G = max i16 little-endian
///   B,A = min i16 little-endian
///
/// This can be uploaded directly as RGBA8 to Qt QRhi/OpenGL, WebGL2 or WebGPU.
pub fn encode_envelope_rgba8(peaks: &[PeakPair], channels: usize) -> RgbaImage {
    let width = if channels == 0 {
        0
    } else {
        peaks.len() / channels
    };
    let height = channels;
    let mut data = vec![0u8; width.saturating_mul(height).saturating_mul(4)];
    for x in 0..width {
        for c in 0..channels {
            let q = peaks[x * channels + c];
            let mx = q.max.to_le_bytes();
            let mn = q.min.to_le_bytes();
            let o = (c * width + x) * 4;
            data[o] = mx[0];
            data[o + 1] = mx[1];
            data[o + 2] = mn[0];
            data[o + 3] = mn[1];
        }
    }
    RgbaImage {
        width,
        height,
        stride: width * 4,
        data,
    }
}

pub fn encode_wave_tile_rgba8(tile: &WaveTile, channels: usize) -> RgbaImage {
    encode_envelope_rgba8(&tile.peaks, channels)
}

/// Lossless packed spectral-peak texture.
/// One RGBA8 texel is exactly the little-endian 32-bit REAPER spectral code:
/// low 15 bits frequency in Hz, next 14 bits density/tonality.
pub fn encode_spectral_rgba8(peaks: &[SpectralPeak], channels: usize) -> RgbaImage {
    let width = if channels == 0 {
        0
    } else {
        peaks.len() / channels
    };
    let height = channels;
    let mut data = vec![0u8; width.saturating_mul(height).saturating_mul(4)];
    for x in 0..width {
        for c in 0..channels {
            let code = peaks[x * channels + c].code().to_le_bytes();
            let o = (c * width + x) * 4;
            data[o..o + 4].copy_from_slice(&code);
        }
    }
    RgbaImage {
        width,
        height,
        stride: width * 4,
        data,
    }
}

#[inline]
fn put_rgba(buf: &mut [u8], width: usize, x: usize, y: usize, color: [u8; 4]) {
    let o = (y * width + x) * 4;
    if o + 4 <= buf.len() {
        buf[o..o + 4].copy_from_slice(&color);
    }
}

pub fn render_waveform_rgba8(
    pyramid: &WavePyramid,
    width: usize,
    height: usize,
    start_frame: u64,
    end_frame: u64,
    background: [u8; 4],
    waveform: [u8; 4],
) -> RgbaImage {
    render_waveform_rgba8_scaled(
        pyramid,
        width,
        height,
        start_frame,
        end_frame,
        1.0,
        background,
        waveform,
    )
}

/// CPU fallback renderer producing byte-ordered RGBA8 suitable for
/// QImage::Format_RGBA8888 or browser ImageData.
///
/// `vertical_full_scale` is the amplitude mapped to 47% of each channel band.
/// Use 1.0 for conventional 0 dBFS display; values >1 in RPKL then clip at the
/// band edge. Set it larger (e.g. 4.0) to inspect over-range floating media.
pub fn render_waveform_rgba8_scaled(
    pyramid: &WavePyramid,
    width: usize,
    height: usize,
    start_frame: u64,
    end_frame: u64,
    vertical_full_scale: f32,
    background: [u8; 4],
    waveform: [u8; 4],
) -> RgbaImage {
    let mut data = vec![0u8; width.saturating_mul(height).saturating_mul(4)];
    for px in data.chunks_exact_mut(4) {
        px.copy_from_slice(&background);
    }
    if width == 0
        || height == 0
        || pyramid.channels == 0
        || end_frame <= start_frame
        || !vertical_full_scale.is_finite()
        || vertical_full_scale <= 0.0
    {
        return RgbaImage {
            width,
            height,
            stride: width * 4,
            data,
        };
    }
    let Some(plan) = pyramid.choose_level(start_frame, end_frame, width) else {
        return RgbaImage {
            width,
            height,
            stride: width * 4,
            data,
        };
    };
    let Some(peaks) = pyramid.read_plan(plan) else {
        return RgbaImage {
            width,
            height,
            stride: width * 4,
            data,
        };
    };
    let band_h = height as f64 / pyramid.channels as f64;
    let span = (end_frame - start_frame) as f64;

    for x in 0..width {
        let f0 = start_frame as f64 + span * x as f64 / width as f64;
        let f1 = start_frame as f64 + span * (x + 1) as f64 / width as f64;
        let abs0 = (f0 / plan.division as f64).floor().max(0.0) as usize;
        let abs1 = (f1 / plan.division as f64).ceil().max((abs0 + 1) as f64) as usize;
        let a = abs0.max(plan.first_peak);
        let b = abs1.min(plan.first_peak + plan.peak_count);
        if a >= b {
            continue;
        }
        let local_a = a - plan.first_peak;
        let local_b = b - plan.first_peak;
        for c in 0..pyramid.channels {
            let mut mx = i16::MIN;
            let mut mn = i16::MAX;
            for p in local_a..local_b {
                let q = peaks[p * pyramid.channels + c];
                mx = mx.max(q.max);
                mn = mn.min(q.min);
            }
            let amp_max = decode_peak_code(pyramid.encoding, mx) as f64;
            let amp_min = decode_peak_code(pyramid.encoding, mn) as f64;
            let center = band_h * (c as f64 + 0.5);
            let scale = band_h * 0.47 / vertical_full_scale as f64;
            let mut y_top = (center - amp_max * scale).round() as isize;
            let mut y_bot = (center - amp_min * scale).round() as isize;
            if y_top > y_bot {
                std::mem::swap(&mut y_top, &mut y_bot);
            }
            y_top = y_top.clamp(0, height as isize - 1);
            y_bot = y_bot.clamp(0, height as isize - 1);
            for y in y_top..=y_bot {
                put_rgba(&mut data, width, x, y as usize, waveform);
            }
        }
    }
    RgbaImage {
        width,
        height,
        stride: width * 4,
        data,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn envelope_texture_is_lossless_le() {
        let p = [PeakPair {
            max: 0x1234,
            min: -2,
        }];
        let x = encode_envelope_rgba8(&p, 1);
        assert_eq!(x.width, 1);
        assert_eq!(x.height, 1);
        assert_eq!(x.data, vec![0x34, 0x12, 0xfe, 0xff]);
    }

    #[test]
    fn spectral_texture_is_raw_code_le() {
        let p = [SpectralPeak {
            frequency_hz: 1000,
            density: 12345,
        }];
        let x = encode_spectral_rgba8(&p, 1);
        assert_eq!(x.data, p[0].code().to_le_bytes());
    }
}
