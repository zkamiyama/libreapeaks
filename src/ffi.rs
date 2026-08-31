use crate::format::ReaPeaks;
use crate::generate::{generate_f32, generate_pcm16, GenerateOptions};
use crate::pyramid::{WavePyramid, WaveTileKey};
use crate::texture::{
    encode_envelope_rgba8, encode_spectral_rgba8, encode_wave_tile_rgba8,
    render_waveform_rgba8_scaled,
};
use std::ffi::{c_char, CStr};
use std::ptr;

#[repr(C)]
pub struct RpkBuffer {
    pub data: *mut u8,
    pub len: usize,
    pub capacity: usize,
}

impl RpkBuffer {
    fn empty() -> Self {
        Self {
            data: ptr::null_mut(),
            len: 0,
            capacity: 0,
        }
    }
    fn from_vec(mut v: Vec<u8>) -> Self {
        let out = Self {
            data: v.as_mut_ptr(),
            len: v.len(),
            capacity: v.capacity(),
        };
        std::mem::forget(v);
        out
    }
}

pub struct RpkHandle {
    pub file: ReaPeaks,
    pub pyramid: WavePyramid,
}

#[repr(C)]
pub struct RpkLevelInfo {
    pub division: u64,
    pub peak_count: usize,
    pub native: u8,
}

#[repr(C)]
pub struct RpkViewPlan {
    pub level_index: usize,
    pub division: u64,
    pub first_peak: usize,
    pub peak_count: usize,
    pub peaks_per_pixel: f64,
}

#[no_mangle]
pub unsafe extern "C" fn rpk_open(path: *const c_char, out: *mut *mut RpkHandle) -> i32 {
    if out.is_null() {
        return -1;
    }
    *out = ptr::null_mut();
    if path.is_null() {
        return -1;
    }
    let Ok(s) = CStr::from_ptr(path).to_str() else {
        return -2;
    };
    let Ok(file) = ReaPeaks::open(s) else {
        return -3;
    };
    let pyramid = WavePyramid::from_reapeaks(&file, 4);
    *out = Box::into_raw(Box::new(RpkHandle { file, pyramid }));
    0
}

#[no_mangle]
pub unsafe extern "C" fn rpk_close(h: *mut RpkHandle) {
    if !h.is_null() {
        drop(Box::from_raw(h));
    }
}

#[no_mangle]
pub unsafe extern "C" fn rpk_wave_encoding(h: *const RpkHandle) -> u8 {
    h.as_ref().map(|x| x.pyramid.encoding as u8).unwrap_or(0)
}

#[no_mangle]
pub unsafe extern "C" fn rpk_level_count(h: *const RpkHandle) -> usize {
    h.as_ref().map(|x| x.pyramid.levels.len()).unwrap_or(0)
}

#[no_mangle]
pub unsafe extern "C" fn rpk_get_level_info(
    h: *const RpkHandle,
    index: usize,
    out: *mut RpkLevelInfo,
) -> i32 {
    let (Some(h), Some(out)) = (h.as_ref(), out.as_mut()) else {
        return -1;
    };
    let Some(l) = h.pyramid.levels.get(index) else {
        return -2;
    };
    *out = RpkLevelInfo {
        division: l.division,
        peak_count: l.peak_count,
        native: l.native as u8,
    };
    0
}

#[no_mangle]
pub unsafe extern "C" fn rpk_plan_view(
    h: *const RpkHandle,
    start: u64,
    end: u64,
    pixels: usize,
    out: *mut RpkViewPlan,
) -> i32 {
    let (Some(h), Some(out)) = (h.as_ref(), out.as_mut()) else {
        return -1;
    };
    let Some(p) = h.pyramid.choose_level(start, end, pixels) else {
        return -2;
    };
    *out = RpkViewPlan {
        level_index: p.level_index,
        division: p.division,
        first_peak: p.first_peak,
        peak_count: p.peak_count,
        peaks_per_pixel: p.peaks_per_pixel,
    };
    0
}

#[no_mangle]
pub unsafe extern "C" fn rpk_tile_peaks(h: *const RpkHandle) -> usize {
    h.as_ref().map(|x| x.pyramid.tile_peaks).unwrap_or(0)
}

