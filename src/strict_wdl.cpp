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

    // Diagnostic probe: REAPER's spectral path changes abruptly immediately
    // above 22.05 kHz. Test whether the 22051 -> 22050 case bypasses WDL
    // resampling altogether. The pointwise fresh-process oracle decides this;
    // this branch is intentionally narrow and will be removed or generalized
    // after the experiment.
    if (input_rate == 22051.0 && output_rate == 22050.0) {
        const long long n = std::min(input_frames, output_capacity_frames);
        std::memcpy(output, input,
                    static_cast<size_t>(n) * static_cast<size_t>(channels) * sizeof(double));
        return n;
    }

    WDL_Resampler rs;
    rs.SetMode(true, 1, false, 64, 32);
    rs.SetFeedMode(true);
    rs.SetRates(input_rate, output_rate);

    long long in_pos = 0;
    long long out_pos = 0;

    // REAPER 7.79's ReaPeaks builder allocates a 4096-sample double buffer
    // and divides it by the channel count. It feeds the resampler in chunks
    // of at most that many frames and uses the same value as ResampleOut's
    // output-frame capacity. Matching this call schedule matters at startup
    // and around impulses because the default WDL IIR filter is stateful.
    const int block_frames = std::max(1, 4096 / channels);

    while (in_pos < input_frames && out_pos < output_capacity_frames) {
        const int avail = static_cast<int>(
            std::min<long long>(block_frames, input_frames - in_pos));
        WDL_ResampleSample *inbuf = nullptr;
        const int wanted = rs.ResamplePrepare(avail, channels, &inbuf);
        if (wanted <= 0 || !inbuf) break;
        // In REAPER 7.79 this return value is compared to the requested block
        // and the peak-building pass aborts on a mismatch.
        if (wanted != avail) return -3;
        std::memcpy(
            inbuf,
            input + in_pos * channels,
            static_cast<size_t>(avail) * static_cast<size_t>(channels) * sizeof(double));

        const int out_cap = static_cast<int>(std::min<long long>(
            block_frames, output_capacity_frames - out_pos));
        const int got = rs.ResampleOut(
            output + out_pos * channels, avail, out_cap, channels);
        if (got < 0) return -2;
        in_pos += avail;
        out_pos += got;
    }
    return out_pos;
}

} // extern "C"
