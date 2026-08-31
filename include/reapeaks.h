#ifndef LIBREAPEAKS_H
#define LIBREAPEAKS_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct RpkHandle RpkHandle;

enum {
  RPK_WAVE_ENCODING_UNKNOWN = 0,
  RPK_WAVE_ENCODING_RPKN = 1,
  RPK_WAVE_ENCODING_RPKL = 2
};

typedef struct RpkBuffer {
  uint8_t *data;
  size_t len;
  size_t capacity;
} RpkBuffer;

typedef struct RpkLevelInfo {
  uint64_t division;
  size_t peak_count;
  uint8_t native;
} RpkLevelInfo;

typedef struct RpkViewPlan {
  size_t level_index;
  uint64_t division;
  size_t first_peak;
  size_t peak_count;
  double peaks_per_pixel;
} RpkViewPlan;

int32_t rpk_open(const char *path, RpkHandle **out);
void rpk_close(RpkHandle *h);
uint8_t rpk_wave_encoding(const RpkHandle *h);

size_t rpk_level_count(const RpkHandle *h);
int32_t rpk_get_level_info(const RpkHandle *h, size_t index, RpkLevelInfo *out);
int32_t rpk_plan_view(const RpkHandle *h, uint64_t start_frame, uint64_t end_frame,
                      size_t pixel_width, RpkViewPlan *out);

/* Fixed-size tile interface intended for GUI LRU/GPU caches. */
size_t rpk_tile_peaks(const RpkHandle *h);
size_t rpk_tile_count(const RpkHandle *h, size_t level_index);
int32_t rpk_tile_texture_rgba8(const RpkHandle *h, size_t level_index,
                               uint64_t tile_index, size_t *out_first_peak,
                               size_t *out_width, size_t *out_height,
                               RpkBuffer *out);

/* Materializes an entire level. Prefer tiles for long sources. */
int32_t rpk_level_texture_rgba8(const RpkHandle *h, size_t level_index,
                                size_t *out_width, size_t *out_height,
                                RpkBuffer *out);

/* Spectral peak data texture: one little-endian 32-bit spectral code per texel. */
size_t rpk_spectral_layer_count(const RpkHandle *h);
int32_t rpk_spectral_tile_texture_rgba8(const RpkHandle *h,
                                        size_t spectral_layer_index,
                                        uint64_t tile_index,
                                        size_t *out_first_peak,
                                        size_t *out_width,
                                        size_t *out_height,
                                        RpkBuffer *out);

/* CPU RGBA renderer. Colors are byte-ordered RGBA packed into a little-endian u32. */
int32_t rpk_render_rgba8(const RpkHandle *h, size_t width, size_t height,
                         uint64_t start_frame, uint64_t end_frame,
                         uint32_t background_rgba_le, uint32_t waveform_rgba_le,
                         RpkBuffer *out);
int32_t rpk_render_rgba8_scaled(const RpkHandle *h, size_t width, size_t height,
                                uint64_t start_frame, uint64_t end_frame,
                                float vertical_full_scale,
                                uint32_t background_rgba_le,
                                uint32_t waveform_rgba_le,
                                RpkBuffer *out);

/* Calculate REAPER-style three-level divisions for peakcachegenrs. */
int32_t rpk_default_divisions(uint32_t sample_rate,
                              uint32_t fine_peaks_per_second,
                              uint32_t out_divisions[3]);

/* RPKN generator for decoded PCM16. */
int32_t rpk_generate_pcm16(const int16_t *pcm, size_t frames, size_t channels,
                           uint32_t sample_rate, const uint32_t *divisions,
                           size_t division_count, uint32_t source_mtime_low32,
                           uint32_t source_size_low32, uint8_t spectral,
                           RpkBuffer *out);

/* Float generator. large_range=1 writes RPKL; 0 writes RPKN. */
int32_t rpk_generate_f32(const float *pcm, size_t frames, size_t channels,
                         uint32_t sample_rate, const uint32_t *divisions,
                         size_t division_count, uint32_t source_mtime_low32,
                         uint32_t source_size_low32, uint8_t large_range,
                         uint8_t spectral, RpkBuffer *out);

void rpk_buffer_free(RpkBuffer *buf);

#ifdef __cplusplus
}
#endif
#endif
