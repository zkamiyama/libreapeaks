use crate::ffi::{RpkBuffer, RpkHandle};
use crate::rpkx::{
    append_rpkx_chunk, read_rpkx, remove_rpkx_chunks, set_rpkx_chunk, strip_rpkx, RpkxChunk,
    RpkxKey,
};
use std::ptr;

#[repr(C)]
pub struct RpkxChunkInfo {
    pub namespace_id: [u8; 16],
    pub kind: [u8; 4],
    pub version: u32,
    pub flags: u32,
    pub payload_len: usize,
}

fn buffer_from_vec(mut value: Vec<u8>) -> RpkBuffer {
    let out = RpkBuffer {
        data: value.as_mut_ptr(),
        len: value.len(),
        capacity: value.capacity(),
    };
    std::mem::forget(value);
    out
}

unsafe fn read_key(namespace_id: *const u8, kind: *const u8) -> Option<RpkxKey> {
    if namespace_id.is_null() || kind.is_null() {
        return None;
    }
    let mut namespace = [0u8; 16];
    let mut fourcc = [0u8; 4];
    ptr::copy_nonoverlapping(namespace_id, namespace.as_mut_ptr(), namespace.len());
    ptr::copy_nonoverlapping(kind, fourcc.as_mut_ptr(), fourcc.len());
    Some(RpkxKey::new(namespace, fourcc))
}

#[no_mangle]
pub unsafe extern "C" fn rpk_rpkx_chunk_count(h: *const RpkHandle) -> usize {
    let Some(h) = h.as_ref() else { return 0 };
    match read_rpkx(&h.file.raw) {
        Ok(Some(container)) => container.chunks.len(),
        _ => 0,
    }
}

#[no_mangle]
pub unsafe extern "C" fn rpk_rpkx_get_chunk_info(
    h: *const RpkHandle,
    index: usize,
    out: *mut RpkxChunkInfo,
) -> i32 {
    let (Some(h), Some(out)) = (h.as_ref(), out.as_mut()) else {
        return -1;
    };
    let Ok(Some(container)) = read_rpkx(&h.file.raw) else {
        return -2;
    };
    let Some(chunk) = container.chunks.get(index) else {
        return -3;
    };
    *out = RpkxChunkInfo {
        namespace_id: chunk.key.namespace,
        kind: chunk.key.kind,
        version: chunk.version,
        flags: chunk.flags,
        payload_len: chunk.payload.len(),
    };
    0
}

#[no_mangle]
pub unsafe extern "C" fn rpk_rpkx_get_chunk_payload(
    h: *const RpkHandle,
    index: usize,
    out: *mut RpkBuffer,
) -> i32 {
    let (Some(h), Some(out)) = (h.as_ref(), out.as_mut()) else {
        return -1;
    };
    let Ok(Some(container)) = read_rpkx(&h.file.raw) else {
        return -2;
    };
    let Some(chunk) = container.chunks.get(index) else {
        return -3;
    };
    *out = buffer_from_vec(chunk.payload.clone());
    0
}

unsafe fn input_bytes<'a>(data: *const u8, len: usize) -> Option<&'a [u8]> {
    if data.is_null() && len != 0 {
        return None;
    }
    Some(if len == 0 {
        &[]
    } else {
        std::slice::from_raw_parts(data, len)
    })
}

#[no_mangle]
pub unsafe extern "C" fn rpk_rpkx_set_chunk(
    reapeaks: *const u8,
    reapeaks_len: usize,
    namespace_id: *const u8,
    kind: *const u8,
    version: u32,
    flags: u32,
    payload: *const u8,
    payload_len: usize,
    out: *mut RpkBuffer,
) -> i32 {
    let (Some(raw), Some(key), Some(payload), Some(out)) = (
        input_bytes(reapeaks, reapeaks_len),
        read_key(namespace_id, kind),
        input_bytes(payload, payload_len),
        out.as_mut(),
    ) else {
        return -1;
    };
    let chunk = RpkxChunk::new(key.namespace, key.kind, version, flags, payload.to_vec());
    match set_rpkx_chunk(raw, chunk) {
        Ok(value) => {
            *out = buffer_from_vec(value);
            0
        }
        Err(_) => -2,
    }
}

#[no_mangle]
pub unsafe extern "C" fn rpk_rpkx_append_chunk(
    reapeaks: *const u8,
    reapeaks_len: usize,
    namespace_id: *const u8,
    kind: *const u8,
    version: u32,
    flags: u32,
    payload: *const u8,
    payload_len: usize,
    out: *mut RpkBuffer,
) -> i32 {
    let (Some(raw), Some(key), Some(payload), Some(out)) = (
        input_bytes(reapeaks, reapeaks_len),
        read_key(namespace_id, kind),
        input_bytes(payload, payload_len),
        out.as_mut(),
    ) else {
        return -1;
    };
    let chunk = RpkxChunk::new(key.namespace, key.kind, version, flags, payload.to_vec());
    match append_rpkx_chunk(raw, chunk) {
        Ok(value) => {
            *out = buffer_from_vec(value);
            0
        }
        Err(_) => -2,
    }
}

#[no_mangle]
pub unsafe extern "C" fn rpk_rpkx_remove_chunks(
    reapeaks: *const u8,
    reapeaks_len: usize,
    namespace_id: *const u8,
    kind: *const u8,
    out: *mut RpkBuffer,
) -> i32 {
    let (Some(raw), Some(key), Some(out)) = (
        input_bytes(reapeaks, reapeaks_len),
        read_key(namespace_id, kind),
        out.as_mut(),
    ) else {
        return -1;
    };
    match remove_rpkx_chunks(raw, key) {
        Ok(value) => {
            *out = buffer_from_vec(value);
            0
        }
        Err(_) => -2,
    }
}

#[no_mangle]
pub unsafe extern "C" fn rpk_rpkx_strip(
    reapeaks: *const u8,
    reapeaks_len: usize,
    out: *mut RpkBuffer,
) -> i32 {
    let (Some(raw), Some(out)) = (input_bytes(reapeaks, reapeaks_len), out.as_mut()) else {
        return -1;
    };
    match strip_rpkx(raw) {
        Ok(value) => {
            *out = buffer_from_vec(value);
            0
        }
        Err(_) => -2,
    }
}
