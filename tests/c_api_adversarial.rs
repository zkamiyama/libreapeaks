use reapeaks::ffi::{
    rpk_buffer_free, rpk_default_divisions, rpk_generate_pcm16, rpk_level_count, rpk_open,
    rpk_wave_encoding, RpkBuffer,
};
use std::ffi::CString;
use std::ptr;

fn empty_buffer() -> RpkBuffer {
    RpkBuffer {
        data: ptr::null_mut(),
        len: 0,
        capacity: 0,
    }
}

#[test]
fn c_default_divisions_matches_reaper_preference_matrix_and_rejects_bad_inputs() {
    let cases = [
        (22_051, 100, [220, 1_320, 22_440]),
        (44_100, 150, [294, 2_352, 44_688]),
        (48_000, 200, [240, 2_400, 48_000]),
        (22_051, 300, [73, 1_168, 22_192]),
        (44_100, 500, [88, 2_288, 45_760]),
        (48_000, 1_000, [48, 2_400, 48_000]),
    ];
    for (sample_rate, peak_rate, expected) in cases {
        let mut output = [0u32; 3];
        assert_eq!(
            unsafe { rpk_default_divisions(sample_rate, peak_rate, output.as_mut_ptr()) },
            0
        );
        assert_eq!(output, expected);
    }

    let mut output = [0xdead_beefu32; 3];
    assert_eq!(unsafe { rpk_default_divisions(0, 300, output.as_mut_ptr()) }, -1);
    assert_eq!(output, [0xdead_beef; 3]);
    assert_eq!(unsafe { rpk_default_divisions(48_000, 0, output.as_mut_ptr()) }, -1);
    assert_eq!(unsafe { rpk_default_divisions(48_000, 300, ptr::null_mut()) }, -1);
}

#[test]
fn c_null_handles_are_total_and_return_neutral_values() {
    assert_eq!(unsafe { rpk_level_count(ptr::null()) }, 0);
    assert_eq!(unsafe { rpk_wave_encoding(ptr::null()) }, 0);
    unsafe { rpk_buffer_free(ptr::null_mut()) };
}

#[test]
fn c_open_rejects_null_and_non_utf8_paths_without_touching_output() {
    let mut handle = 1usize as *mut _;
    assert_eq!(unsafe { rpk_open(ptr::null(), &mut handle) }, -1);
    assert!(handle.is_null());
    assert_eq!(unsafe { rpk_open(ptr::null(), ptr::null_mut()) }, -1);

    let path = CString::new(vec![0xff]).expect("CString");
    handle = 1usize as *mut _;
    assert_eq!(unsafe { rpk_open(path.as_ptr(), &mut handle) }, -2);
    assert!(handle.is_null());
}

#[test]
fn c_generator_rejects_pointer_shape_and_overflow_attacks() {
    let pcm = [0i16; 4];
    let division = [1u32];
    let mut out = empty_buffer();

    assert_eq!(
        unsafe {
            rpk_generate_pcm16(
                ptr::null(),
                1,
                1,
                48_000,
                division.as_ptr(),
                1,
                0,
                0,
                0,
                &mut out,
            )
        },
        -1
    );
    assert_eq!(
        unsafe {
            rpk_generate_pcm16(
                pcm.as_ptr(),
                1,
                1,
                48_000,
                ptr::null(),
                1,
                0,
                0,
                0,
                &mut out,
            )
        },
        -1
    );
    assert_eq!(
        unsafe {
            rpk_generate_pcm16(
                pcm.as_ptr(),
                usize::MAX,
                2,
                48_000,
                division.as_ptr(),
                1,
                0,
                0,
                0,
                &mut out,
            )
        },
        -1
    );
    assert_eq!(
        unsafe {
            rpk_generate_pcm16(
                pcm.as_ptr(),
                1,
                256,
                48_000,
                division.as_ptr(),
                1,
                0,
                0,
                0,
                &mut out,
            )
        },
        -1
    );
    assert_eq!(
        unsafe {
            rpk_generate_pcm16(
                pcm.as_ptr(),
                1,
                1,
                0,
                division.as_ptr(),
                1,
                0,
                0,
                0,
                &mut out,
            )
        },
        -1
    );
    assert_eq!(
        unsafe {
            rpk_generate_pcm16(
                pcm.as_ptr(),
                1,
                1,
                48_000,
                division.as_ptr(),
                0,
                0,
                0,
                0,
                &mut out,
            )
        },
        -1
    );
    assert_eq!(
        unsafe {
            rpk_generate_pcm16(
                pcm.as_ptr(),
                1,
                1,
                48_000,
                division.as_ptr(),
                1,
                0,
                0,
                0,
                ptr::null_mut(),
            )
        },
        -1
    );
}

#[test]
fn c_generator_errors_zero_the_output_and_successful_buffers_free_idempotently() {
    let pcm = [1i16, -1, 2, -2];
    let bad_division = [0u32];
    let mut out = RpkBuffer {
        data: 1usize as *mut u8,
        len: 123,
        capacity: 456,
    };
    assert_eq!(
        unsafe {
            rpk_generate_pcm16(
                pcm.as_ptr(),
                pcm.len(),
                1,
                48_000,
                bad_division.as_ptr(),
                1,
                0,
                0,
                0,
                &mut out,
            )
        },
        -2
    );
    assert!(out.data.is_null());
    assert_eq!(out.len, 0);
    assert_eq!(out.capacity, 0);

    let division = [1u32];
    assert_eq!(
        unsafe {
            rpk_generate_pcm16(
                pcm.as_ptr(),
                pcm.len(),
                1,
                48_000,
                division.as_ptr(),
                1,
                0,
                0,
                0,
                &mut out,
            )
        },
        0
    );
    assert!(!out.data.is_null());
    assert!(out.len > 0);
    unsafe { rpk_buffer_free(&mut out) };
    assert!(out.data.is_null());
    assert_eq!(out.len, 0);
    assert_eq!(out.capacity, 0);
    unsafe { rpk_buffer_free(&mut out) };
    assert!(out.data.is_null());
}
