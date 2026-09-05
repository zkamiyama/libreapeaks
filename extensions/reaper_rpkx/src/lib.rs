#![allow(clippy::missing_safety_doc)]
pub mod store;
mod read_guard;
mod read_only;
use std::cell::RefCell;
use std::ffi::{c_char,c_void,CStr};
use std::io;
use std::path::Path;
use std::ptr;
use reapeaks::{GenerateOptions,ReaperPeakMode,SourceStamp};

thread_local! {static ERROR:RefCell<String>=const {RefCell::new(String::new())};}
#[repr(C)]
pub struct Buffer{pub data:*mut u8,pub len:usize,pub capacity:usize}
impl Buffer{
    fn take(mut v:Vec<u8>)->Self{let r=Self{data:v.as_mut_ptr(),len:v.len(),capacity:v.capacity()};std::mem::forget(v);r}
    fn empty()->Self{Self{data:ptr::null_mut(),len:0,capacity:0}}
}
fn guard(f:impl FnOnce()->io::Result<()>)->i32{
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)){
        Ok(Ok(()))=>{ERROR.with(|s|s.borrow_mut().clear());0},
        Ok(Err(e))=>{ERROR.with(|s|*s.borrow_mut()=e.to_string());-1},
        Err(_)=>{ERROR.with(|s|*s.borrow_mut()="panic contained at C ABI".into());-2},
    }
}
fn err(s:impl ToString)->io::Error{io::Error::other(s.to_string())}
unsafe fn path<'a>(p:*const c_char)->io::Result<&'a Path>{
    if p.is_null(){return Err(err("null path"));}
    Ok(Path::new(CStr::from_ptr(p).to_str().map_err(err)?))
}
unsafe fn bytes<'a>(p:*const u8,n:usize)->io::Result<&'a [u8]>{
    if p.is_null() || n>isize::MAX as usize{return Err(err("invalid buffer"));}
    Ok(std::slice::from_raw_parts(p,n))
}
#[no_mangle]
pub unsafe extern "C" fn lrpk_last_error(out:*mut c_char,cap:usize)->usize{
    ERROR.with(|s|{let s=s.borrow();let b=s.as_bytes();if !out.is_null()&&cap>0{let n=b.len().min(cap-1);ptr::copy_nonoverlapping(b.as_ptr(),out.cast(),n);*out.add(n)=0;}b.len()})
}
#[no_mangle]
pub unsafe extern "C" fn lrpk_free(b:*mut Buffer){if let Some(b)=b.as_mut(){if !b.data.is_null(){drop(Vec::from_raw_parts(b.data,b.len,b.capacity));}*b=Buffer::empty();}}
#[no_mangle]
pub unsafe extern "C" fn lrpk_stamp(p:*const c_char,mtime:*mut u32,size:*mut u32)->i32{
    guard(||{if mtime.is_null()||size.is_null(){return Err(err("null stamp output"));}let s=SourceStamp::from_path(path(p)?).map_err(err)?;*mtime=s.mtime_low32;*size=s.size_low32;Ok(())})
}
#[no_mangle]
pub unsafe extern "C" fn lrpk_read_standard(p:*const c_char,out:*mut Buffer)->i32{
    if out.is_null(){return -1;}*out=Buffer::empty();guard(||{*out=Buffer::take(read_only::read_standard(path(p)?)?);Ok(())})
}
#[no_mangle]
pub unsafe extern "C" fn lrpk_recover(p:*const c_char)->i32{guard(||{store::recover(path(p)?)?;Ok(())})}
#[no_mangle]
pub unsafe extern "C" fn lrpk_replace(p:*const c_char,data:*const u8,len:usize,preserve_stale:u8,out:*mut store::Report)->i32{
    if out.is_null(){return -1;}*out=store::Report::default();guard(||{*out=store::replace(path(p)?,bytes(data,len)?,preserve_stale!=0)?;Ok(())})
}
/// PCM format 0=i16, 1=f32->RPKN, 2=f32->RPKL. Input ownership stays with caller.
/// This first plugin version intentionally uses the tested batch analysis core;
/// decoded input is capped, and is NOT advertised as a constant-memory DSP path.
#[no_mangle]
pub unsafe extern "C" fn lrpk_generate(pcm:*const c_void,frames:usize,channels:u32,rate:u32,pps:u32,mtime:u32,size:u32,format:u8,mode:u8,out:*mut Buffer)->i32{
    if out.is_null(){return -1;}*out=Buffer::empty();
    guard(||{
        let n=frames.checked_mul(channels as usize).ok_or_else(||err("PCM size overflow"))?;
        let sample_size=if format==0{2}else{4};
        if pcm.is_null()||format>2||channels==0||channels>32||rate==0||pps==0||n>256*1024*1024/sample_size||(pcm as usize)%sample_size!=0{return Err(err("unsupported PCM geometry or memory budget exceeded"));}
        let options=GenerateOptions{sample_rate:rate,channels:channels as usize,divisions:reapeaks::default_divisions(rate,pps).to_vec(),source_mtime_low32:mtime,source_size_low32:size,spectral:false};
        let mode=ReaperPeakMode::try_from(mode).map_err(err)?;
        let data=if format==0{reapeaks::generate_pcm16_reaper(std::slice::from_raw_parts(pcm.cast::<i16>(),n),&options,mode)}else{reapeaks::generate_f32_reaper(std::slice::from_raw_parts(pcm.cast::<f32>(),n),&options,format==2,mode)}.map_err(err)?;
        if data.len() as u64>store::MAX_STANDARD{return Err(err("standard exceeds memory budget"));}
        *out=Buffer::take(data);Ok(())
    })
}