//! A bounded-memory, same-file redo transaction for replacing a standard prefix.
//!
//! The completed file is still the unmodified packed RPKX v1 format. A temporary
//! *region*, not a temporary file, holds the new standard and one relocation
//! block. The existing persistent .rpkx.lock coordinates legacy file updaters.
//! In-flight journals must be recovered before any other mutation. Arbitrary EOF
//! suffixes are deliberately rejected, never guessed or silently discarded.
use reapeaks::{scan_rpkx, standard_end, standard_end_reader};
use sha2::{Digest, Sha256};
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;

pub const SLOT: u64 = 4096;
pub const BLOCK: u64 = 1024 * 1024;
pub const MAX_STANDARD: u64 = 256 * 1024 * 1024;
const INIT: &[u8; 8] = b"LRPKINI1";
const WAL: &[u8; 8] = b"LRPKWAL1";
const STATE: &[u8; 8] = b"LRPKST01";
const FAST_INIT: &[u8; 8] = b"LRPKIF01";
const FAST_WAL: &[u8; 8] = b"LRPKWF01";
fn bad(s: impl Into<String>) -> io::Error { io::Error::new(io::ErrorKind::InvalidData, s.into()) }
fn sum(a: u64, b: u64) -> io::Result<u64> { a.checked_add(b).ok_or_else(|| bad("offset overflow")) }
fn align(n: u64) -> io::Result<u64> { Ok(sum(n, SLOT - 1)? / SLOT * SLOT) }
fn hash(b: &[u8]) -> [u8; 32] { Sha256::digest(b).into() }
fn put(b: &mut [u8], off: usize, n: u64) { b[off..off+8].copy_from_slice(&n.to_le_bytes()); }
fn get(b: &[u8], off: usize) -> u64 { u64::from_le_bytes(b[off..off+8].try_into().unwrap()) }
fn seal(b: &mut [u8]) { let n = b.len()-32; let h = hash(&b[..n]); b[n..].copy_from_slice(&h); }
fn valid(b: &[u8], magic: &[u8; 8]) -> bool { b.len() == SLOT as usize && &b[..8] == magic && hash(&b[..b.len()-32]) == b[b.len()-32..] }

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct Report {
    pub standard_bytes_written: u64,
    pub tail_bytes_moved: u64,
    pub journal_bytes_written: u64,
    pub syncs: u64,
    pub recovered: u64,
}

// Unit tests inject abrupt stops after individual writes/syncs, including torn
// writes. Production never reads fault-injection environment variables.
struct Io<'a> {
    file: &'a mut File,
    report: Report,
    stop: Option<usize>,
    torn: bool,
}
impl Io<'_> {
    fn checkpoint(&mut self) -> io::Result<()> {
        if let Some(n) = &mut self.stop {
            if *n == 0 { return Err(io::Error::other("injected stop")); }
            *n -= 1;
        }
        Ok(())
    }
    fn read(&mut self, off: u64, n: usize) -> io::Result<Vec<u8>> {
        let mut b = vec![0; n];
        self.file.seek(SeekFrom::Start(off))?;
        self.file.read_exact(&mut b)?;
        Ok(b)
    }
    fn write(&mut self, off: u64, b: &[u8], journal: bool) -> io::Result<()> {
        self.file.seek(SeekFrom::Start(off))?;
        if self.stop == Some(0) && self.torn && !b.is_empty() {
            self.file.write_all(&b[..(b.len()/2).max(1)])?;
            return Err(io::Error::other("injected torn write"));
        }
        self.file.write_all(b)?;
        if journal { self.report.journal_bytes_written += b.len() as u64; }
        self.checkpoint()
    }
    fn sync(&mut self) -> io::Result<()> {
        self.file.sync_all()?;
        self.report.syncs += 1;
        self.checkpoint()
    }
    fn truncate(&mut self, n: u64) -> io::Result<()> {
        self.file.set_len(n)?;
        self.checkpoint()?;
        self.sync()
    }
}

