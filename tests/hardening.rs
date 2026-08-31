use reapeaks::ffi::{rpk_close, rpk_generate_pcm16, rpk_open, rpk_tile_texture_rgba8, RpkBuffer};
use reapeaks::format::{
    encode, GeneratedLayer, LayerHeader, Version, TOKEN_LOUDNESS, TOKEN_SPECTRAL,
};
use reapeaks::{generate_pcm16, GenerateOptions, ReaPeaks, WaveEncoding, WaveLayer, WavePyramid};
use std::ffi::CString;
use std::ptr::{self, NonNull};

fn bare_header(channels: u8, mipmaps: u8, sample_rate: u32) -> Vec<u8> {
    let mut raw = Vec::new();
    raw.extend_from_slice(b"RPKN");
    raw.push(channels);
    raw.push(mipmaps);
    raw.extend_from_slice(&sample_rate.to_le_bytes());
    raw.extend_from_slice(&0u32.to_le_bytes());
    raw.extend_from_slice(&0u32.to_le_bytes());
    raw
}

#[test]
fn generator_rejects_channel_header_truncation() {
    let opt = GenerateOptions {
        sample_rate: 48_000,
        channels: 257,
        divisions: vec![160],
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: false,
    };
    assert!(generate_pcm16(&[], &opt).is_err());
}

#[test]
fn generator_rejects_division_that_would_become_negative_token() {
    let opt = GenerateOptions {
        sample_rate: 48_000,
        channels: 1,
        divisions: vec![i32::MAX as u32 + 1],
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: false,
    };
    assert!(generate_pcm16(&[0], &opt).is_err());
}

#[test]
fn parser_rejects_zero_sample_rate() {
    assert!(ReaPeaks::parse(bare_header(1, 0, 0)).is_err());
}

#[test]
fn parser_rejects_unknown_negative_layer_token() {
    let mut raw = bare_header(1, 1, 48_000);
    raw.extend_from_slice(&(-1i32).to_le_bytes());
    raw.extend_from_slice(&0u32.to_le_bytes());
    assert!(ReaPeaks::parse(raw).is_err());
}

#[test]
fn parser_rejects_trailing_garbage() {
    let mut raw = bare_header(1, 0, 48_000);
    raw.push(0xaa);
    assert!(ReaPeaks::parse(raw).is_err());
}

#[test]
fn parser_rejects_truncated_terminal_loudness() {
    let mut raw = bare_header(1, 1, 48_000);
    raw.extend_from_slice(&TOKEN_LOUDNESS.to_le_bytes());
    raw.extend_from_slice(&1u32.to_le_bytes());
    assert!(ReaPeaks::parse(raw).is_err());
}

#[test]
fn parser_rejects_spectral_layer_without_wave_partner() {
    let mut raw = bare_header(1, 1, 48_000);
    raw.extend_from_slice(&TOKEN_SPECTRAL.to_le_bytes());
    raw.extend_from_slice(&0u32.to_le_bytes());
    assert!(ReaPeaks::parse(raw).is_err());
}

#[test]
fn encoder_rejects_inconsistent_payload() {
    let layer = GeneratedLayer {
        header: LayerHeader {
            division: 160,
            peak_count: 1,
        },
        bytes: Vec::new(),
    };
    assert!(encode(Version::Rpkn, 1, 48_000, 0, 0, &[layer]).is_err());
}

#[test]
fn parser_random_bytes_never_panic() {
    let mut state = 0x7a31_19d2_6c8f_42b5u64;
    for len in 0..384usize {
        for _ in 0..16 {
            let mut raw = vec![0u8; len];
            for byte in &mut raw {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                *byte = state as u8;
            }
            let result = std::panic::catch_unwind(|| ReaPeaks::parse(raw));
            assert!(result.is_ok(), "parser panicked for len={len}");
        }
    }
}

#[test]
fn ffi_rejects_interleaved_length_overflow_before_slice_creation() {
    let div = 1u32;
    let mut out = RpkBuffer {
        data: ptr::null_mut(),
        len: 0,
        capacity: 0,
    };
    let rc = unsafe {
        rpk_generate_pcm16(
            NonNull::<i16>::dangling().as_ptr(),
            usize::MAX,
            2,
            48_000,
            &div,
            1,
            0,
            0,
            0,
            &mut out,
        )
    };
    assert_eq!(rc, -1);
    assert!(out.data.is_null());
}

#[test]
fn ffi_does_not_truncate_large_level_index_to_u16() {
    let opt = GenerateOptions {
        sample_rate: 48_000,
        channels: 1,
        divisions: vec![1],
        source_mtime_low32: 0,
        source_size_low32: 0,
        spectral: false,
    };
    let blob = generate_pcm16(&[0, 1, -1], &opt).unwrap();
    let path = std::env::temp_dir().join(format!(
        "libreapeaks-hardening-{}-{}.reapeaks",
        std::process::id(),
        0x51a7u32
    ));
    std::fs::write(&path, blob).unwrap();
    let cpath = CString::new(path.to_string_lossy().as_bytes()).unwrap();
    let mut handle = ptr::null_mut();
    assert_eq!(unsafe { rpk_open(cpath.as_ptr(), &mut handle) }, 0);
    let mut out = RpkBuffer {
        data: ptr::null_mut(),
        len: 0,
        capacity: 0,
    };
    let rc = unsafe {
        rpk_tile_texture_rgba8(
            handle,
            u16::MAX as usize + 1,
            0,
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
            &mut out,
        )
    };
    assert_eq!(rc, -2);
    unsafe { rpk_close(handle) };
    let _ = std::fs::remove_file(path);
}

#[test]
fn pyramid_ignores_zero_division_native_layer() {
    let native = [WaveLayer {
        division: 0,
        peaks: Vec::new(),
    }];
    let pyramid = WavePyramid::from_native(1, 0, &native, 4, WaveEncoding::Rpkn);
    assert!(pyramid.levels.is_empty());
    assert!(pyramid.choose_level(0, 1, 1).is_none());
}

#[test]
fn choose_level_handles_u64_end_without_addition_overflow() {
    let native = [WaveLayer {
        division: 4,
        peaks: vec![Default::default()],
    }];
    let pyramid = WavePyramid::from_native(1, 4, &native, 4, WaveEncoding::Rpkn);
    let plan = pyramid.choose_level(u64::MAX - 16, u64::MAX, 8).unwrap();
    assert_eq!(plan.peak_count, 0);
}
