#pragma once
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
struct LrpkBuffer{uint8_t*data;size_t len;size_t capacity;};
struct LrpkReport{uint64_t standard_bytes_written,tail_bytes_moved,journal_bytes_written,syncs,recovered;};
size_t lrpk_last_error(char*,size_t);
void lrpk_free(struct LrpkBuffer*);
int32_t lrpk_stamp(const char*,uint32_t*,uint32_t*);
int32_t lrpk_read_standard(const char*,struct LrpkBuffer*);
int32_t lrpk_recover(const char*);
int32_t lrpk_replace(const char*,const uint8_t*,size_t,uint8_t,struct LrpkReport*);
int32_t lrpk_generate(const void*,size_t,uint32_t,uint32_t,uint32_t,uint32_t,uint32_t,uint8_t,uint8_t,struct LrpkBuffer*);
#ifdef __cplusplus
}
#endif
