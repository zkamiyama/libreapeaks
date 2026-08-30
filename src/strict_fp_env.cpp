// Floating-point environment isolation for REAPER 7.79 strict spectral mode.
// This is libreapeaks-owned glue; no WDL source is modified.
#include <cfenv>
#include <cstdint>
#include <new>

#if defined(__SSE2__) || defined(_M_X64)
#include <xmmintrin.h>
#endif

namespace {

struct RpkStrictFpEnv {
    fenv_t env{};
    bool have_env = false;
#if defined(__SSE2__) || defined(_M_X64)
    unsigned int mxcsr = 0;
#endif
};

} // namespace

extern "C" {

void *rpk_strict_fp_env_enter() {
    auto *state = new (std::nothrow) RpkStrictFpEnv();
    if (!state) return nullptr;

    state->have_env = std::fegetenv(&state->env) == 0;
#if defined(__SSE2__) || defined(_M_X64)
    state->mxcsr = _mm_getcsr();
#endif

    // A fresh x86_64 Linux process starts in round-to-nearest with all
    // exceptions masked and FTZ/DAZ disabled. Canonicalize the complete
    // thread-local FP environment before *any* strict spectral arithmetic,
    // including Rust's Hann/phase/density math, rather than merely preserving
    // whatever state the previous media left behind.
    std::fesetenv(FE_DFL_ENV);
    std::fesetround(FE_TONEAREST);
    std::feclearexcept(FE_ALL_EXCEPT);
#if defined(__SSE2__) || defined(_M_X64)
    _mm_setcsr(0x1f80u);
#endif
    return state;
}

void rpk_strict_fp_env_leave(void *opaque) {
    auto *state = static_cast<RpkStrictFpEnv *>(opaque);
    if (!state) return;

    if (state->have_env) {
        std::fesetenv(&state->env);
    }
#if defined(__SSE2__) || defined(_M_X64)
    _mm_setcsr(state->mxcsr);
#endif
    delete state;
}

} // extern "C"