#[derive(Clone, Debug)]
struct FastPlan {
    old_len: u64, old_std: u64, new_std: u64, stage: u64, footer: u64,
    new_hash: [u8; 32],
}
impl FastPlan {
    fn new(old_len:u64, old_std:u64, new:&[u8]) -> io::Result<Self> {
        let new_std=new.len() as u64;
        if new_std!=old_std || new_std<18 || new_std>MAX_STANDARD {return Err(bad("same-size redo geometry"));}
        let stage=align(sum(old_len,SLOT)?)?;
        let footer=align(sum(stage,new_std)?)?;
        Ok(Self{old_len,old_std,new_std,stage,footer,new_hash:hash(new)})
    }
    fn encode(&self,magic:&[u8;8])->Vec<u8>{
        let mut b=vec![0;SLOT as usize];b[..8].copy_from_slice(magic);
        for (i,v) in [self.old_len,self.old_std,self.new_std,self.stage,self.footer].iter().enumerate(){put(&mut b,8+i*8,*v);}
        b[48..80].copy_from_slice(&self.new_hash);seal(&mut b);b
    }
    fn decode(b:&[u8],magic:&[u8;8])->io::Result<Self>{
        if !valid(b,magic){return Err(bad("invalid same-size redo descriptor"));}
        let p=Self{old_len:get(b,8),old_std:get(b,16),new_std:get(b,24),stage:get(b,32),footer:get(b,40),new_hash:b[48..80].try_into().unwrap()};
        if p.old_std>p.old_len || p.old_std!=p.new_std || p.new_std<18 || p.new_std>MAX_STANDARD{return Err(bad("same-size redo geometry"));}
        if p.stage!=align(sum(p.old_len,SLOT)?)? || p.footer!=align(sum(p.stage,p.new_std)?)?{return Err(bad("same-size redo offsets"));}
        Ok(p)
    }
}

#[derive(Clone, Debug)]
struct Plan {
    old_len: u64, old_std: u64, new_std: u64, stage: u64, scratch: u64,
    slot_a: u64, footer: u64, new_hash: [u8; 32],
}
impl Plan {
    fn new(old_len: u64, old_std: u64, new: &[u8]) -> io::Result<Self> {
        let new_std = new.len() as u64;
        let final_len = sum(new_std, old_len-old_std)?;
        let stage = align(sum(old_len, SLOT)?.max(final_len))?;
        let scratch = align(sum(stage, new_std)?)?;
        let slot_a = sum(scratch, BLOCK)?;
        let footer = sum(slot_a, 2*SLOT)?;
        Ok(Self { old_len, old_std, new_std, stage, scratch, slot_a, footer, new_hash: hash(new) })
    }
    fn tail(&self) -> u64 { self.old_len-self.old_std }
    fn final_len(&self) -> u64 { self.new_std+self.tail() }
    fn encode(&self, magic: &[u8;8]) -> Vec<u8> {
        let mut b = vec![0; SLOT as usize]; b[..8].copy_from_slice(magic);
        for (i, v) in [self.old_len,self.old_std,self.new_std,self.stage,self.scratch,self.slot_a,self.footer].iter().enumerate() { put(&mut b,8+i*8,*v); }
        b[64..96].copy_from_slice(&self.new_hash); seal(&mut b); b
    }
    fn decode(b: &[u8], magic: &[u8;8]) -> io::Result<Self> {
        if !valid(b,magic) { return Err(bad("invalid redo descriptor")); }
        let p = Self { old_len:get(b,8),old_std:get(b,16),new_std:get(b,24),stage:get(b,32),scratch:get(b,40),slot_a:get(b,48),footer:get(b,56),new_hash:b[64..96].try_into().unwrap() };
        if p.old_std>p.old_len || p.new_std<18 || p.new_std>MAX_STANDARD { return Err(bad("redo geometry")); }
        let end = sum(p.new_std,p.old_len-p.old_std)?;
        if p.stage != align(sum(p.old_len,SLOT)?.max(end))? || p.scratch != align(sum(p.stage,p.new_std)?)? || p.slot_a != sum(p.scratch,BLOCK)? || p.footer != sum(p.slot_a,2*SLOT)? { return Err(bad("redo offsets")); }
        Ok(p)
    }
}

