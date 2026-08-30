// Optional strict compatibility bridge to Cockos WDL.
// This file is original glue code; WDL itself is provided as a submodule.
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <vector>

#include "fft.h"
#include "resample.h"

extern "C" {

int rpk_wdl_real_fft_1024(const double *input, double *out_re, double *out_im) {
    if (!input || !out_re || !out_im) return -1;
    WDL_fft_init();
    double buf[1024];
    std::memcpy(buf, input, sizeof(buf));
    WDL_real_fft(buf, 1024, 0);

    // WDL real FFT: DC at complex[0].re, Nyquist at complex[0].im.
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

    WDL_Resampler rs;
    rs.SetMode(true, 1, false, 64, 32);
    rs.SetFeedMode(true);
    rs.SetRates(input_rate, output_rate);

    long long in_pos = 0;
    long long out_pos = 0;
    constexpr int kChunk = 16384;

    while (in_pos < input_frames && out_pos < output_capacity_frames) {
        const int avail = static_cast<int>(std::min<long long>(kChunk, input_frames - in_pos));
        WDL_ResampleSample *inbuf = nullptr;
        const int wanted = rs.ResamplePrepare(avail, channels, &inbuf);
        if (wanted <= 0 || !inbuf) break;
        const int feed = std::min(wanted, avail);
        std::memcpy(
            inbuf,
            input + in_pos * channels,
            static_cast<size_t>(feed) * static_cast<size_t>(channels) * sizeof(double));

        const int out_cap = static_cast<int>(std::min<long long>(
            1 << 20, output_capacity_frames - out_pos));
        const int got = rs.ResampleOut(
            output + out_pos * channels, feed, out_cap, channels);
        if (got < 0) return -2;
        in_pos += feed;
        out_pos += got;
        if (feed == 0) break;
    }
    return out_pos;
}

} // extern "C"
