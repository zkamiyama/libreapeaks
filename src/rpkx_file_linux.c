#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64

#include <errno.h>
#include <linux/fs.h>
#include <stdint.h>
#include <sys/ioctl.h>
#include <sys/types.h>
#include <unistd.h>

int rpk_linux_ficlone(int dst_fd, int src_fd) {
    return ioctl(dst_fd, FICLONE, src_fd);
}

int rpk_linux_ficlonerange(
    int dst_fd,
    int src_fd,
    uint64_t src_offset,
    uint64_t src_length,
    uint64_t dst_offset) {
    struct file_clone_range range;
    range.src_fd = src_fd;
    range.src_offset = src_offset;
    range.src_length = src_length;
    range.dest_offset = dst_offset;
    return ioctl(dst_fd, FICLONERANGE, &range);
}

ssize_t rpk_linux_copy_file_range_once(
    int src_fd,
    uint64_t src_offset,
    int dst_fd,
    uint64_t dst_offset,
    size_t length) {
    off_t src = (off_t)src_offset;
    off_t dst = (off_t)dst_offset;
    return copy_file_range(src_fd, &src, dst_fd, &dst, length, 0);
}
