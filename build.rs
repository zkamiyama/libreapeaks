use std::path::Path;

fn main() {
    println!("cargo:rerun-if-changed=src/strict_wdl.cpp");
    println!("cargo:rerun-if-changed=src/spectrogram_wdl_float.cpp");
    println!("cargo:rerun-if-changed=third_party/WDL/WDL/fft.c");
    println!("cargo:rerun-if-changed=third_party/WDL/WDL/resample.cpp");

    if std::env::var_os("CARGO_FEATURE_STRICT_WDL").is_none() {
        return;
    }

    let wdl = Path::new("third_party/WDL/WDL");
    if !wdl.join("fft.c").is_file() || !wdl.join("resample.cpp").is_file() {
        panic!(
            "strict-wdl requires the WDL submodule. Run: git submodule update --init --recursive"
        );
    }

    cc::Build::new()
        .cpp(true)
        .define("WDL_FFT_REALSIZE", Some("8"))
        // Keep the old 256-point double bridge available under a private name;
        // the public symbol is supplied by the dedicated float spectrogram
        // translation unit below. The 1024-point strict spectral bridge stays
        // exactly as before.
        .define(
            "rpk_wdl_real_fft_256",
            Some("rpk_wdl_real_fft_256_f64"),
        )
        // REAPER 7.79's ResampleOut temporarily enables MXCSR FTZ with the
        // exact SSE2 mask emitted by WDL when this define is active. Without
        // it, tiny IIR tails survive and produce non-zero spectral codes after
        // impulse material where REAPER has already flushed to silence.
        .define("WDL_DENORMAL_WANTS_SCOPED_FTZ", None)
        .include(wdl)
        .file(wdl.join("fft.c"))
        .file(wdl.join("resample.cpp"))
        .file("src/strict_wdl.cpp")
        .flag_if_supported("-std=c++17")
        .compile("reapeaks_wdl");

    // REAPER's public WDL FFT defaults to single precision. Keep a separately
    // namespaced copy so -'g' can reproduce that numerical path without
    // changing the double-precision strict spectral/resampler bridge above.
    cc::Build::new()
        .cpp(true)
        .include(wdl)
        .file("src/spectrogram_wdl_float.cpp")
        .flag_if_supported("-std=c++17")
        .compile("reapeaks_wdl_spectrogram_float");
}
