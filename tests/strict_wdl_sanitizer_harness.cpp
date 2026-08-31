#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <string>
#include <thread>
#include <vector>

extern "C" int rpk_wdl_real_fft_1024(
    const double *input,
    double *out_re,
    double *out_im) noexcept;

extern "C" long long rpk_wdl_resample_all(
    const double *input,
    long long input_frames,
    int channels,
    double input_rate,
    double output_rate,
    double *output,
    long long output_capacity_frames) noexcept;

namespace {

constexpr int kInvalidArgument = -1;
constexpr std::size_t kFftSize = 1024;
constexpr int kChannels = 2;
constexpr long long kInputFrames = 4096;
constexpr long long kOutputFrames = 5000;

struct Buffers {
    std::array<double, kFftSize> fft_input{};
    std::array<double, kFftSize / 2 + 1> fft_re{};
    std::array<double, kFftSize / 2 + 1> fft_im{};
    std::vector<double> input;
    std::vector<double> output;

    Buffers()
        : input(static_cast<std::size_t>(kInputFrames) * kChannels),
          output(static_cast<std::size_t>(kOutputFrames) * kChannels) {
        constexpr double pi = 3.141592653589793238462643383279502884;
        for (std::size_t i = 0; i < fft_input.size(); ++i) {
            fft_input[i] = 0.4 * std::sin(2.0 * pi * 37.0 * static_cast<double>(i) /
                                           static_cast<double>(kFftSize)) +
                           0.2 * std::cos(2.0 * pi * 113.0 * static_cast<double>(i) /
                                           static_cast<double>(kFftSize));
        }
        for (long long frame = 0; frame < kInputFrames; ++frame) {
            const double t = static_cast<double>(frame) / 48000.0;
            input[static_cast<std::size_t>(frame) * kChannels] =
                0.5 * std::sin(2.0 * pi * 997.0 * t);
            input[static_cast<std::size_t>(frame) * kChannels + 1] =
                0.25 * std::cos(2.0 * pi * 613.0 * t);
        }
    }
};

void check_finite_fft(const Buffers &buffers) {
    for (double value : buffers.fft_re) assert(std::isfinite(value));
    for (double value : buffers.fft_im) assert(std::isfinite(value));
}

void exercise_valid_paths() {
    Buffers buffers;
    const int fft_rc = rpk_wdl_real_fft_1024(
        buffers.fft_input.data(), buffers.fft_re.data(), buffers.fft_im.data());
    assert(fft_rc == 0);
    check_finite_fft(buffers);

    const long long resampled = rpk_wdl_resample_all(
        buffers.input.data(),
        kInputFrames,
        kChannels,
        48000.0,
        44100.0,
        buffers.output.data(),
        kOutputFrames);
    assert(resampled > 0);
    assert(resampled <= kOutputFrames);
    const auto sample_count = static_cast<std::size_t>(resampled) * kChannels;
    for (std::size_t i = 0; i < sample_count; ++i) {
        assert(std::isfinite(buffers.output[i]));
    }
}

void exercise_invalid_boundaries() {
    Buffers buffers;
    assert(rpk_wdl_real_fft_1024(nullptr, buffers.fft_re.data(), buffers.fft_im.data()) ==
           kInvalidArgument);
    assert(rpk_wdl_real_fft_1024(buffers.fft_input.data(), nullptr, buffers.fft_im.data()) ==
           kInvalidArgument);
    assert(rpk_wdl_real_fft_1024(buffers.fft_input.data(), buffers.fft_re.data(), nullptr) ==
           kInvalidArgument);

    auto call = [&](const double *input,
                    long long input_frames,
                    int channels,
                    double input_rate,
                    double output_rate,
                    double *output,
                    long long output_frames) {
        return rpk_wdl_resample_all(
            input,
            input_frames,
            channels,
            input_rate,
            output_rate,
            output,
            output_frames);
    };

    assert(call(nullptr, 1, 1, 48000.0, 44100.0, buffers.output.data(), 1) ==
           kInvalidArgument);
    assert(call(buffers.input.data(), 1, 1, 48000.0, 44100.0, nullptr, 1) ==
           kInvalidArgument);
    assert(call(buffers.input.data(), -1, 1, 48000.0, 44100.0, buffers.output.data(), 1) ==
           kInvalidArgument);
    assert(call(buffers.input.data(), 1, 0, 48000.0, 44100.0, buffers.output.data(), 1) ==
           kInvalidArgument);
    assert(call(buffers.input.data(), 1, 256, 48000.0, 44100.0, buffers.output.data(), 1) ==
           kInvalidArgument);
    assert(call(buffers.input.data(), 1, 1, 0.0, 44100.0, buffers.output.data(), 1) ==
           kInvalidArgument);
    assert(call(buffers.input.data(), 1, 1, 48000.0, 0.0, buffers.output.data(), 1) ==
           kInvalidArgument);
    assert(call(buffers.input.data(), 1, 1,
                std::numeric_limits<double>::quiet_NaN(), 44100.0,
                buffers.output.data(), 1) == kInvalidArgument);
    assert(call(buffers.input.data(), 1, 1, 48000.0,
                std::numeric_limits<double>::infinity(), buffers.output.data(), 1) ==
           kInvalidArgument);
    assert(call(buffers.input.data(), std::numeric_limits<long long>::max(), 255,
                48000.0, 44100.0, buffers.output.data(), 1) == kInvalidArgument);
    assert(call(buffers.input.data(), 1, 255, 48000.0, 44100.0,
                buffers.output.data(), std::numeric_limits<long long>::max()) ==
           kInvalidArgument);
}

void exercise_parallel_paths() {
    constexpr int threads = 24;
    constexpr int iterations = 24;
    std::atomic<int> ready{0};
    std::atomic<bool> start{false};
    std::vector<std::thread> workers;
    workers.reserve(threads);
    for (int thread_index = 0; thread_index < threads; ++thread_index) {
        workers.emplace_back([&] {
            ready.fetch_add(1, std::memory_order_release);
            while (!start.load(std::memory_order_acquire)) {
                std::this_thread::yield();
            }
            for (int iteration = 0; iteration < iterations; ++iteration) {
                exercise_valid_paths();
            }
        });
    }
    while (ready.load(std::memory_order_acquire) != threads) {
        std::this_thread::yield();
    }
    start.store(true, std::memory_order_release);
    for (auto &worker : workers) worker.join();
}

} // namespace

int main(int argc, char **argv) {
    exercise_invalid_boundaries();
    exercise_valid_paths();
    if (argc > 1 && std::string(argv[1]) == "--threads") {
        exercise_parallel_paths();
    }
    std::cout << "STRICT_WDL_NATIVE_SANITIZER_OK\n";
    return 0;
}