#[no_mangle]
pub unsafe extern "C" fn rpk_tile_count(h: *const RpkHandle, level_index: usize) -> usize {
    h.as_ref()
        .and_then(|x| x.pyramid.tile_count(level_index))
        .unwrap_or(0)
}

#[no_mangle]
pub unsafe extern "C" fn rpk_tile_texture_rgba8(
    h: *const RpkHandle,
    level_index: usize,
    tile_index: u64,
    out_first_peak: *mut usize,
    out_width: *mut usize,
    out_height: *mut usize,
    out: *mut RpkBuffer,
) -> i32 {
    let (Some(h), Some(out)) = (h.as_ref(), out.as_mut()) else {
        return -1;
    };
    let Ok(level_index) = u16::try_from(level_index) else {
        return -2;
    };
    let Some(tile) = h.pyramid.tile(WaveTileKey {
        level_index,
        tile_index,
    }) else {
        return -2;
    };
    let img = encode_wave_tile_rgba8(&tile, h.pyramid.channels);
    if let Some(x) = out_first_peak.as_mut() {
        *x = tile.first_peak;
    }
    if let Some(x) = out_width.as_mut() {
        *x = img.width;
    }
    if let Some(x) = out_height.as_mut() {
        *x = img.height;
    }
    *out = RpkBuffer::from_vec(img.data);
    0
}

#[no_mangle]
pub unsafe extern "C" fn rpk_level_texture_rgba8(
    h: *const RpkHandle,
    level_index: usize,
    out_width: *mut usize,
    out_height: *mut usize,
    out: *mut RpkBuffer,
) -> i32 {
    let (Some(h), Some(out)) = (h.as_ref(), out.as_mut()) else {
        return -1;
    };
    let Some(meta) = h.pyramid.levels.get(level_index).copied() else {
        return -2;
    };
    let Some(peaks) = h.pyramid.read_range(level_index, 0, meta.peak_count) else {
        return -3;
    };
    let img = encode_envelope_rgba8(&peaks, h.pyramid.channels);
    if let Some(x) = out_width.as_mut() {
        *x = img.width;
    }
    if let Some(x) = out_height.as_mut() {
        *x = img.height;
    }
    *out = RpkBuffer::from_vec(img.data);
    0
}

#[no_mangle]
pub unsafe extern "C" fn rpk_spectral_layer_count(h: *const RpkHandle) -> usize {
    h.as_ref()
        .map(|x| x.file.spectral_layers.len())
        .unwrap_or(0)
}

#[no_mangle]
pub unsafe extern "C" fn rpk_spectral_tile_texture_rgba8(
    h: *const RpkHandle,
    spectral_layer_index: usize,
    tile_index: u64,
    out_first_peak: *mut usize,
    out_width: *mut usize,
    out_height: *mut usize,
    out: *mut RpkBuffer,
) -> i32 {
    let (Some(h), Some(out)) = (h.as_ref(), out.as_mut()) else {
        return -1;
    };
    let Some(layer) = h.file.spectral_layers.get(spectral_layer_index) else {
        return -2;
    };
    let ch = h.file.header.channels as usize;
    let n = if ch == 0 { 0 } else { layer.peaks.len() / ch };
    let tile = h.pyramid.tile_peaks.max(1);
    let Ok(tile_index) = usize::try_from(tile_index) else {
        return -3;
    };
    let Some(first) = tile_index.checked_mul(tile) else {
        return -3;
    };
    if first >= n {
        return -3;
    }
    let count = tile.min(n - first);
    let a = first * ch;
    let b = (first + count) * ch;
    let img = encode_spectral_rgba8(&layer.peaks[a..b], ch);
    if let Some(x) = out_first_peak.as_mut() {
        *x = first;
    }
    if let Some(x) = out_width.as_mut() {
        *x = img.width;
    }
    if let Some(x) = out_height.as_mut() {
        *x = img.height;
    }
    *out = RpkBuffer::from_vec(img.data);
    0
}

