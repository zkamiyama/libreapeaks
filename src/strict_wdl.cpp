// Optional strict compatibility bridge to Cockos WDL.
// This file is original glue code; WDL itself is provided as a submodule.
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <mutex>
#include <vector>

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

} // namespace

extern "C" {

int rpk_wdl_real_fft_1024(const double *input, double *out_re, double *out_im) {
    if (!input || !out_re || !out_im) return -1;
    rpk_wdl_fft_init_once();

    double buf[1024];
    std::memcpy(buf, input, sizeof(buf));
    WDL_real_fft(buf, 1024, 0);

    // WDL_real_fft stores 512 permuted complex pairs directly in the packed
    // double buffer. Read those pairs through the buffer's declared type rather
    // than reinterpret_casting double[] to WDL_FFT_COMPLEX*, which is undefined
    // under C++ strict-aliasing and produced executable-layout-dependent output.
    out_re[0] = buf[0];
    out_im[0] = 0.0;
    out_re[512] = buf[1];
    out_im[512] = 0.0;
    int *perm = WDL_fft_permute_tab(512);
    for (int k = 1; k < 512; ++k) {
        const int j = perm[k];
        out_re[k] = buf[2 * j];
        out_im[k] = buf[2 * j + 1];
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
