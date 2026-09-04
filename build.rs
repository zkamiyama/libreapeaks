use std::path::Path;

fn main() {
    println!("cargo:rerun-if-changed=src/rpkx_file_linux.c");
    println!("cargo:rerun-if-changed=src/strict_wdl.cpp");
    println!("cargo:rerun-if-changed=third_party/WDL/WDL/fft.c");
    println!("cargo:rerun-if-changed=third_party/WDL/WDL/resample.cpp");

    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("linux") {
        cc::Build::new()
            .file("src/rpkx_file_linux.c")
            .compile("reapeaks_rpkx_file_linux");
    }

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
}