#[derive(Clone, Debug, Default)]
struct Progress { seq:u64, moved:u64, pending:u64, dest:u64, digest:[u8;32], phase:u64 }
impl Progress {
    fn encode(&self) -> Vec<u8> {
        let mut b=vec![0;SLOT as usize]; b[..8].copy_from_slice(STATE);
        for (i,v) in [self.seq,self.moved,self.pending,self.dest,self.phase].iter().enumerate() { put(&mut b,8+i*8,*v); }
        b[48..80].copy_from_slice(&self.digest); seal(&mut b); b
    }
    fn decode(b:&[u8], p:&Plan) -> Option<Self> {
        if !valid(b,STATE) {return None;}
        let s=Self{seq:get(b,8),moved:get(b,16),pending:get(b,24),dest:get(b,32),phase:get(b,40),digest:b[48..80].try_into().ok()?};
        if s.seq==0 || s.phase>2 || s.moved>p.tail() || s.pending>BLOCK || s.moved.checked_add(s.pending)?>p.tail() {return None;}
        if s.pending>0 {
            let pos=if p.new_std>p.old_std {p.tail()-s.moved-s.pending} else {s.moved};
            if s.phase!=0 || s.dest!=p.new_std.checked_add(pos)? {return None;}
        }
        Some(s)
    }
    fn save(&mut self, io:&mut Io<'_>, p:&Plan) -> io::Result<()> {
        self.seq=self.seq.checked_add(1).ok_or_else(||bad("sequence overflow"))?;
        io.write(p.slot_a+(self.seq%2)*SLOT,&self.encode(),true)?; io.sync()
    }
}

fn apply_fast(io:&mut Io<'_>,p:&FastPlan)->io::Result<()>{
    let image=io.read(p.stage,p.new_std as usize)?;
    if hash(&image)!=p.new_hash{return Err(bad("new same-size standard redo hash mismatch"));}
    for (i,b) in image.chunks(BLOCK as usize).enumerate(){io.write(i as u64*BLOCK,b,false)?;io.report.standard_bytes_written+=b.len() as u64;}
    // Once the new prefix is durable the trailing FAST_WAL is sufficient to
    // finish or replay the transaction after any crash. Only then remove it.
    io.sync()?;
    let actual=io.read(0,p.new_std as usize)?;
    if hash(&actual)!=p.new_hash{return Err(bad("same-size prefix read-back mismatch"));}
    io.truncate(p.old_len)
}

fn apply(io:&mut Io<'_>, p:&Plan) -> io::Result<()> {
    let a=Progress::decode(&io.read(p.slot_a,SLOT as usize)?,p);
    let b=Progress::decode(&io.read(p.slot_a+SLOT,SLOT as usize)?,p);
    let mut s=match (a,b) { (Some(a),Some(b))=>if a.seq>b.seq {a}else{b}, (Some(a),None)|(None,Some(a))=>a,_=>return Err(bad("both redo state slots are invalid")) };
    let image=io.read(p.stage,p.new_std as usize)?;
    if hash(&image)!=p.new_hash {return Err(bad("new standard redo hash mismatch"));}
    if s.phase==0 && p.new_std!=p.old_std {
        while s.moved<p.tail() {
            if s.pending==0 {
                let n=(p.tail()-s.moved).min(BLOCK);
                let pos=if p.new_std>p.old_std {p.tail()-s.moved-n} else {s.moved};
                let block=io.read(p.old_std+pos,n as usize)?;
                io.write(p.scratch,&block,true)?; io.sync()?;
                s.pending=n; s.dest=p.new_std+pos; s.digest=hash(&block); s.save(io,p)?;
            }
            let block=io.read(p.scratch,s.pending as usize)?;
            if hash(&block)!=s.digest {return Err(bad("relocation scratch hash mismatch"));}
            io.write(s.dest,&block,false)?; io.sync()?;
            io.report.tail_bytes_moved+=s.pending;
            s.moved+=s.pending; s.pending=0; s.save(io,p)?;
        }
    }
    if s.phase==0 {s.phase=1;s.save(io,p)?;}
    if s.phase==1 {
        for (i,b) in image.chunks(BLOCK as usize).enumerate() {
            io.write(i as u64*BLOCK,b,false)?;
            io.report.standard_bytes_written+=b.len() as u64;
        }
        io.sync()?;
        // The redo image is immutable until prefix verification and DONE.
        let actual=io.read(0,p.new_std as usize)?;
        if hash(&actual)!=p.new_hash {return Err(bad("prefix read-back mismatch"));}
        s.phase=2;s.save(io,p)?;
    }
    io.truncate(p.final_len())
}

