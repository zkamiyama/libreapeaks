use crate::error::{ReaPeaksError, Result};
use crate::rpkx::{
    scan_rpkx, standard_end_reader, RpkxChunk, RpkxEntry, RpkxIndex, RpkxKey,
    RPKX_DIRECTORY_ENTRY_SIZE, RPKX_HEADER_SIZE, RPKX_MAGIC, RPKX_VERSION,
};
use crate::source::SourceStamp;
use std::ffi::OsString;
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::SystemTime;

#[cfg(target_os = "linux")]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::fs::MetadataExt;

static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// One high-level mutation of the packed RPKX container stored in a file.
#[derive(Debug, Clone)]
pub enum RpkxFileUpdate {
    /// Replace all chunks with the same `(namespace, kind)` key with one chunk.
    Set(RpkxChunk),
    /// Append one chunk, preserving duplicate keys.
    Append(RpkxChunk),
    /// Remove every chunk with the key.
    Remove(RpkxKey),
    /// Remove the complete RPKX container while preserving unrelated EOF bytes.
    Strip,
}

/// I/O accounting for one file update.
///
/// The counters describe source-file bytes transferred by each preservation
/// strategy. A Linux reflink can make a very large logical transfer without
/// physically reading or writing the cloned data blocks.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RpkxFileUpdateReport {
    pub changed: bool,
    pub old_file_len: u64,
    pub new_file_len: u64,
    pub reflinked_source_bytes: u64,
    pub copy_file_range_source_bytes: u64,
    pub buffered_source_bytes: u64,
    pub payload_bytes_written: u64,
    pub metadata_bytes_written: u64,
}

impl RpkxFileUpdateReport {
    pub const fn preserved_source_bytes(self) -> u64 {
        self.reflinked_source_bytes
            .saturating_add(self.copy_file_range_source_bytes)
            .saturating_add(self.buffered_source_bytes)
    }
}

