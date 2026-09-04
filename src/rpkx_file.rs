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

#[cfg(unix)]
use std::os::unix::fs::MetadataExt;
#[cfg(windows)]
use std::os::windows::fs::MetadataExt;
#[cfg(not(any(unix, windows)))]
use std::time::SystemTime;

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

/// Logical I/O accounting for one file update.
///
/// `source_bytes_copied` counts bytes handed to Rust's standard filesystem/I/O
/// copy APIs. It is deliberately not a claim about physical storage traffic:
/// `std::fs::copy` and `std::io::copy` may use platform-specific kernel or
/// filesystem acceleration internally.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RpkxFileUpdateReport {
    pub changed: bool,
    pub old_file_len: u64,
    pub new_file_len: u64,
    pub source_bytes_copied: u64,
    pub payload_bytes_written: u64,
    pub metadata_bytes_written: u64,
}

impl RpkxFileUpdateReport {
    pub const fn preserved_source_bytes(self) -> u64 {
        self.source_bytes_copied
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
    #[cfg(unix)]
    dev: u64,
    #[cfg(unix)]
    ino: u64,
    #[cfg(unix)]
    mtime_sec: i64,
    #[cfg(unix)]
    mtime_nsec: i64,
    #[cfg(windows)]
    creation_time: u64,
    #[cfg(windows)]
    last_write_time: u64,
    #[cfg(not(any(unix, windows)))]
    modified: Option<SystemTime>,
}

fn file_generation(metadata: &Metadata) -> FileGeneration {
    FileGeneration {
        len: metadata.len(),
        #[cfg(unix)]
        dev: metadata.dev(),
        #[cfg(unix)]
        ino: metadata.ino(),
        #[cfg(unix)]
        mtime_sec: metadata.mtime(),
        #[cfg(unix)]
        mtime_nsec: metadata.mtime_nsec(),
        #[cfg(windows)]
        creation_time: metadata.creation_time(),
        #[cfg(windows)]
        last_write_time: metadata.last_write_time(),
        #[cfg(not(any(unix, windows)))]
        modified: metadata.modified().ok(),
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
            let replacement_key = chunk.key;
            let matches: Vec<usize> = entries
                .iter()
                .enumerate()
                .filter_map(|(index, entry)| (entry.key == replacement_key).then_some(index))
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
            for entry in entries {
                if entry.key == replacement_key {
                    if let Some(replacement) = replacement.take() {
                        chunks.push(replacement);
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
        payload_offset =
            payload_offset
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
    fn new(path: PathBuf) -> Self {
        Self {
            path,
            committed: false,
        }
    }

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

fn temp_path(target: &Path) -> Result<PathBuf> {
    let file_name = target
        .file_name()
        .ok_or(ReaPeaksError::InvalidArgument("RPKX path has no file name"))?;
    let counter = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let mut name = OsString::from(".");
    name.push(file_name);
    name.push(format!(".rpkx-tmp-{}-{counter}", std::process::id()));
    Ok(parent_dir(target).join(name))
}

fn create_temp_file(target: &Path) -> Result<(File, TempGuard)> {
    for _ in 0..128 {
        let path = temp_path(target)?;
        match OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(file) => return Ok((file, TempGuard::new(path))),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.into()),
        }
    }
    Err(ReaPeaksError::Io(
        "could not allocate unique RPKX temporary file".to_owned(),
    ))
}

fn copy_file_to_temp(source_path: &Path, target: &Path) -> Result<(File, TempGuard, u64)> {
    for _ in 0..128 {
        let path = temp_path(target)?;
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(reservation) => {
                drop(reservation);
                let guard = TempGuard::new(path.clone());
                let copied = fs::copy(source_path, &path)?;
                let file = OpenOptions::new().read(true).write(true).open(&path)?;
                return Ok((file, guard, copied));
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.into()),
        }
    }
    Err(ReaPeaksError::Io(
        "could not allocate unique RPKX temporary copy".to_owned(),
    ))
}

fn copy_range(
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
    let mut source = source.try_clone()?;
    source.seek(SeekFrom::Start(source_offset))?;
    destination.seek(SeekFrom::Start(destination_offset))?;
    let mut limited = source.take(len);
    let copied = std::io::copy(&mut limited, destination)?;
    if copied != len {
        return Err(ReaPeaksError::Truncated);
    }
    report.source_bytes_copied = report.source_bytes_copied.saturating_add(copied);
    Ok(())
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

fn write_same_size_update(
    destination: &mut File,
    layout: &FileLayout,
    plan: &UpdatePlan,
    index_position: usize,
    report: &mut RpkxFileUpdateReport,
) -> Result<()> {
    let index = layout.index.as_ref().ok_or(ReaPeaksError::InvalidHeader(
        "same-size RPKX update requires an existing container",
    ))?;
    let chunk = plan
        .chunks
        .get(index_position)
        .ok_or(ReaPeaksError::InvalidHeader(
            "RPKX chunk index out of range",
        ))?;
    let PlannedPayload::New(payload) = &chunk.payload else {
        return Err(ReaPeaksError::InvalidHeader(
            "same-size RPKX update requires a replacement payload",
        ));
    };
    let old = index.entries[index_position];
    if old.key != chunk.key || old.payload_len != payload.len() as u64 {
        return Err(ReaPeaksError::InvalidHeader(
            "same-size RPKX update plan no longer matches directory",
        ));
    }

    let directory_entry = layout
        .standard_end
        .checked_add(RPKX_HEADER_SIZE as u64)
        .and_then(|offset| offset.checked_add((index_position * RPKX_DIRECTORY_ENTRY_SIZE) as u64))
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

    report.payload_bytes_written = payload.len() as u64;
    report.metadata_bytes_written = 8;
    report.new_file_len = layout.file_len;
    Ok(())
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
        copy_range(source, destination, 0, 0, layout.standard_end, report)?;
        copy_range(
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

    copy_range(source, destination, 0, 0, layout.standard_end, report)?;
    destination.seek(SeekFrom::Start(layout.standard_end))?;
    destination.write_all(&prefix)?;
    report.metadata_bytes_written = prefix.len() as u64;

    let mut destination_payload_offset = layout.standard_end + prefix.len() as u64;
    for chunk in &plan.chunks {
        match &chunk.payload {
            PlannedPayload::Existing(entry) => {
                let source_payload_offset =
                    layout
                        .standard_end
                        .checked_add(entry.payload_offset)
                        .ok_or(ReaPeaksError::InvalidHeader("RPKX file offset overflow"))?;
                copy_range(
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
    copy_range(
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
/// atomically renamed over the original path.
///
/// Filesystem copy acceleration is intentionally delegated to Rust's standard
/// library. A unique same-size replacement uses `std::fs::copy` for the whole
/// cache and patches only the changed directory fields and payload. Repacking
/// updates stream unchanged ranges through `std::io::copy`.
pub fn update_rpkx_file(
    path: impl AsRef<Path>,
    update: RpkxFileUpdate,
) -> Result<RpkxFileUpdateReport> {
    let path = path.as_ref();
    let lock_path = rpkx_file_lock_path(path);
    let lock_file = OpenOptions::new()
        .create(true)
        .truncate(false)
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

    let (mut destination, mut temp) = if let Some(index_position) = plan.same_size_set_index {
        let (mut destination, temp, copied) = copy_file_to_temp(path, path)?;
        report.source_bytes_copied = copied;
        write_same_size_update(
            &mut destination,
            &layout,
            &plan,
            index_position,
            &mut report,
        )?;
        (destination, temp)
    } else {
        let (mut destination, temp) = create_temp_file(path)?;
        write_general_update(&source, &mut destination, &layout, &plan, &mut report)?;
        fs::set_permissions(&temp.path, source_metadata.permissions())?;
        (destination, temp)
    };

    destination.flush()?;
    destination.sync_all()?;
    verify_generation(path, &generation)?;
    drop(destination);
    drop(source);
    fs::rename(&temp.path, path)?;
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
