//! Nonblocking coordination for the host's read-only peak reader.
use std::ffi::{c_char,c_void};
use std::ptr;
use crate::{guard,path,err};
#[no_mangle]
pub unsafe extern "C" fn lrpk_try_read_guard(p:*const c_char)->*mut c_void{
    let mut out=ptr::null_mut();
    guard(||{
        let p=path(p)?;
        let f=std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).open(reapeaks::rpkx_file_lock_path(p))?;
        f.try_lock_shared().map_err(err)?;
        let mut source=std::fs::File::open(p)?;
        let n=source.metadata()?.len();
        let s=reapeaks::standard_end_reader(&mut source).map_err(err)?;
        let idx=reapeaks::scan_rpkx(&mut source).map_err(err)?;
        let end=if let Some(idx)=idx{s.checked_add(idx.container_len).ok_or_else(||err("size overflow"))?}else{s};
        if end!=n{return Err(err("pending transaction or unsupported suffix"));}
        out=Box::into_raw(Box::new(f)).cast();Ok(())
    });out
}
#[no_mangle]
pub unsafe extern "C" fn lrpk_release_read_guard(p:*mut c_void){if !p.is_null(){drop(Box::from_raw(p.cast::<std::fs::File>()));}}