/// Stable sidecar lock path used by libreapeaks RPKX-aware writers.
///
/// The lock file is intentionally persistent. Deleting it after an update can
/// break synchronization with another process that already has the old lock
/// inode/handle open. Only the OS lock is held during a mutation.
pub fn rpkx_file_lock_path(path: impl AsRef<Path>) -> PathBuf {
    let mut value = path.as_ref().as_os_str().to_os_string();
    value.push(".rpkx.lock");
    PathBuf::from(value)
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct FileGeneration {
    len: u64,
    modified: Option<SystemTime>,
    #[cfg(unix)]
    dev: u64,
    #[cfg(unix)]
    ino: u64,
    #[cfg(unix)]
    mtime_sec: i64,
    #[cfg(unix)]
    mtime_nsec: i64,
}

fn file_generation(metadata: &Metadata) -> FileGeneration {
    FileGeneration {
        len: metadata.len(),
        modified: metadata.modified().ok(),
        #[cfg(unix)]
        dev: metadata.dev(),
        #[cfg(unix)]
        ino: metadata.ino(),
        #[cfg(unix)]
        mtime_sec: metadata.mtime(),
        #[cfg(unix)]
        mtime_nsec: metadata.mtime_nsec(),
    }
}

struct FileLayout {
    file_len: u64,
    standard_end: u64,
    source_stamp: SourceStamp,
    index: Option<RpkxIndex>,
    suffix_start: u64,
}

fn read_source_stamp(reader: &mut File) -> Result<SourceStamp> {
    reader.seek(SeekFrom::Start(10))?;
    let mut bytes = [0u8; 8];
    reader.read_exact(&mut bytes)?;
    Ok(SourceStamp::new(
        u32::from_le_bytes(bytes[0..4].try_into().unwrap()),
        u32::from_le_bytes(bytes[4..8].try_into().unwrap()),
    ))
}

fn scan_layout(reader: &mut File) -> Result<FileLayout> {
    let file_len = reader.metadata()?.len();
    let standard_end = standard_end_reader(reader)?;
    if standard_end > file_len {
        return Err(ReaPeaksError::Truncated);
    }
    let source_stamp = read_source_stamp(reader)?;
    if standard_end == file_len {
        return Ok(FileLayout {
            file_len,
            standard_end,
            source_stamp,
            index: None,
            suffix_start: file_len,
        });
    }

    let tail_len = file_len - standard_end;
    if tail_len < 4 {
        return Err(ReaPeaksError::Unsupported(
            "non-RPKX trailing bytes precede extension container",
        ));
    }
    reader.seek(SeekFrom::Start(standard_end))?;
    let mut magic = [0u8; 4];
    reader.read_exact(&mut magic)?;
    if magic != RPKX_MAGIC {
        return Err(ReaPeaksError::Unsupported(
            "non-RPKX trailing bytes precede extension container",
        ));
    }

    let index = scan_rpkx(reader)?.ok_or(ReaPeaksError::InvalidHeader(
        "RPKX magic present but container could not be scanned",
    ))?;
    if index.source_stamp != source_stamp {
        return Err(ReaPeaksError::InvalidArgument(
            "existing RPKX source stamp does not match .reapeaks header",
        ));
    }
    let suffix_start = standard_end
        .checked_add(index.container_len)
        .ok_or(ReaPeaksError::InvalidHeader("RPKX file offset overflow"))?;
    if suffix_start > file_len {
        return Err(ReaPeaksError::Truncated);
    }
    Ok(FileLayout {
        file_len,
        standard_end,
        source_stamp,
        index: Some(index),
        suffix_start,
    })
}

#[derive(Debug, Clone)]
enum PlannedPayload {
    Existing(RpkxEntry),
    New(Vec<u8>),
}

#[derive(Debug, Clone)]
struct PlannedChunk {
    key: RpkxKey,
    version: u32,
    flags: u32,
    payload: PlannedPayload,
}

impl PlannedChunk {
    fn existing(entry: RpkxEntry) -> Self {
        Self {
            key: entry.key,
            version: entry.version,
            flags: entry.flags,
            payload: PlannedPayload::Existing(entry),
        }
    }

    fn new(chunk: RpkxChunk) -> Self {
        Self {
            key: chunk.key,
            version: chunk.version,
            flags: chunk.flags,
            payload: PlannedPayload::New(chunk.payload),
        }
    }

    fn payload_len(&self) -> u64 {
        match &self.payload {
            PlannedPayload::Existing(entry) => entry.payload_len,
            PlannedPayload::New(payload) => payload.len() as u64,
        }
    }
}

struct UpdatePlan {
    chunks: Vec<PlannedChunk>,
    container_flags: u32,
    changed: bool,
    same_size_set_index: Option<usize>,
}

fn build_plan(layout: &FileLayout, update: RpkxFileUpdate) -> UpdatePlan {
    let entries = layout
        .index
        .as_ref()
        .map(|index| index.entries.as_slice())
        .unwrap_or(&[]);
    let container_flags = layout.index.as_ref().map_or(0, |index| index.flags);

    match update {
        RpkxFileUpdate::Set(chunk) => {
            let matches: Vec<usize> = entries
                .iter()
                .enumerate()
                .filter_map(|(index, entry)| (entry.key == chunk.key).then_some(index))
                .collect();
            let same_size_set_index = if matches.len() == 1
                && entries[matches[0]].payload_len == chunk.payload.len() as u64
            {
                Some(matches[0])
            } else {
                None
            };

            let mut replacement = Some(PlannedChunk::new(chunk));
            let mut chunks = Vec::with_capacity(entries.len() + usize::from(matches.is_empty()));
            let mut inserted = false;
            for entry in entries {
                if entry.key == replacement.as_ref().unwrap().key {
                    if !inserted {
                        chunks.push(replacement.take().unwrap());
                        inserted = true;
                    }
                } else {
                    chunks.push(PlannedChunk::existing(*entry));
                }
            }
            if let Some(replacement) = replacement {
                chunks.push(replacement);
            }
            UpdatePlan {
                chunks,
                container_flags,
                changed: true,
                same_size_set_index,
            }
        }
        RpkxFileUpdate::Append(chunk) => {
            let mut chunks: Vec<_> = entries
                .iter()
                .copied()
                .map(PlannedChunk::existing)
                .collect();
            chunks.push(PlannedChunk::new(chunk));
            UpdatePlan {
                chunks,
                container_flags,
                changed: true,
                same_size_set_index: None,
            }
        }
        RpkxFileUpdate::Remove(key) => {
            let chunks: Vec<_> = entries
                .iter()
                .copied()
                .filter(|entry| entry.key != key)
                .map(PlannedChunk::existing)
                .collect();
            UpdatePlan {
                changed: chunks.len() != entries.len(),
                chunks,
                container_flags,
                same_size_set_index: None,
            }
        }
        RpkxFileUpdate::Strip => UpdatePlan {
            chunks: Vec::new(),
            container_flags,
            changed: layout.index.is_some(),
            same_size_set_index: None,
        },
    }
}

fn encode_prefix(
    chunks: &[PlannedChunk],
    flags: u32,
    source_stamp: SourceStamp,
) -> Result<(Vec<u8>, u64)> {
    let chunk_count = u32::try_from(chunks.len())
        .map_err(|_| ReaPeaksError::InvalidArgument("too many RPKX chunks"))?;
    let directory_len = chunks
        .len()
        .checked_mul(RPKX_DIRECTORY_ENTRY_SIZE)
        .and_then(|len| len.checked_add(RPKX_HEADER_SIZE))
        .ok_or(ReaPeaksError::InvalidArgument(
            "RPKX directory size overflow",
        ))?;
    let mut container_len = directory_len as u64;
    for chunk in chunks {
        container_len = container_len
            .checked_add(chunk.payload_len())
            .ok_or(ReaPeaksError::InvalidArgument("RPKX size overflow"))?;
    }

    let mut out = Vec::with_capacity(directory_len);
    out.extend_from_slice(&RPKX_MAGIC);
    out.extend_from_slice(&RPKX_VERSION.to_le_bytes());
    out.extend_from_slice(&(RPKX_HEADER_SIZE as u16).to_le_bytes());
    out.extend_from_slice(&flags.to_le_bytes());
    out.extend_from_slice(&chunk_count.to_le_bytes());
    out.extend_from_slice(&container_len.to_le_bytes());
    out.extend_from_slice(&source_stamp.mtime_low32.to_le_bytes());
    out.extend_from_slice(&source_stamp.size_low32.to_le_bytes());

    let mut payload_offset = directory_len as u64;
    for chunk in chunks {
        let payload_len = chunk.payload_len();
        out.extend_from_slice(&chunk.key.namespace);
        out.extend_from_slice(&chunk.key.kind);
        out.extend_from_slice(&chunk.version.to_le_bytes());
        out.extend_from_slice(&chunk.flags.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&payload_offset.to_le_bytes());
        out.extend_from_slice(&payload_len.to_le_bytes());
        payload_offset = payload_offset
            .checked_add(payload_len)
            .ok_or(ReaPeaksError::InvalidArgument(
                "RPKX payload offset overflow",
            ))?;
    }
    debug_assert_eq!(out.len(), directory_len);
    Ok((out, container_len))
}

fn parent_dir(path: &Path) -> &Path {
    path.parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
}

struct TempGuard {
    path: PathBuf,
    committed: bool,
}

impl TempGuard {
    fn commit(&mut self) {
        self.committed = true;
    }
}

impl Drop for TempGuard {
    fn drop(&mut self) {
        if !self.committed {
            let _ = fs::remove_file(&self.path);
        }
    }
}

fn create_temp_file(target: &Path) -> Result<(File, TempGuard)> {
    let file_name = target
        .file_name()
        .ok_or(ReaPeaksError::InvalidArgument("RPKX path has no file name"))?;
    for _ in 0..128 {
        let counter = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let mut name = OsString::from(".");
        name.push(file_name);
        name.push(format!(
            ".rpkx-tmp-{}-{counter}",
            std::process::id()
        ));
        let path = parent_dir(target).join(name);
        match OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(file) => {
                return Ok((
                    file,
                    TempGuard {
                        path,
                        committed: false,
                    },
                ));
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.into()),
        }
    }
    Err(ReaPeaksError::Io(
        "could not allocate unique RPKX temporary file".to_owned(),
    ))
}

