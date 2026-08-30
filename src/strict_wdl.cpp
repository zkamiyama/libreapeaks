// Optional strict compatibility bridge to Cockos WDL.
// This file is original glue code; WDL itself is provided as a submodule.
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <mutex>
#include <vector>

#if defined(__SSE2__) || defined(_M_X64)
#include <xmmintrin.h>
#endif

#include "fft.h"
#include "resample.h"

namespace {

void rpk_wdl_fft_init_once() {
    // WDL's WDL_fft_init() uses a plain static int guard and sets it before
    // filling its process-global twiddle/permutation tables. Concurrent first
    // calls can therefore observe partially initialized tables. Strict mode is
    // callable from parallel Rust/Python workers, so complete WDL's one-time
    // initialization under C++'s thread-safe once primitive before any FFT.
    static std::once_flag once;
    std::call_once(once, [] { WDL_fft_init(); });
}

// Keep WDL's floating-point environment changes local to the bridge call.
// ResampleOut() intentionally enables FTZ/exception masks in strict builds;
// preserving/restoring the complete caller MXCSR prevents one media analysis
// from changing the arithmetic environment observed by the next media.
class RpkFpEnvScope {
public:
#if defined(__SSE2__) || defined(_M_X64)
    RpkFpEnvScope() : mxcsr_(_mm_getcsr()) {}
    ~RpkFpEnvScope() { _mm_setcsr(mxcsr_); }
private:
    unsigned int mxcsr_;
#else
    RpkFpEnvScope() = default;
    ~RpkFpEnvScope() = default;
#endif
};

} // namespace

extern "C" {

int rpk_wdl_real_fft_1024(const double *input, double *out_re, double *out_im) {
    if (!input || !out_re || !out_im) return -1;
    RpkFpEnvScope fp_env;
    rpk_wdl_fft_init_once();
    double buf[1024];
    std::memcpy(buf, input, sizeof(buf));
    WDL_real_fft(buf, 1024, 0);

    auto *c = reinterpret_cast<WDL_FFT_COMPLEX *>(buf);
    out_re[0] = c[0].re;
    out_im[0] = 0.0;
    out_re[512] = c[0].im;
    out_im[512] = 0.0;
    int *perm = WDL_fft_permute_tab(512);
    for (int k = 1; k < 512; ++k) {
        const int j = perm[k];
        out_re[k] = c[j].re;
        out_im[k] = c[j].im;
    }
    return 0;
}

long long rpk_wdl_resample_all(
    const double *input,
    long long input_frames,
    int channels,
    double input_rate,
    double output_rate,
    double *output,
    long long output_capacity_frames) {
    if (!input || !output || input_frames < 0 || channels <= 0 ||
        input_rate <= 0.0 || output_rate <= 0.0 || output_capacity_frames < 0) {
        return -1;
    }

    RpkFpEnvScope fp_env;

    WDL_Resampler rs;
    rs.SetMode(true, 1, false, 64, 32);
    rs.SetFeedMode(true);
    rs.SetRates(input_rate, output_rate);

    long long in_pos = 0;
    long long out_pos = 0;

    // Fresh-process REAPER 7.79 pointwise probes at the 22.05 kHz boundary
    // identify a 2048-double source buffer. WDL's IIR prefilter fade depends
    // on the number of frames passed to ResampleOut(), so this feed granularity
    // is part of strict spectral compatibility. The buffer is interleaved,
    // therefore the frame count is divided by the channel count.
    const int block_frames = std::max(1, 2048 / channels);

    while (in_pos < input_frames && out_pos < output_capacity_frames) {
        WDL_ResampleSample *inbuf = nullptr;
        const int wanted = rs.ResamplePrepare(block_frames, channels, &inbuf);
        if (wanted <= 0 || !inbuf) break;

        // ResamplePrepare() returns writable storage for exactly `wanted`
        // frames. Clear the whole returned region before copying media data so
        // a partial EOF block cannot depend on allocator/heap contents from a
        // previous source. ResampleOut() still receives `avail`, preserving
        // WDL's own EOF/flush behavior and all established REAPER numerics.
        std::memset(
            inbuf,
            0,
            static_cast<size_t>(wanted) * static_cast<size_t>(channels) *
                sizeof(WDL_ResampleSample));

        const int avail = static_cast<int>(std::min<long long>(
            wanted, input_frames - in_pos));
        if (avail > 0) {
            std::memcpy(
                inbuf,
                input + in_pos * channels,
                static_cast<size_t>(avail) * static_cast<size_t>(channels) * sizeof(double));
        }

        const int out_cap = static_cast<int>(std::min<long long>(
            block_frames, output_capacity_frames - out_pos));
        const int got = rs.ResampleOut(
            output + out_pos * channels, avail, out_cap, channels);
        if (got < 0) return -2;

        in_pos += avail;
        out_pos += got;
        if (avail < wanted) break;
    }
    return out_pos;
}

} // extern "C"
