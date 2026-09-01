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