fn logical_end(file:&mut File) -> io::Result<(u64,u64)> {
    let len=file.metadata()?.len();
    if len==0 {return Ok((0,0));}
    let std=standard_end_reader(file).map_err(|e|bad(e.to_string()))?;
    if std>len {return Err(bad("truncated standard"));}
    let index=scan_rpkx(file).map_err(|e|bad(e.to_string()))?;
    let end=match index {Some(i)=>sum(std,i.container_len)?,None=>std};
    Ok((std,end))
}

fn recover_io(io:&mut Io<'_>) -> io::Result<bool> {
    let len=io.file.metadata()?.len();
    if len>=SLOT {
        let footer=io.read(len-SLOT,SLOT as usize)?;
        if &footer[..8]==FAST_WAL {
            if let Ok(p)=FastPlan::decode(&footer,FAST_WAL){
                if sum(p.footer,SLOT)?!=len{return Err(bad("unexpected same-size redo EOF"));}
                apply_fast(io,&p)?;io.report.recovered=1;return Ok(true);
            }
            // A torn FAST_WAL can only occur during prepare: prefix writes start
            // after the prepare sync returns. Fall through to FAST_INIT rollback.
        } else if &footer[..8]==WAL {
            let p=Plan::decode(&footer,WAL)?;
            if sum(p.footer,SLOT)?!=len {return Err(bad("unexpected redo EOF"));}
            apply(io,&p)?; io.report.recovered=1; return Ok(true);
        }
    }
    // Before the WAL is durable no live bytes have been changed. An INIT at
    // the canonical old EOF permits safe rollback of an interrupted prepare.
    let head=if len>=8 {io.read(0,8)?} else {Vec::new()};
    let old_end=if head==INIT {0}else{logical_end(io.file)?.1};
    if old_end==len {return Ok(false);}
    if len-old_end<SLOT {return Err(bad("unknown/torn trailing bytes; preserving file"));}
    let init=io.read(old_end,SLOT as usize)?;
    if &init[..8]==FAST_INIT {
        let p=FastPlan::decode(&init,FAST_INIT)?;
        if p.old_len!=old_end{return Err(bad("same-size INIT original length mismatch"));}
        let complete=sum(p.footer,SLOT)?;
        if len>complete{return Err(bad("unexpected bytes after same-size redo"));}
        // If the footer was valid, the entry check above would have replayed it.
        // Otherwise prefix mutation never began, so the append-only prepare can
        // be discarded without touching the original standard or RPKX bytes.
        io.truncate(p.old_len)?;io.report.recovered=1;return Ok(true);
    }
    if &init[..8]!=INIT {return Err(bad("unrelated EOF suffix is unsupported; preserving it"));}
    let p=Plan::decode(&init,INIT)?;
    if p.old_len!=old_end {return Err(bad("INIT original length mismatch"));}
    if len>=sum(p.footer,SLOT)? {
        // A corrupted complete descriptor is not proof that prefix was untouched.
        return Err(bad("complete but corrupt WAL; explicit repair required"));
    }
    io.truncate(p.old_len)?; io.report.recovered=1; Ok(true)
}