#[cfg(target_os = "linux")]
unsafe extern "C" {
    fn rpk_linux_ficlone(dst_fd: i32, src_fd: i32) -> i32;
    fn rpk_linux_ficlonerange(
        dst_fd: i32,
        src_fd: i32,
        src_offset: u64,
        src_length: u64,
        dst_offset: u64,
    ) -> i32;
    fn rpk_linux_copy_file_range_once(
        src_fd: i32,
        src_offset: u64,
        dst_fd: i32,
        dst_offset: u64,
        length: usize,
    ) -> isize;
}

#[cfg(target_os = "linux")]
fn try_reflink_whole(source: &File, destination: &File) -> bool {
    // SAFETY: both descriptors are live regular-file handles owned by the
    // caller. FICLONE does not retain the integer descriptors after return.
    unsafe { rpk_linux_ficlone(destination.as_raw_fd(), source.as_raw_fd()) == 0 }
}

#[cfg(not(target_os = "linux"))]
fn try_reflink_whole(_source: &File, _destination: &File) -> bool {
    false
}

#[cfg(target_os = "linux")]
fn try_reflink_range(
    source: &File,
    destination: &File,
    source_offset: u64,
    destination_offset: u64,
    len: u64,
) -> bool {
    if len == 0 {
        return true;
    }
    // SAFETY: the C shim builds Linux's file_clone_range structure from these
    // values and invokes FICLONERANGE synchronously.
    unsafe {
        rpk_linux_ficlonerange(
            destination.as_raw_fd(),
            source.as_raw_fd(),
            source_offset,
            len,
            destination_offset,
        ) == 0
    }
}

