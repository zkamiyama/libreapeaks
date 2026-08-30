use std::path::Path;

fn main() {
    println!("cargo:rerun-if-changed=src/strict_wdl.cpp");
    println!("cargo:rerun-if-changed=src/strict_fp_env.cpp");
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

    // WDL's FFT implementation is C and intentionally reinterprets its packed
    // real-FFT buffer as complex pairs. Compile it as C, not as C++, and turn
    // off strict-aliasing optimization so the upstream type-punning contract is
    // deterministic regardless of the final executable's layout.
    cc::Build::new()
        .define("WDL_FFT_REALSIZE", Some("8"))
        .include(wdl)
        .file(wdl.join("fft.c"))
        .flag_if_supported("-std=c99")
        .flag_if_supported("-fno-strict-aliasing")
        .compile("reapeaks_wdl_fft");

    cc::Build::new()
        .cpp(true)
        .define("WDL_FFT_REALSIZE", Some("8"))
        // REAPER 7.79's ResampleOut temporarily enables MXCSR FTZ with the
        // exact SSE2 mask emitted by WDL when this define is active. Without
        // it, tiny IIR tails survive and produce non-zero spectral codes after
        // impulse material where REAPER has already flushed to silence.
        .define("WDL_DENORMAL_WANTS_SCOPED_FTZ", None)
        .include(wdl)
        .file(wdl.join("resample.cpp"))
        .file("src/strict_wdl.cpp")
        .file("src/strict_fp_env.cpp")
        .flag_if_supported("-std=c++17")
        .compile("reapeaks_wdl_cpp");
}
