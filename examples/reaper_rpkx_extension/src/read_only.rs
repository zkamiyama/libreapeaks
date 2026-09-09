//! Read-only validation used by the REAPER host while it may already have the
//! peak cache open. Mutating WAL recovery is deliberately deferred to a writer.
use crate::store;
use reapeaks::{scan_rpkx,standard_end_reader};
use std::fs::File;
use std::io::{self,Read,Seek,SeekFrom};
use std::path::Path;

fn bad(s:impl Into<String>)->io::Error{io::Error::new(io::ErrorKind::InvalidData,s.into())}
fn logical_end(file:&mut File)->io::Result<(u64,u64)>{
    let len=file.metadata()?.len();
    if len==0{return Ok((0,0));}
    let std=standard_end_reader(file).map_err(|e|bad(e.to_string()))?;
    if std>len{return Err(bad("truncated standard"));}
    let index=scan_rpkx(file).map_err(|e|bad(e.to_string()))?;
    let end=match index{Some(i)=>std.checked_add(i.container_len).ok_or_else(||bad("offset overflow"))?,None=>std};
    Ok((std,end))
}

pub fn read_standard(path:&Path)->io::Result<Vec<u8>>{
    let _lock=store::lock_file(path)?;
    let mut file=File::open(path)?;
    let len=file.metadata()?.len();
    let (std,end)=logical_end(&mut file)?;
    if end!=len{return Err(bad("pending transaction or unsupported suffix; refusing read until a writer can recover it"));}
    if std>store::MAX_STANDARD{return Err(bad("standard exceeds memory budget"));}
    let mut out=vec![0;std as usize];
    file.seek(SeekFrom::Start(0))?;file.read_exact(&mut out)?;Ok(out)
}

#[cfg(test)]
mod tests{
    use super::*;
    use std::sync::atomic::{AtomicU64,Ordering};
    static ID:AtomicU64=AtomicU64::new(0);
    struct Dir(std::path::PathBuf);
    impl Dir{fn new()->Self{let p=std::env::temp_dir().join(format!("lrpk-read-test-{}-{}",std::process::id(),ID.fetch_add(1,Ordering::SeqCst)));std::fs::create_dir(&p).unwrap();Self(p)}}
    impl Drop for Dir{fn drop(&mut self){let _=std::fs::remove_dir_all(&self.0);}}
    fn standard()->Vec<u8>{reapeaks::generate_pcm16(&vec![1234;4801],&reapeaks::GenerateOptions{sample_rate:48000,channels:1,divisions:vec![160],source_mtime_low32:1,source_size_low32:9999,spectral:false}).unwrap()}
    #[test]
    fn reads_packed_cache_without_write_recovery(){
        let d=Dir::new();let p=d.0.join("a.reapeaks");let s=standard();
        let packed=reapeaks::set_rpkx_chunk(&s,reapeaks::RpkxChunk::new([7;16],*b"TEST",1,0,vec![9;8192])).unwrap();
        std::fs::write(&p,&packed).unwrap();assert_eq!(read_standard(&p).unwrap(),s);assert_eq!(std::fs::read(&p).unwrap(),packed);
    }
    #[test]
    fn refuses_suffix_without_mutating(){
        let d=Dir::new();let p=d.0.join("a.reapeaks");let s=standard();let mut packed=reapeaks::set_rpkx_chunk(&s,reapeaks::RpkxChunk::new([7;16],*b"TEST",1,0,vec![9;512])).unwrap();
        packed.extend_from_slice(b"pending-or-unknown");std::fs::write(&p,&packed).unwrap();assert!(read_standard(&p).is_err());assert_eq!(std::fs::read(&p).unwrap(),packed);
    }
}