#[cfg(not(target_os = "linux"))]
fn try_reflink_range(
    _source: &File,
    _destination: &File,
    _source_offset: u64,
    _destination_offset: u64,
    _len: u64,
) -> bool {
    false
}

fn buffered_copy_range(
    source: &File,
    destination: &mut File,
    source_offset: u64,
    destination_offset: u64,
    len: u64,
) -> Result<()> {
    let mut source = source.try_clone()?;
    source.seek(SeekFrom::Start(source_offset))?;
    destination.seek(SeekFrom::Start(destination_offset))?;
    let mut remaining = len;
    let mut buffer = vec![0u8; 1024 * 1024];
    while remaining != 0 {
        let count = usize::try_from(remaining.min(buffer.len() as u64)).unwrap();
        source.read_exact(&mut buffer[..count])?;
        destination.write_all(&buffer[..count])?;
        remaining -= count as u64;
    }
    Ok(())
}

fn copy_range_optimized(
    source: &File,
    destination: &mut File,
    source_offset: u64,
    destination_offset: u64,
    len: u64,
    report: &mut RpkxFileUpdateReport,
) -> Result<()> {
    if len == 0 {
        return Ok(());
    }
    if try_reflink_range(
        source,
        destination,
        source_offset,
        destination_offset,
        len,
    ) {
        report.reflinked_source_bytes = report.reflinked_source_bytes.saturating_add(len);
        return Ok(());
    }

    #[cfg(target_os = "linux")]
    {
        let mut copied = 0u64;
        while copied < len {
            let remaining = len - copied;
            let request = usize::try_from(remaining.min(1024 * 1024 * 1024)).unwrap();
            // SAFETY: the C shim calls copy_file_range once using live file
            // descriptors and value offsets. It does not retain pointers.
            let result = unsafe {
                rpk_linux_copy_file_range_once(
                    source.as_raw_fd(),
                    source_offset + copied,
                    destination.as_raw_fd(),
                    destination_offset + copied,
                    request,
                )
            };
            if result > 0 {
                copied += result as u64;
                continue;
            }
            if result == 0 {
                break;
            }
            break;
        }
        if copied != 0 {
            report.copy_file_range_source_bytes = report
                .copy_file_range_source_bytes
                .saturating_add(copied);
        }
        if copied == len {
            return Ok(());
        }
        buffered_copy_range(
            source,
            destination,
            source_offset + copied,
            destination_offset + copied,
            len - copied,
        )?;
        report.buffered_source_bytes = report
            .buffered_source_bytes
            .saturating_add(len - copied);
        return Ok(());
    }

    #[cfg(not(target_os = "linux"))]
    {
        buffered_copy_range(
            source,
            destination,
            source_offset,
            destination_offset,
            len,
        )?;
        report.buffered_source_bytes = report.buffered_source_bytes.saturating_add(len);
        Ok(())
    }
}

