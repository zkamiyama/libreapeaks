// Dedicated single-precision WDL FFT copy for REAPER spectrogram probes.
// The ordinary strict-WDL bridge intentionally remains double precision for
// the already byte-exact -'s' spectral path.
#include <cstring>
#include <mutex>

#define WDL_FFT_REALSIZE 4
#define WDL_fft_init rpk_g_wdl_fft_init
#define WDL_fft_complexmul rpk_g_wdl_fft_complexmul
#define WDL_fft_complexmul2 rpk_g_wdl_fft_complexmul2
#define WDL_fft_complexmul3 rpk_g_wdl_fft_complexmul3
#define WDL_fft rpk_g_wdl_fft
#define WDL_real_fft rpk_g_wdl_real_fft
#define WDL_fft_permute rpk_g_wdl_fft_permute
#define WDL_fft_permute_tab rpk_g_wdl_fft_permute_tab
#include "../third_party/WDL/WDL/fft.c"

namespace {
void init_fft_once() {
    static std::once_flag once;
    std::call_once(once, [] { rpk_g_wdl_fft_init(); });
}
} // namespace

extern "C" int rpk_wdl_real_fft_256(
    const double *input,
    double *out_re,
    double *out_im) noexcept {
    try {
        if (!input || !out_re || !out_im) return -1;
        init_fft_once();
        float buf[256];
        for (int i = 0; i < 256; ++i) buf[i] = static_cast<float>(input[i]);
        rpk_g_wdl_real_fft(buf, 256, 0);

        auto *c = reinterpret_cast<WDL_FFT_COMPLEX *>(buf);
        out_re[0] = static_cast<double>(c[0].re);
        out_im[0] = 0.0;
        out_re[128] = static_cast<double>(c[0].im);
        out_im[128] = 0.0;
        int *perm = rpk_g_wdl_fft_permute_tab(128);
        if (!perm) return -2;
        for (int k = 1; k < 128; ++k) {
            const int j = perm[k];
            out_re[k] = static_cast<double>(c[j].re);
            out_im[k] = static_cast<double>(c[j].im);
        }
        return 0;
    } catch (...) {
        return -3;
    }
}
