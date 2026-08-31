#![cfg(feature = "strict-wdl")]

use reapeaks::default_divisions;
use std::sync::{Arc, Barrier};
use std::thread;

const FFT_SIZE: usize = 1024;
const FFT_BINS: usize = FFT_SIZE / 2 + 1;
const ANALYSIS_RATE: f64 = 22_050.0;

unsafe extern "C" {
    fn rpk_wdl_real_fft_1024(input: *const f64, out_re: *mut f64, out_im: *mut f64) -> i32;
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

fn fft_bits() -> Vec<(u64, u64)> {
    let mut input = [0.0f64; FFT_SIZE];
    let mut re = [0.0f64; FFT_BINS];
    let mut im = [0.0f64; FFT_BINS];
    input[17] = 0.75;
    input[511] = -0.25;

    let rc = unsafe { rpk_wdl_real_fft_1024(input.as_ptr(), re.as_mut_ptr(), im.as_mut_ptr()) };
    assert_eq!(rc, 0, "strict-WDL FFT bridge failed with status {rc}");

    re.into_iter()
        .zip(im)
        .map(|(real, imaginary)| (real.to_bits(), imaginary.to_bits()))
        .collect()
}

#[test]
fn fft_bridge_is_deterministic_under_concurrent_first_use() {
    // Referencing the Rust library target makes Cargo propagate build.rs's
    // native-link metadata to this integration-test binary. The C bridge is
    // intentionally called directly below so its raw ABI contract is tested.
    assert_eq!(default_divisions(48_000, 300), [160, 2_400, 48_000]);

    const WORKERS: usize = 32;
    let barrier = Arc::new(Barrier::new(WORKERS));
    let handles: Vec<_> = (0..WORKERS)
        .map(|_| {
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                barrier.wait();
                fft_bits()
            })
        })
        .collect();

    let mut results = handles
        .into_iter()
        .map(|handle| handle.join().expect("strict-WDL worker panicked"));
    let expected = results.next().expect("at least one strict-WDL worker");
    for result in results {
        assert_eq!(result, expected);
    }
}

#[test]
fn resampler_bridge_rejects_invalid_boundary_values() {
    let input = [0.0f64; 1];
    let mut output = [0.0f64; 1];

    let nan_rate = unsafe {
        rpk_wdl_resample_all(
            input.as_ptr(),
            1,
            1,
            f64::NAN,
            ANALYSIS_RATE,
            output.as_mut_ptr(),
            1,
        )
    };
    assert_eq!(nan_rate, -1);

    let too_many_channels = unsafe {
        rpk_wdl_resample_all(
            input.as_ptr(),
            1,
            256,
            48_000.0,
            ANALYSIS_RATE,
            output.as_mut_ptr(),
            1,
        )
    };
    assert_eq!(too_many_channels, -1);

    let negative_frames = unsafe {
        rpk_wdl_resample_all(
            input.as_ptr(),
            -1,
            1,
            48_000.0,
            ANALYSIS_RATE,
            output.as_mut_ptr(),
            1,
        )
    };
    assert_eq!(negative_frames, -1);
}
