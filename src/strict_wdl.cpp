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

#include "denormal.h"
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

struct RpkIirState {
    double h0 = 0.0;
    double h1 = 0.0;
    double h2 = 0.0;
    double h3 = 0.0;
};

struct RpkIirCoeffs {
    double b0;
    double b1;
    double b2;
    double a1;
    double a2;
};

RpkIirCoeffs rpk_wdl_prefilter_coeffs(double ratio) {
    // Mirrors WDL_Resampler_Filter::setParms() for the exact strict mode we
    // use: filtercnt=1, sinc=false, source_rate > 22050, output_rate=22050.
    const double fpos2 = static_cast<double>(0.693f);
    const double q = static_cast<double>(0.707f);
    double fpos = 1.0 / ratio;
    if (fpos < 1.0) {
        fpos *= fpos2;
    } else {
        fpos = 1.0;
    }

    const double pos = (fpos == 1.0 ? fpos2 : fpos) *
        3.1415926535897932384626433832795;
    const double cpos = std::cos(pos);
    const double spos = std::sin(pos);
    const double alpha = spos / (2.0 * q);
    const double sc = 1.0 / (1.0 + alpha);
    const double b1 = (1.0 - cpos) * sc;
    return RpkIirCoeffs {
        b1 * 0.5,
        b1,
        b1 * 0.5,
        -2.0 * cpos * sc,
        (1.0 - alpha) * sc,
    };
}

void rpk_wdl_filter_block(
    std::vector<double> &block,
    int frames,
    int channels,
    const RpkIirCoeffs &co,
    std::vector<RpkIirState> &state,
    bool fade_in) {
    if (frames <= 0) return;

    // WDL's ApplyBuffer() iterates channel first, then filter stage, then
    // samples. Keep that ordering because strict compatibility is bit-exact.
    for (int ch = 0; ch < channels; ++ch) {
        RpkIirState &s = state[static_cast<size_t>(ch)];
        double v0 = 0.0;
        const double dv = fade_in ? 1.0 / static_cast<double>(frames) : 0.0;
        for (int f = 0; f < frames; ++f) {
            const size_t idx = static_cast<size_t>(f) * channels + ch;
            const double in = block[idx];
            const double y = static_cast<double>(
                in * co.b0 + s.h0 * co.b1 + s.h1 * co.b2 -
                s.h2 * co.a1 - s.h3 * co.a2);
            s.h1 = s.h0;
            s.h0 = in;
            s.h3 = s.h2;
            s.h2 = denormal_filter_double(y);
            if (fade_in) {
                block[idx] = s.h2 * v0 + in * (1.0 - v0);
                v0 += dv;
            } else {
                block[idx] = s.h2;
            }
        }
    }
}

long long rpk_local_wdl_linear_resample(
    const double *input,
    long long input_frames,
    int channels,
    double input_rate,
    double output_rate,
    double *output,
    long long output_capacity_frames) {
    const double ratio = input_rate / output_rate;
    if (!(ratio > 1.0)) {
        return -3; // strict spectral path only calls this for source_rate > 22050.
    }

#ifdef WDL_DENORMAL_WANTS_SCOPED_FTZ
    WDL_denormal_ftz_scope ftz_force;
#endif

    const RpkIirCoeffs co = rpk_wdl_prefilter_coeffs(ratio);
    std::vector<RpkIirState> filter_state(static_cast<size_t>(channels));

    // REAPER 7.79 probes identify 2048 interleaved doubles per source feed.
    const int block_frames = std::max(1, 2048 / channels);

    std::vector<double> buffered;
    buffered.reserve(static_cast<size_t>(block_frames + 4) * channels);
    long long in_pos = 0;
    long long out_pos = 0;
    double fracpos = 0.0;
    bool first_filter_block = true;

    // WDL starts the pre-filter fade only for a newly activated near-unity
    // varispeed path: 1/ratio >= 0.97.
    const bool near_unity = (1.0 / ratio) >= 0.97;

    while (in_pos < input_frames && out_pos < output_capacity_frames) {
        const int avail = static_cast<int>(std::min<long long>(
            block_frames, input_frames - in_pos));
        if (avail <= 0) break;

        std::vector<double> incoming(static_cast<size_t>(avail) * channels);
        std::memcpy(
            incoming.data(),
            input + in_pos * channels,
            incoming.size() * sizeof(double));

        rpk_wdl_filter_block(
            incoming,
            avail,
            channels,
            co,
            filter_state,
            first_filter_block && near_unity);
        first_filter_block = false;

        buffered.insert(buffered.end(), incoming.begin(), incoming.end());
        in_pos += avail;

        const int actual_frames = static_cast<int>(buffered.size() / channels);
        const bool eof = avail < block_frames;
        const int padded_frames = eof ? (block_frames - avail) * 2 : 0;
        const int available_for_output = actual_frames + padded_frames;
        const int out_cap = static_cast<int>(std::min<long long>(
            block_frames, output_capacity_frames - out_pos));

        int produced = 0;
        double srcpos = fracpos;
        for (; produced < out_cap; ++produced) {
            const int ipos = static_cast<int>(srcpos);
            if (ipos >= available_for_output - 1) break;
            const double f = srcpos - static_cast<double>(ipos);
            const double inv = 1.0 - f;
            for (int ch = 0; ch < channels; ++ch) {
                const auto sample_at = [&](int frame) -> double {
                    if (frame < 0 || frame >= actual_frames) return 0.0;
                    return buffered[static_cast<size_t>(frame) * channels + ch];
                };
                const double a = sample_at(ipos);
                const double b = sample_at(ipos + 1);
                output[(out_pos + produced) * channels + ch] =
                    a * inv + b * f;
            }
            srcpos += ratio;
        }

        int returned = produced;
        if (eof) {
            // Match WDL_Resampler::ResampleOut()'s flush adjustment: padded
            // samples may be computed into the caller buffer, but they are not
            // included in the returned valid-frame count.
            const double adj =
                (srcpos - static_cast<double>(actual_frames)) / ratio;
            if (adj > 0.0) {
                returned -= static_cast<int>(adj + 0.5);
                if (returned < 0) returned = 0;
            }
            out_pos += returned;
            break;
        }

        int consumed = static_cast<int>(srcpos);
        if (consumed > actual_frames) consumed = actual_frames;
        fracpos = srcpos - static_cast<double>(consumed);
        if (consumed > 0) {
            const size_t erase_samples = static_cast<size_t>(consumed) * channels;
            buffered.erase(buffered.begin(), buffered.begin() + erase_samples);
        }
        out_pos += returned;
    }

    return out_pos;
}

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
    rpk_wdl_fft_init_once();
    return rpk_local_wdl_linear_resample(
        input,
        input_frames,
        channels,
        input_rate,
        output_rate,
        output,
        output_capacity_frames);
}

} // extern "C"
