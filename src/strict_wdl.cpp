// Optional strict compatibility bridge to Cockos WDL.
// This file is original glue code; WDL itself is provided as a submodule.
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <limits>
#include <mutex>
#include <type_traits>

#include "fft.h"
#include "resample.h"

namespace {

constexpr int kInvalidArgument = -1;
constexpr int kProcessingFailure = -2;
constexpr int kCppException = -3;
constexpr int kMaxReaPeaksChannels = 255;

static_assert(
    std::is_same<WDL_ResampleSample, double>::value,
    "strict-wdl requires WDL_ResampleSample=double");

void rpk_wdl_fft_init_once() {
    // WDL's WDL_fft_init() uses a plain static int guard and sets it before
    // filling its process-global twiddle/permutation tables. Concurrent first
    // calls can therefore observe partially initialized tables. Strict mode is
    // callable from parallel Rust/Python workers, so complete WDL's one-time
    // initialization under C++'s thread-safe once primitive before any FFT.
    static std::once_flag once;
    std::call_once(once, [] { WDL_fft_init(); });
}

bool valid_interleaved_span(long long frames, int channels) noexcept {
    if (frames < 0 || channels <= 0 || channels > kMaxReaPeaksChannels) {
        return false;
    }

    const auto frame_count = static_cast<unsigned long long>(frames);
    const auto channel_count = static_cast<unsigned long long>(channels);
    const auto max_pointer_elements = static_cast<unsigned long long>(
        std::numeric_limits<std::ptrdiff_t>::max());
    const auto max_double_elements = static_cast<unsigned long long>(
        std::numeric_limits<std::size_t>::max() / sizeof(double));

    return frame_count <= max_pointer_elements / channel_count &&
           frame_count <= max_double_elements / channel_count;
}

} // namespace

extern "C" {

int rpk_wdl_real_fft_1024(
    const double *input,
    double *out_re,
    double *out_im) noexcept {
    try {
        if (!input || !out_re || !out_im) return kInvalidArgument;
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
        if (!perm) return kProcessingFailure;
        for (int k = 1; k < 512; ++k) {
            const int j = perm[k];
            out_re[k] = c[j].re;
            out_im[k] = c[j].im;
        }
        return 0;
    } catch (...) {
        // No C++ exception may cross the C ABI into Rust.
        return kCppException;
    }
}

int rpk_wdl_real_fft_256(
    const double *input,
    double *out_re,
    double *out_im) noexcept {
    try {
        if (!input || !out_re || !out_im) return kInvalidArgument;
        rpk_wdl_fft_init_once();

        // A/B probe for the -'g' path: REAPER may feed a real-valued vector to
        // WDL's full complex FFT rather than WDL_real_fft. WDL_fft returns the
        // conventional DFT scale, while the Rust spectrogram caller applies a
        // 0.5 normalization because WDL_real_fft is 2x. Multiply by two here
        // so this probe changes only the FFT arithmetic/order, not amplitude.
        WDL_FFT_COMPLEX buf[256];
        for (int i = 0; i < 256; ++i) {
            buf[i].re = input[i];
            buf[i].im = 0.0;
        }
        WDL_fft(buf, 256, 0);

        int *perm = WDL_fft_permute_tab(256);
        if (!perm) return kProcessingFailure;
        for (int k = 0; k <= 128; ++k) {
            const int j = perm[k];
            out_re[k] = buf[j].re * 2.0;
            out_im[k] = buf[j].im * 2.0;
        }
        return 0;
    } catch (...) {
        return kCppException;
    }
}

long long rpk_wdl_resample_all(
    const double *input,
    long long input_frames,
    int channels,
    double input_rate,
    double output_rate,
    double *output,
    long long output_capacity_frames) noexcept {
    try {
        if (!input || !output || !std::isfinite(input_rate) ||
            !std::isfinite(output_rate) || input_rate <= 0.0 ||
            output_rate <= 0.0 ||
            !valid_interleaved_span(input_frames, channels) ||
            !valid_interleaved_span(output_capacity_frames, channels)) {
            return kInvalidArgument;
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
            if (wanted <= 0 || !inbuf) return kProcessingFailure;

            const int avail = static_cast<int>(std::min<long long>(
                wanted, input_frames - in_pos));
            if (avail <= 0) return kProcessingFailure;

            const auto input_offset = static_cast<std::size_t>(in_pos) *
                                      static_cast<std::size_t>(channels);
            const auto available_samples = static_cast<std::size_t>(avail) *
                                           static_cast<std::size_t>(channels);
            std::memcpy(
                inbuf,
                input + input_offset,
                available_samples * sizeof(double));

            const int out_cap = static_cast<int>(std::min<long long>(
                block_frames, output_capacity_frames - out_pos));
            if (out_cap <= 0) return kProcessingFailure;

            const auto output_offset = static_cast<std::size_t>(out_pos) *
                                       static_cast<std::size_t>(channels);
            const int got = rs.ResampleOut(
                output + output_offset, avail, out_cap, channels);
            if (got < 0 || got > out_cap) return kProcessingFailure;

            in_pos += avail;
            out_pos += got;
            if (avail < wanted) break;
        }
        return out_pos;
    } catch (...) {
        // WDL allocates internally. Convert allocation and other C++ failures
        // into an ordinary negative backend status instead of unwinding over FFI.
        return kCppException;
    }
}

} // extern "C"
