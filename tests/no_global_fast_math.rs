use std::fs;
use std::path::PathBuf;

#[test]
fn strict_wdl_must_not_use_global_unsafe_fast_math_as_a_compatibility_shortcut() {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let build_rs = fs::read_to_string(manifest.join("build.rs")).expect("read build.rs");

    // These options alter NaN, infinity, signed-zero, reassociation, and reciprocal
    // semantics globally.  A REAPER compatibility fix must instead reproduce the
    // specific runtime floating-point environment or the exact local algorithm.
    // Passing a finite oracle corpus is not sufficient justification for enabling
    // any of these flags across the whole strict-WDL translation unit.
    let forbidden = [
        "-ffast-math",
        "-Ofast",
        "-funsafe-math-optimizations",
        "-fassociative-math",
        "-ffinite-math-only",
        "-freciprocal-math",
        "/fp:fast",
    ];

    for flag in forbidden {
        assert!(
            !build_rs.contains(flag),
            "build.rs contains forbidden global floating-point flag {flag:?}; use an explicit, scoped REAPER-compatible floating-point environment instead"
        );
    }
}
