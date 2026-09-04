use reapeaks::ffi::{rpk_buffer_free, RpkBuffer};
use reapeaks::{default_divisions, ReaPeaks};

unsafe extern "C" {
    fn rpk_generate_pcm16_reaper(
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
    ) -> i32;

    fn rpk_generate_pcm24_reaper(
        pcm24le: *const u8,
        frames: usize,
        channels: usize,
        sample_rate: u32,
        divisions: *const u32,
        division_count: usize,
        source_mtime_low32: u32,
        source_size_low32: u32,
        mode: u8,
        out: *mut RpkBuffer,
    ) -> i32;

    fn rpk_generate_pcm24_i32_reaper(
        pcm: *const i32,
        frames: usize,
        channels: usize,
        sample_rate: u32,
        divisions: *const u32,
        division_count: usize,
        source_mtime_low32: u32,
        source_size_low32: u32,
        mode: u8,
        out: *mut RpkBuffer,
    ) -> i32;
}

fn buffer() -> RpkBuffer {
    RpkBuffer {
        data: std::ptr::null_mut(),
        len: 0,
        capacity: 0,
    }
}

#[test]
fn c_pcm16_reaper_modes_match_native_layer_shapes() {
    let channels = 2usize;
    let frames = 48_137usize;
    let pcm = vec![0i16; frames * channels];
    let divisions = default_divisions(48_000, 300);

    for (mode, spectral, spectrogram, loudness) in [
        (0u8, 0usize, 0usize, 0usize),
        (1u8, 3usize, 0usize, 2usize),
        (2u8, 3usize, 2usize, 2usize),
    ] {
        let mut out = buffer();
        let rc = unsafe {
            rpk_generate_pcm16_reaper(
                pcm.as_ptr(),
                frames,
                channels,
                48_000,
                divisions.as_ptr(),
                divisions.len(),
                0,
                0,
                mode,
                &mut out,
            )
        };
        assert_eq!(rc, 0, "mode={mode}");
        assert!(!out.data.is_null());
        let bytes = unsafe { std::slice::from_raw_parts(out.data, out.len) }.to_vec();
        let parsed = ReaPeaks::parse(bytes).unwrap();
        assert_eq!(parsed.wave_layers.len(), 3);
        assert_eq!(parsed.spectral_layers.len(), spectral);
        assert_eq!(parsed.spectrogram_layers.len(), spectrogram);
        assert_eq!(parsed.loudness_layers.len(), loudness);
        unsafe { rpk_buffer_free(&mut out) };
    }
}

#[test]
fn c_pcm24_packed_and_i32_paths_match() {
    let channels = 2usize;
    let frames = 4_097usize;
    let pcm_i32 = vec![0i32; frames * channels];
    let pcm24le = vec![0u8; frames * channels * 3];
    let divisions = default_divisions(48_000, 300);

    let mut packed_out = buffer();
    let packed_rc = unsafe {
        rpk_generate_pcm24_reaper(
            pcm24le.as_ptr(),
            frames,
            channels,
            48_000,
            divisions.as_ptr(),
            divisions.len(),
            0,
            pcm24le.len() as u32,
            2,
            &mut packed_out,
        )
    };
    assert_eq!(packed_rc, 0);
    let packed = unsafe { std::slice::from_raw_parts(packed_out.data, packed_out.len) }.to_vec();

    let mut i32_out = buffer();
    let i32_rc = unsafe {
        rpk_generate_pcm24_i32_reaper(
            pcm_i32.as_ptr(),
            frames,
            channels,
            48_000,
            divisions.as_ptr(),
            divisions.len(),
            0,
            pcm24le.len() as u32,
            2,
            &mut i32_out,
        )
    };
    assert_eq!(i32_rc, 0);
    let i32_bytes = unsafe { std::slice::from_raw_parts(i32_out.data, i32_out.len) }.to_vec();
    assert_eq!(i32_bytes, packed);

    let parsed = ReaPeaks::parse(packed).unwrap();
    assert_eq!(parsed.wave_layers.len(), 3);
    assert_eq!(parsed.spectral_layers.len(), 3);
    assert_eq!(parsed.spectrogram_layers.len(), 2);
    assert_eq!(parsed.loudness_layers.len(), 2);

    unsafe {
        rpk_buffer_free(&mut packed_out);
        rpk_buffer_free(&mut i32_out);
    }
}

#[test]
fn c_pcm24_i32_rejects_out_of_range_sample() {
    let pcm = [8_388_608i32];
    let divisions = default_divisions(48_000, 300);
    let mut out = buffer();
    let rc = unsafe {
        rpk_generate_pcm24_i32_reaper(
            pcm.as_ptr(),
            1,
            1,
            48_000,
            divisions.as_ptr(),
            divisions.len(),
            0,
            0,
            0,
            &mut out,
        )
    };
    assert_eq!(rc, -2);
    assert!(out.data.is_null());
    assert_eq!(out.len, 0);
}

#[test]
fn c_reaper_mode_rejects_unknown_value() {
    let pcm = [0i16; 64];
    let divisions = default_divisions(48_000, 300);
    let mut out = buffer();
    let rc = unsafe {
        rpk_generate_pcm16_reaper(
            pcm.as_ptr(),
            pcm.len(),
            1,
            48_000,
            divisions.as_ptr(),
            divisions.len(),
            0,
            0,
            255,
            &mut out,
        )
    };
    assert_eq!(rc, -1);
    assert!(out.data.is_null());
    assert_eq!(out.len, 0);
}
