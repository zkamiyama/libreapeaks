// Optional strict compatibility bridge to Cockos WDL.
// This file is original glue code; WDL itself is provided as a submodule.
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <limits>
#include <mutex>
#include <type_traits>
#include <vector>

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

void configure_reaper_spectral_resampler(
    WDL_Resampler &rs,
    double input_rate,
    double output_rate) {
    rs.SetMode(true, 1, false, 64, 32);
    rs.SetFeedMode(true);
    rs.SetRates(input_rate, output_rate);
}

struct RpkWdlResamplerState {
    WDL_Resampler rs;
    int channels;

    RpkWdlResamplerState(int channel_count, double input_rate, double output_rate)
        : channels(channel_count) {
        configure_reaper_spectral_resampler(rs, input_rate, output_rate);
    }
};

} // namespace

extern "C" {

void *rpk_wdl_resampler_create(
    int channels,
    double input_rate,
    double output_rate) noexcept {
    try {
        if (channels <= 0 || channels > kMaxReaPeaksChannels ||
            !std::isfinite(input_rate) || !std::isfinite(output_rate) ||
            input_rate <= 0.0 || output_rate <= 0.0) {
            return nullptr;
        }
        return new RpkWdlResamplerState(channels, input_rate, output_rate);
    } catch (...) {
        return nullptr;
    }
}

void rpk_wdl_resampler_destroy(void *opaque) noexcept {
    try {
        delete static_cast<RpkWdlResamplerState *>(opaque);
    } catch (...) {
        // Destruction must never unwind across the C ABI.
    }
}

int rpk_wdl_resampler_prepare(
    void *opaque,
    int request_frames,
    double **input_buffer) noexcept {
    try {
        if (!opaque || !input_buffer || request_frames <= 0) {
            return kInvalidArgument;
        }
        auto *state = static_cast<RpkWdlResamplerState *>(opaque);
        WDL_ResampleSample *inbuf = nullptr;
        const int wanted = state->rs.ResamplePrepare(
            request_frames, state->channels, &inbuf);
        if (wanted <= 0 || !inbuf) {
            *input_buffer = nullptr;
            return kProcessingFailure;
        }
        *input_buffer = inbuf;
        return wanted;
    } catch (...) {
        if (input_buffer) *input_buffer = nullptr;
        return kCppException;
    }
}

int rpk_wdl_resampler_out(
    void *opaque,
    double *output,
    int input_frames,
    int output_capacity_frames) noexcept {
    try {
        if (!opaque || !output || input_frames <= 0 ||
            output_capacity_frames <= 0) {
            return kInvalidArgument;
        }
        auto *state = static_cast<RpkWdlResamplerState *>(opaque);
        if (!valid_interleaved_span(input_frames, state->channels) ||
            !valid_interleaved_span(output_capacity_frames, state->channels)) {
            return kInvalidArgument;
        }
        const int got = state->rs.ResampleOut(
            output, input_frames, output_capacity_frames, state->channels);
        if (got < 0 || got > output_capacity_frames) {
            return kProcessingFailure;
        }
        return got;
    } catch (...) {
        return kCppException;
    }
}

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
        double buf[256];
        std::memcpy(buf, input, sizeof(buf));
        WDL_real_fft(buf, 256, 0);

        auto *c = reinterpret_cast<WDL_FFT_COMPLEX *>(buf);
        out_re[0] = c[0].re;
        out_im[0] = 0.0;
        out_re[128] = c[0].im;
        out_im[128] = 0.0;
        int *perm = WDL_fft_permute_tab(128);
        if (!perm) return kProcessingFailure;
        for (int k = 1; k < 128; ++k) {
            const int j = perm[k];
            out_re[k] = c[j].re;
            out_im[k] = c[j].im;
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
        configure_reaper_spectral_resampler(rs, input_rate, output_rate);

        long long in_pos = 0;
        long long out_pos = 0;

        // Fresh-process REAPER 7.79 pointwise probes at the 22.05 kHz boundary
        // identify a 2048-double source buffer. WDL's IIR prefilter fade depends
        // on the number of frames passed to ResampleOut(), so this feed granularity
        // is part of strict spectral compatibility. The buffer is interleaved,
        // therefore the frame count is divided by the channel count.
        const int block_frames = std::max(1, 2048 / channels);

        while (in_pos < input_frames && out_pos < output_capacity_frames) {
            // Feed mode's request is the amount of source input being offered.
            // Shrink the final request to the actual remaining source frames so
            // the returned analysis stream can be compared directly with the
            // fresh-process EOF oracle, without adding synthetic analysis data.
            const int request_frames = static_cast<int>(std::min<long long>(
                block_frames, input_frames - in_pos));
            if (request_frames <= 0) return kProcessingFailure;

            WDL_ResampleSample *inbuf = nullptr;
            const int wanted = rs.ResamplePrepare(request_frames, channels, &inbuf);
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

long long rpk_wdl_resample_count(
    long long input_frames,
    int channels,
    double input_rate,
    double output_rate) noexcept {
    try {
        if (!std::isfinite(input_rate) || !std::isfinite(output_rate) ||
            input_rate <= 0.0 || output_rate <= 0.0 ||
            !valid_interleaved_span(input_frames, channels)) {
            return kInvalidArgument;
        }

        WDL_Resampler rs;
        configure_reaper_spectral_resampler(rs, input_rate, output_rate);
        const int block_frames = std::max(1, 2048 / channels);
        std::vector<double> out(
            static_cast<std::size_t>(block_frames) *
            static_cast<std::size_t>(channels));

        long long in_pos = 0;
        long long out_pos = 0;
        while (in_pos < input_frames) {
            const int request_frames = static_cast<int>(std::min<long long>(
                block_frames, input_frames - in_pos));
            if (request_frames <= 0) return kProcessingFailure;

            WDL_ResampleSample *inbuf = nullptr;
            const int wanted = rs.ResamplePrepare(request_frames, channels, &inbuf);
            if (wanted <= 0 || !inbuf) return kProcessingFailure;

            const int avail = static_cast<int>(std::min<long long>(
                wanted, input_frames - in_pos));
            if (avail <= 0) return kProcessingFailure;
            std::fill_n(
                inbuf,
                static_cast<std::size_t>(avail) * static_cast<std::size_t>(channels),
                0.0);

            const int got = rs.ResampleOut(
                out.data(), avail, block_frames, channels);
            if (got < 0 || got > block_frames) return kProcessingFailure;

            in_pos += avail;
            out_pos += got;
            if (avail < wanted) break;
        }
        return out_pos;
    } catch (...) {
        return kCppException;
    }
}

} // extern "C"