#[no_mangle]
pub unsafe extern "C" fn rpk_render_rgba8(
    h: *const RpkHandle,
    width: usize,
    height: usize,
    start: u64,
    end: u64,
    bg_rgba: u32,
    wave_rgba: u32,
    out: *mut RpkBuffer,
) -> i32 {
    rpk_render_rgba8_scaled(h, width, height, start, end, 1.0, bg_rgba, wave_rgba, out)
}

#[no_mangle]
pub unsafe extern "C" fn rpk_render_rgba8_scaled(
    h: *const RpkHandle,
    width: usize,
    height: usize,
    start: u64,
    end: u64,
    vertical_full_scale: f32,
    bg_rgba: u32,
    wave_rgba: u32,
    out: *mut RpkBuffer,
) -> i32 {
    let (Some(h), Some(out)) = (h.as_ref(), out.as_mut()) else {
        return -1;
    };
    let unpack = |x: u32| x.to_le_bytes();
    let img = render_waveform_rgba8_scaled(
        &h.pyramid,
        width,
        height,
        start,
        end,
        vertical_full_scale,
        unpack(bg_rgba),
        unpack(wave_rgba),
    );
    *out = RpkBuffer::from_vec(img.data);
    0
}

#[no_mangle]
pub unsafe extern "C" fn rpk_generate_pcm16(
    pcm: *const i16,
    frames: usize,
    channels: usize,
    sample_rate: u32,
    divisions: *const u32,
    division_count: usize,
    source_mtime_low32: u32,
    source_size_low32: u32,
    spectral: u8,
    out: *mut RpkBuffer,
) -> i32 {
    let Some(out) = out.as_mut() else { return -1 };
    if pcm.is_null()
        || divisions.is_null()
        || channels == 0
        || channels > u8::MAX as usize
        || sample_rate == 0
        || division_count == 0
        || division_count > u8::MAX as usize
        || (spectral != 0 && division_count > (u8::MAX as usize / 2))
    {
        return -1;
    }
    let Some(sample_len) = frames.checked_mul(channels) else {
        return -1;
    };
    let pcm = std::slice::from_raw_parts(pcm, sample_len);
    let divs = std::slice::from_raw_parts(divisions, division_count).to_vec();
    let opt = GenerateOptions {
        sample_rate,
        channels,
        divisions: divs,
        source_mtime_low32,
        source_size_low32,
        spectral: spectral != 0,
    };
    match generate_pcm16(pcm, &opt) {
        Ok(v) => {
            *out = RpkBuffer::from_vec(v);
            0
        }
        Err(_) => {
            *out = RpkBuffer::empty();
            -2
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn rpk_generate_f32(
    pcm: *const f32,
    frames: usize,
    channels: usize,
    sample_rate: u32,
    divisions: *const u32,
    division_count: usize,
    source_mtime_low32: u32,
    source_size_low32: u32,
    large_range: u8,
    spectral: u8,
    out: *mut RpkBuffer,
) -> i32 {
    let Some(out) = out.as_mut() else { return -1 };
    if pcm.is_null()
        || divisions.is_null()
        || channels == 0
        || channels > u8::MAX as usize
        || sample_rate == 0
        || division_count == 0
        || division_count > u8::MAX as usize
        || (spectral != 0 && division_count > (u8::MAX as usize / 2))
    {
        return -1;
    }
    let Some(sample_len) = frames.checked_mul(channels) else {
        return -1;
    };
    let pcm = std::slice::from_raw_parts(pcm, sample_len);
    let divs = std::slice::from_raw_parts(divisions, division_count).to_vec();
    let opt = GenerateOptions {
        sample_rate,
        channels,
        divisions: divs,
        source_mtime_low32,
        source_size_low32,
        spectral: spectral != 0,
    };
    match generate_f32(pcm, &opt, large_range != 0) {
        Ok(v) => {
            *out = RpkBuffer::from_vec(v);
            0
        }
        Err(_) => {
            *out = RpkBuffer::empty();
            -2
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn rpk_buffer_free(buf: *mut RpkBuffer) {
    let Some(b) = buf.as_mut() else { return };
    if !b.data.is_null() {
        drop(Vec::from_raw_parts(b.data, b.len, b.capacity));
        *b = RpkBuffer::empty();
    }
}
