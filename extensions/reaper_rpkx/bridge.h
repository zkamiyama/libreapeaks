#pragma once
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
#include <limits>
extern "C" {
#endif
struct LrpkBuffer{uint8_t*data;size_t len;size_t capacity;};
struct LrpkReport{uint64_t standard_bytes_written,tail_bytes_moved,journal_bytes_written,syncs,recovered;};
struct LrpkI16Extrema{int16_t max,min;};
size_t lrpk_last_error(char*,size_t);
void lrpk_free(struct LrpkBuffer*);
int32_t lrpk_stamp(const char*,uint32_t*,uint32_t*);
int32_t lrpk_read_standard(const char*,struct LrpkBuffer*);
int32_t lrpk_recover(const char*);
int32_t lrpk_replace(const char*,const uint8_t*,size_t,uint8_t,struct LrpkReport*);
int32_t lrpk_generate(const void*,size_t,uint32_t,uint32_t,uint32_t,uint32_t,uint32_t,uint8_t,uint8_t,struct LrpkBuffer*);
int32_t lrpk_generate_wave_pcm16(const struct LrpkI16Extrema*,size_t,size_t,uint32_t,uint32_t,uint32_t,uint32_t,uint32_t,struct LrpkBuffer*);
void* lrpk_try_read_guard(const char*);
void lrpk_release_read_guard(void*);
#ifdef __cplusplus
}
#endif