use crate::ffi::RpkBuffer;
use crate::generate::GenerateOptions;
use crate::reaper_generate::{generate_f32_reaper, generate_pcm16_reaper, ReaperPeakMode};

fn empty_buffer() -> RpkBuffer {
    RpkBuffer {
        data: std::ptr::null_mut(),
        len: 0,
        capacity: 0,
    }
}

fn into_buffer(mut bytes: Vec<u8>) -> RpkBuffer {
    let out = RpkBuffer {
        data: bytes.as_mut_ptr(),
        len: bytes.len(),
        capacity: bytes.capacity(),
    };
    std::mem::forget(bytes);
    out
}

unsafe fn common_options(
    channels: usize,
    sample_rate: u32,
    divisions: *const u32,
    division_count: usize,
    source_mtime_low32: u32,
    source_size_low32: u32,
) -> Option<GenerateOptions> {
    if divisions.is_null()
        || channels == 0
        || channels > u8::MAX as usize
        || sample_rate == 0
        || division_count == 0
        || division_count > u8::MAX as usize
    {
        return None;
    }
    let divisions = std::slice::from_raw_parts(divisions, division_count).to_vec();
    Some(GenerateOptions {
        sample_rate,
        channels,
        divisions,
        source_mtime_low32,
        source_size_low32,
        spectral: false,
    })
}

/// Generate a REAPER-native PCM16 cache mode.
///
/// mode=0: waveform only
/// mode=1: waveform + -'s' spectral + -'r' loudness
/// mode=2: waveform + -'s' + -'g' spectrogram + -'r'
#[no_mangle]
pub unsafe extern "C" fn rpk_generate_pcm16_reaper(
    pcm: *const i16,
    frames: usize,
    channels: usize,
    sample_rate: u32,
    divisions: *const u32,
    division_count: usize,
    source_mtime_low32: u32,
    source_size_low32: u32,
    mode: u8,
    out: *mut RpkBuffer,
) -> i32 {
    let Some(out) = out.as_mut() else { return -1 };
    *out = empty_buffer();
    if pcm.is_null() {
        return -1;
    }
    let Ok(mode) = ReaperPeakMode::try_from(mode) else {
        return -1;
    };
    let Some(options) = common_options(
        channels,
        sample_rate,
        divisions,
        division_count,
        source_mtime_low32,
        source_size_low32,
    ) else {
        return -1;
    };
    let Some(sample_len) = frames.checked_mul(channels) else {
        return -1;
    };
    let pcm = std::slice::from_raw_parts(pcm, sample_len);
    match generate_pcm16_reaper(pcm, &options, mode) {
        Ok(bytes) => {
            *out = into_buffer(bytes);
            0
        }
        Err(_) => -2,
    }
}

/// Generate a REAPER-native float32 cache mode.
///
/// mode=0: waveform only
/// mode=1: waveform + -'s' spectral + -'r' loudness
/// mode=2: waveform + -'s' + -'g' spectrogram + -'r'
///
/// Float `-'g'` generation is implemented without a PCM16 approximation, but
/// remains outside the byte-identical REAPER compatibility claim until a
/// dedicated float32 live-oracle matrix is added.
#[no_mangle]
pub unsafe extern "C" fn rpk_generate_f32_reaper(
    pcm: *const f32,
    frames: usize,
    channels: usize,
    sample_rate: u32,
    divisions: *const u32,
    division_count: usize,
    source_mtime_low32: u32,
    source_size_low32: u32,
    large_range: u8,
    mode: u8,
    out: *mut RpkBuffer,
) -> i32 {
    let Some(out) = out.as_mut() else { return -1 };
    *out = empty_buffer();
    if pcm.is_null() {
        return -1;
    }
    let Ok(mode) = ReaperPeakMode::try_from(mode) else {
        return -1;
    };
    let Some(options) = common_options(
        channels,
        sample_rate,
        divisions,
        division_count,
        source_mtime_low32,
        source_size_low32,
    ) else {
        return -1;
    };
    let Some(sample_len) = frames.checked_mul(channels) else {
        return -1;
    };
    let pcm = std::slice::from_raw_parts(pcm, sample_len);
    match generate_f32_reaper(pcm, &options, large_range != 0, mode) {
        Ok(bytes) => {
            *out = into_buffer(bytes);
            0
        }
        Err(_) => -2,
    }
}