#[cfg(windows)]
fn atomic_replace(temp: &Path, target: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;

    const MOVEFILE_REPLACE_EXISTING: u32 = 0x1;
    const MOVEFILE_WRITE_THROUGH: u32 = 0x8;
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn MoveFileExW(existing: *const u16, new: *const u16, flags: u32) -> i32;
    }

    let mut temp_wide: Vec<u16> = temp.as_os_str().encode_wide().collect();
    temp_wide.push(0);
    let mut target_wide: Vec<u16> = target.as_os_str().encode_wide().collect();
    target_wide.push(0);
    // SAFETY: both UTF-16 buffers are NUL terminated and remain alive for the
    // duration of the synchronous Win32 call.
    let ok = unsafe {
        MoveFileExW(
            temp_wide.as_ptr(),
            target_wide.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if ok == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn atomic_replace(temp: &Path, target: &Path) -> std::io::Result<()> {
    fs::rename(temp, target)
}

#[cfg(unix)]
fn sync_parent_directory(path: &Path) {
    if let Ok(directory) = File::open(parent_dir(path)) {
        let _ = directory.sync_all();
    }
}

#[cfg(not(unix))]
fn sync_parent_directory(_path: &Path) {}

fn verify_generation(path: &Path, expected: &FileGeneration) -> Result<()> {
    let current = file_generation(&fs::metadata(path)?);
    if &current != expected {
        return Err(ReaPeaksError::Io(
            "concurrent .reapeaks modification detected before RPKX commit".to_owned(),
        ));
    }
    Ok(())
}

fn try_same_size_reflink_update(
    source: &File,
    destination: &mut File,
    layout: &FileLayout,
    plan: &UpdatePlan,
    index_position: usize,
    report: &mut RpkxFileUpdateReport,
) -> Result<bool> {
    let Some(index) = &layout.index else {
        return Ok(false);
    };
    let Some(chunk) = plan.chunks.get(index_position) else {
        return Ok(false);
    };
    let PlannedPayload::New(payload) = &chunk.payload else {
        return Ok(false);
    };
    let old = index.entries[index_position];
    if old.key != chunk.key || old.payload_len != payload.len() as u64 {
        return Ok(false);
    }
    if !try_reflink_whole(source, destination) {
        return Ok(false);
    }
    destination.set_len(layout.file_len)?;
    let directory_entry = layout
        .standard_end
        .checked_add(RPKX_HEADER_SIZE as u64)
        .and_then(|offset| {
            offset.checked_add((index_position * RPKX_DIRECTORY_ENTRY_SIZE) as u64)
        })
        .ok_or(ReaPeaksError::InvalidHeader("RPKX file offset overflow"))?;
    destination.seek(SeekFrom::Start(directory_entry + 20))?;
    destination.write_all(&chunk.version.to_le_bytes())?;
    destination.write_all(&chunk.flags.to_le_bytes())?;
    let payload_offset = layout
        .standard_end
        .checked_add(old.payload_offset)
        .ok_or(ReaPeaksError::InvalidHeader("RPKX file offset overflow"))?;
    destination.seek(SeekFrom::Start(payload_offset))?;
    destination.write_all(payload)?;

    report.reflinked_source_bytes = layout.file_len;
    report.payload_bytes_written = payload.len() as u64;
    report.metadata_bytes_written = 8;
    report.new_file_len = layout.file_len;
    Ok(true)
}

fn write_general_update(
    source: &File,
    destination: &mut File,
    layout: &FileLayout,
    plan: &UpdatePlan,
    report: &mut RpkxFileUpdateReport,
) -> Result<()> {
    if plan.chunks.is_empty() {
        let suffix_len = layout.file_len - layout.suffix_start;
        let new_len = layout
            .standard_end
            .checked_add(suffix_len)
            .ok_or(ReaPeaksError::InvalidArgument("RPKX output size overflow"))?;
        destination.set_len(new_len)?;
        copy_range_optimized(
            source,
            destination,
            0,
            0,
            layout.standard_end,
            report,
        )?;
        copy_range_optimized(
            source,
            destination,
            layout.suffix_start,
            layout.standard_end,
            suffix_len,
            report,
        )?;
        report.new_file_len = new_len;
        return Ok(());
    }

    let (prefix, container_len) =
        encode_prefix(&plan.chunks, plan.container_flags, layout.source_stamp)?;
    let suffix_len = layout.file_len - layout.suffix_start;
    let new_len = layout
        .standard_end
        .checked_add(container_len)
        .and_then(|value| value.checked_add(suffix_len))
        .ok_or(ReaPeaksError::InvalidArgument("RPKX output size overflow"))?;
    destination.set_len(new_len)?;

    copy_range_optimized(
        source,
        destination,
        0,
        0,
        layout.standard_end,
        report,
    )?;
    destination.seek(SeekFrom::Start(layout.standard_end))?;
    destination.write_all(&prefix)?;
    report.metadata_bytes_written = prefix.len() as u64;

    let mut destination_payload_offset = layout.standard_end + prefix.len() as u64;
    let source_container_offset = layout.standard_end;
    for chunk in &plan.chunks {
        match &chunk.payload {
            PlannedPayload::Existing(entry) => {
                let source_payload_offset = source_container_offset
                    .checked_add(entry.payload_offset)
                    .ok_or(ReaPeaksError::InvalidHeader("RPKX file offset overflow"))?;
                copy_range_optimized(
                    source,
                    destination,
                    source_payload_offset,
                    destination_payload_offset,
                    entry.payload_len,
                    report,
                )?;
                destination_payload_offset += entry.payload_len;
            }
            PlannedPayload::New(payload) => {
                destination.seek(SeekFrom::Start(destination_payload_offset))?;
                destination.write_all(payload)?;
                report.payload_bytes_written = report
                    .payload_bytes_written
                    .saturating_add(payload.len() as u64);
                destination_payload_offset += payload.len() as u64;
            }
        }
    }

    let destination_suffix = layout.standard_end + container_len;
    copy_range_optimized(
        source,
        destination,
        layout.suffix_start,
        destination_suffix,
        suffix_len,
        report,
    )?;
    report.new_file_len = new_len;
    Ok(())
}

/// Apply one RPKX mutation directly to a `.reapeaks` file without loading
/// unrelated payloads into memory.
///
/// Writers coordinate through `<cache>.rpkx.lock`; readers remain lock-free.
/// The replacement file is built in the same directory, synced, checked against
/// the source file generation captured after acquiring the lock, and then
/// atomically renamed over the original path. On Linux, unchanged source ranges
/// attempt reflink first, then `copy_file_range`, then buffered copying.
pub fn update_rpkx_file(
    path: impl AsRef<Path>,
    update: RpkxFileUpdate,
) -> Result<RpkxFileUpdateReport> {
    let path = path.as_ref();
    let lock_path = rpkx_file_lock_path(path);
    let lock_file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(lock_path)?;
    lock_file.lock()?;

    let mut source = File::open(path)?;
    let source_metadata = source.metadata()?;
    let generation = file_generation(&source_metadata);
    if file_generation(&fs::metadata(path)?) != generation {
        return Err(ReaPeaksError::Io(
            "concurrent .reapeaks replacement detected while opening RPKX updater".to_owned(),
        ));
    }
    let layout = scan_layout(&mut source)?;
    let plan = build_plan(&layout, update);
    let mut report = RpkxFileUpdateReport {
        changed: plan.changed,
        old_file_len: layout.file_len,
        new_file_len: layout.file_len,
        ..RpkxFileUpdateReport::default()
    };
    if !plan.changed {
        return Ok(report);
    }

    let (mut destination, mut temp) = create_temp_file(path)?;
    fs::set_permissions(&temp.path, source_metadata.permissions())?;

    let fast_path = if let Some(index_position) = plan.same_size_set_index {
        try_same_size_reflink_update(
            &source,
            &mut destination,
            &layout,
            &plan,
            index_position,
            &mut report,
        )?
    } else {
        false
    };
    if !fast_path {
        destination.set_len(0)?;
        report.reflinked_source_bytes = 0;
        report.copy_file_range_source_bytes = 0;
        report.buffered_source_bytes = 0;
        report.payload_bytes_written = 0;
        report.metadata_bytes_written = 0;
        write_general_update(
            &source,
            &mut destination,
            &layout,
            &plan,
            &mut report,
        )?;
    }

    destination.flush()?;
    destination.sync_all()?;
    verify_generation(path, &generation)?;
    atomic_replace(&temp.path, path)?;
    temp.commit();
    sync_parent_directory(path);
    Ok(report)
}

pub fn set_rpkx_chunk_file(
    path: impl AsRef<Path>,
    chunk: RpkxChunk,
) -> Result<RpkxFileUpdateReport> {
    update_rpkx_file(path, RpkxFileUpdate::Set(chunk))
}

pub fn append_rpkx_chunk_file(
    path: impl AsRef<Path>,
    chunk: RpkxChunk,
) -> Result<RpkxFileUpdateReport> {
    update_rpkx_file(path, RpkxFileUpdate::Append(chunk))
}

pub fn remove_rpkx_chunks_file(
    path: impl AsRef<Path>,
    key: RpkxKey,
) -> Result<RpkxFileUpdateReport> {
    update_rpkx_file(path, RpkxFileUpdate::Remove(key))
}

pub fn strip_rpkx_file(path: impl AsRef<Path>) -> Result<RpkxFileUpdateReport> {
    update_rpkx_file(path, RpkxFileUpdate::Strip)
}