fn replace_io(io:&mut Io<'_>, image:&[u8], preserve_stale:bool) -> io::Result<()> {
    if image.len() as u64>MAX_STANDARD || standard_end(image).map_err(|e|bad(e.to_string()))?!=image.len() {return Err(bad("not a bounded standalone standard image"));}
    recover_io(io)?;
    let len=io.file.metadata()?.len();
    let (old_std,logical)=logical_end(io.file)?;
    if logical!=len {return Err(bad("unknown suffix"));}
    if len>old_std {
        let index=scan_rpkx(io.file).map_err(|e|bad(e.to_string()))?.ok_or_else(||bad("missing RPKX"))?;
        let stamp=reapeaks::reapeaks_source_stamp(image).map_err(|e|bad(e.to_string()))?;
        if index.source_stamp!=stamp && !preserve_stale {return Err(bad("source changed: RPKX binding would be stale (explicit preserve-stale required)"));}
    }
    if old_std==image.len() as u64 && old_std!=0 {
        let p=FastPlan::new(len,old_std,image)?;
        // Same-size replacement never overlaps the RPKX tail. Prepare is wholly
        // append-only; one sync makes INIT + staged standard + FAST_WAL durable.
        // The live prefix is touched only after that sync, making replay idempotent.
        io.write(len,&p.encode(FAST_INIT),true)?;
        io.write(p.stage,image,true)?;
        io.write(p.footer,&p.encode(FAST_WAL),true)?;
        io.sync()?;
        return apply_fast(io,&p);
    }
    let p=Plan::new(len,old_std,image)?;
    io.write(len,&p.encode(INIT),true)?;io.sync()?;
    io.write(p.stage,image,true)?;
    io.write(p.scratch,&[0],true)?;
    io.write(p.slot_a,&vec![0;SLOT as usize],true)?;
    let s=Progress{seq:1,..Default::default()};
    io.write(p.slot_a+SLOT,&s.encode(),true)?;
    io.sync()?;
    io.write(p.footer,&p.encode(WAL),true)?;io.sync()?;
    apply(io,&p)
}

