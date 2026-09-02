use crate::ffi::RpkHandle;
use crate::source::SourceStamp;
use std::ffi::{c_char, CStr};

#[repr(C)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RpkSourceStamp {
    pub source_mtime_low32: u32,
    pub source_size_low32: u32,
}

impl From<SourceStamp> for RpkSourceStamp {
    fn from(value: SourceStamp) -> Self {
        Self {
            source_mtime_low32: value.mtime_low32,
            source_size_low32: value.size_low32,
        }
    }
}

impl From<RpkSourceStamp> for SourceStamp {
    fn from(value: RpkSourceStamp) -> Self {
        Self::new(value.source_mtime_low32, value.source_size_low32)
    }
}

/// Build the REAPER-compatible source stamp from a filesystem path.
#[no_mangle]
pub unsafe extern "C" fn rpk_source_stamp_from_path(
    path: *const c_char,
    out: *mut RpkSourceStamp,
) -> i32 {
    let Some(out) = out.as_mut() else {
        return -1;
    };
    if path.is_null() {
        return -1;
    }
    let Ok(path) = CStr::from_ptr(path).to_str() else {
        return -2;
    };
    let Ok(stamp) = SourceStamp::from_path(path) else {
        return -3;
    };
    *out = stamp.into();
    0
}

/// Build the REAPER-compatible source stamp from whole Unix seconds and size.
#[no_mangle]
pub unsafe extern "C" fn rpk_source_stamp_from_unix_seconds(
    mtime_seconds: i64,
    size: u64,
    out: *mut RpkSourceStamp,
) -> i32 {
    let Some(out) = out.as_mut() else {
        return -1;
    };
    *out = SourceStamp::from_unix_seconds_and_size(mtime_seconds, size).into();
    0
}

/// Read the source stamp embedded in an open cache.
#[no_mangle]
pub unsafe extern "C" fn rpk_get_source_stamp(
    h: *const RpkHandle,
    out: *mut RpkSourceStamp,
) -> i32 {
    let (Some(h), Some(out)) = (h.as_ref(), out.as_mut()) else {
        return -1;
    };
    *out = h.file.source_stamp().into();
    0
}

/// Compare an open cache with an already captured stamp. Returns 1/0 or <0 on error.
#[no_mangle]
pub unsafe extern "C" fn rpk_matches_source_stamp(
    h: *const RpkHandle,
    stamp: *const RpkSourceStamp,
) -> i32 {
    let (Some(h), Some(stamp)) = (h.as_ref(), stamp.as_ref()) else {
        return -1;
    };
    if h.file.matches_source_stamp((*stamp).into()) {
        1
    } else {
        0
    }
}

/// Stat a source path and compare it with an open cache. Returns 1/0 or <0 on error.
#[no_mangle]
pub unsafe extern "C" fn rpk_matches_source(
    h: *const RpkHandle,
    path: *const c_char,
) -> i32 {
    let Some(h) = h.as_ref() else {
        return -1;
    };
    if path.is_null() {
        return -1;
    }
    let Ok(path) = CStr::from_ptr(path).to_str() else {
        return -2;
    };
    match h.file.matches_source_path(path) {
        Ok(true) => 1,
        Ok(false) => 0,
        Err(_) => -3,
    }
}