pub fn lock_file(path:&Path) -> io::Result<File> {
    let lock_path=reapeaks::rpkx_file_lock_path(path);
    let f=OpenOptions::new().read(true).write(true).create(true).truncate(false).open(lock_path)?;
    f.lock()?;Ok(f)
}
/// Keeps the existing RPKX bytes and SourceStamp verbatim. preserve_stale must
/// be explicitly true to allow a new standard stamp to differ; the old analysis
/// is then stale, NOT silently rebound to the new media.
pub fn replace(path:&Path,image:&[u8],preserve_stale:bool) -> io::Result<Report> {
    let _lock=lock_file(path)?;
    let mut file=OpenOptions::new().read(true).write(true).create(true).truncate(false).open(path)?;
    let mut io=Io{file:&mut file,report:Report::default(),stop:None,torn:false};
    replace_io(&mut io,image,preserve_stale)?;Ok(io.report)
}
pub fn recover(path:&Path) -> io::Result<Report> {
    let _lock=lock_file(path)?;
    let mut file=OpenOptions::new().read(true).write(true).open(path)?;
    let mut io=Io{file:&mut file,report:Report::default(),stop:None,torn:false};
    recover_io(&mut io)?;Ok(io.report)
}
/// Read only the standard region, never materializing RPKX payloads.
pub fn read_standard(path:&Path) -> io::Result<Vec<u8>> {
    let _lock=lock_file(path)?;
    let mut file=OpenOptions::new().read(true).write(true).open(path)?;
    let mut io=Io{file:&mut file,report:Report::default(),stop:None,torn:false};
    recover_io(&mut io)?;
    let n=logical_end(io.file)?.0;
    if n>MAX_STANDARD {return Err(bad("standard exceeds memory budget"));}
    io.read(0,n as usize)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64,Ordering};
    static ID:AtomicU64=AtomicU64::new(0);
    struct Dir(std::path::PathBuf);
    impl Dir {fn new()->Self{let p=std::env::temp_dir().join(format!("lrpk-test-{}-{}",std::process::id(),ID.fetch_add(1,Ordering::SeqCst)));std::fs::create_dir(&p).unwrap();Self(p)}}
    impl Drop for Dir{fn drop(&mut self){let _=std::fs::remove_dir_all(&self.0);}}
    fn standard(div:u32,mtime:u32)->Vec<u8>{
        reapeaks::generate_pcm16(&vec![1234;4801],&reapeaks::GenerateOptions{sample_rate:48000,channels:1,divisions:vec![div],source_mtime_low32:mtime,source_size_low32:9999,spectral:false}).unwrap()
    }
    fn extended(s:&[u8],n:usize)->Vec<u8>{reapeaks::set_rpkx_chunk(s,reapeaks::RpkxChunk::new([7;16],*b"TEST",1,0,(0..n).map(|i|(i.wrapping_mul(193)^ (i>>8)) as u8).collect::<Vec<_>>())).unwrap()}
    #[test] fn roundtrip_same_grow_shrink_stale(){
        let d=Dir::new();let p=d.0.join("a.reapeaks");
        let old=standard(160,1);let original=extended(&old,BLOCK as usize+113);
        std::fs::write(&p,&original).unwrap();
        for div in [160,1,400,160]{
            let new=standard(div,1);let r=replace(&p,&new,false).unwrap();
            let actual=std::fs::read(&p).unwrap();assert_eq!(&actual[..new.len()],new);
            assert_eq!(&actual[new.len()..],&original[old.len()..]);
            if div==160 && new.len()==old.len(){assert_eq!(r.tail_bytes_moved,0);assert_eq!(r.syncs,3);}
        }
        let before=std::fs::read(&p).unwrap();assert!(replace(&p,&standard(160,2),false).is_err());assert_eq!(std::fs::read(&p).unwrap(),before);
        replace(&p,&standard(160,2),true).unwrap();
        assert_eq!(&std::fs::read(&p).unwrap()[old.len()..],&original[old.len()..]);
    }
    #[test] fn same_size_never_moves_payload(){let d=Dir::new();let p=d.0.join("a");let s=standard(160,1);std::fs::write(&p,extended(&s,8*1024*1024)).unwrap();let r=replace(&p,&s,false).unwrap();assert_eq!(r.tail_bytes_moved,0);assert_eq!(r.syncs,3);assert!(r.journal_bytes_written < 64*1024);}
    #[test] fn empty_file_and_plain_file(){let d=Dir::new();let p=d.0.join("a");let s=standard(160,1);replace(&p,&s,false).unwrap();assert_eq!(std::fs::read(&p).unwrap(),s);replace(&p,&standard(1,1),false).unwrap();assert_eq!(read_standard(&p).unwrap(),standard(1,1));}
    #[test] fn unknown_suffix_preserved_on_refusal(){let d=Dir::new();let p=d.0.join("a");let s=standard(160,1);let mut b=extended(&s,20);b.extend_from_slice(b"unrelated suffix");std::fs::write(&p,&b).unwrap();assert!(replace(&p,&s,false).is_err());assert_eq!(std::fs::read(&p).unwrap(),b);}
    #[test] fn malformed_new_image_is_rejected(){let d=Dir::new();let p=d.0.join("a");let s=standard(160,1);std::fs::write(&p,&s).unwrap();assert!(replace(&p,b"RPKN",false).is_err());assert_eq!(std::fs::read(&p).unwrap(),s);}
    #[test] fn fault_matrix(){
        let d=Dir::new();let p=d.0.join("a");let old=standard(160,1);let original=extended(&old,BLOCK as usize+113);
        let mut recovered=0;let mut safe_refusals=0;
        for div in [160,1,400] {for torn in [false,true] {for stop in 0..100 {
            std::fs::write(&p,&original).unwrap();let new=standard(div,1);
            {let mut f=OpenOptions::new().read(true).write(true).open(&p).unwrap();let mut io=Io{file:&mut f,report:Report::default(),stop:Some(stop),torn};let _=replace_io(&mut io,&new,false);}
            match recover(&p){Ok(_)=>{let b=std::fs::read(&p).unwrap();let boundary=standard_end(&b).unwrap();assert!(&b[..boundary]==old || &b[..boundary]==new);assert_eq!(&b[boundary..],&original[old.len()..]);recovered+=1;},Err(_)=>{let b=std::fs::read(&p).unwrap();assert_eq!(&b[..original.len()],original,"a refused recovery must not have changed live bytes: div={div} torn={torn} stop={stop}");safe_refusals+=1;}}
        }}}
        eprintln!("fault_matrix: recovered={recovered} safe_refusals={safe_refusals}");
    }
}
