use crate::error::{ReaPeaksError, Result};
use crate::format::{TOKEN_LOUDNESS, TOKEN_LOUDNESS_OLD, TOKEN_SPECTRAL, TOKEN_SPECTROGRAM};
use crate::source::SourceStamp;
use std::io::{Read, Seek, SeekFrom, Write};

pub const RPKX_MAGIC: [u8; 4] = *b"RPKX";
pub const RPKX_VERSION: u16 = 1;
pub const RPKX_HEADER_SIZE: usize = 32;
pub const RPKX_DIRECTORY_ENTRY_SIZE: usize = 48;
/// Backward source-compatibility alias. RPKX v1 now stores a fixed directory
/// before all payloads; there is no interleaved chunk header anymore.
pub const RPKX_CHUNK_HEADER_SIZE: usize = RPKX_DIRECTORY_ENTRY_SIZE;

/// Opaque 128-bit application namespace.
///
/// Applications should use a stable UUID (RFC 4122/9562 byte order) so
/// independently developed chunk types do not collide.
pub type RpkxNamespace = [u8; 16];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct RpkxKey {
    pub namespace: RpkxNamespace,
    pub kind: [u8; 4],
}

impl RpkxKey {
    pub const fn new(namespace: RpkxNamespace, kind: [u8; 4]) -> Self {
        Self { namespace, kind }
    }
}

/// One metadata-only RPKX v1 directory entry.
///
/// `payload_offset` is relative to the RPKX magic, not to the complete
/// `.reapeaks` file. RPKX v1 requires payload extents to be tightly packed in
/// directory order with no gaps or free-list arena.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RpkxEntry {
    pub key: RpkxKey,
    pub version: u32,
    pub flags: u32,
    pub payload_offset: u64,
    pub payload_len: u64,
}

/// Metadata-only index for an RPKX v1 container.
///
/// A seekable file can be scanned by reading only the standard REAPER header
/// and layer table plus `32 + 48 * chunk_count` RPKX metadata bytes. Payloads
/// are not read until explicitly requested.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RpkxIndex {
    pub flags: u32,
    pub source_stamp: SourceStamp,
    /// Absolute offset of the RPKX magic in the source stream. This is zero
    /// when `parse_prefix()` is used on a standalone RPKX byte slice.
    pub container_offset: u64,
    pub container_len: u64,
    pub entries: Vec<RpkxEntry>,
}

impl RpkxIndex {
    /// Parse an RPKX header and directory prefix without requiring payload
    /// bytes to be present in `bytes`.
    pub fn parse_prefix(bytes: &[u8]) -> Result<Self> {
        parse_index_prefix(bytes, 0)
    }

    pub fn entry(&self, key: RpkxKey) -> Option<&RpkxEntry> {
        self.entries.iter().find(|entry| entry.key == key)
    }

    pub fn entries_for(&self, key: RpkxKey) -> impl Iterator<Item = &RpkxEntry> {
        self.entries.iter().filter(move |entry| entry.key == key)
    }

    pub fn directory_len(&self) -> usize {
        RPKX_HEADER_SIZE + self.entries.len() * RPKX_DIRECTORY_ENTRY_SIZE
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RpkxChunk {
    pub key: RpkxKey,
    pub version: u32,
    pub flags: u32,
    pub payload: Vec<u8>,
}

impl RpkxChunk {
    pub fn new(
        namespace: RpkxNamespace,
        kind: [u8; 4],
        version: u32,
        flags: u32,
        payload: impl Into<Vec<u8>>,
    ) -> Self {
        Self {
            key: RpkxKey::new(namespace, kind),
            version,
            flags,
            payload: payload.into(),
        }
    }
}

/// Owning/editable representation of one RPKX v1 container.
///
/// Use `scan_rpkx()` plus `read_rpkx_payload()` or `copy_rpkx_payload()` when
/// large payloads should not be materialized eagerly. `RpkxContainer` remains
/// the convenient owning API for byte-to-byte editing and small containers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RpkxContainer {
    pub flags: u32,
    pub source_stamp: SourceStamp,
    pub chunks: Vec<RpkxChunk>,
}

impl RpkxContainer {
    pub const fn new(source_stamp: SourceStamp) -> Self {
        Self {
            flags: 0,
            source_stamp,
            chunks: Vec::new(),
        }
    }

    pub const fn matches_source_stamp(&self, stamp: SourceStamp) -> bool {
        self.source_stamp.mtime_low32 == stamp.mtime_low32
            && self.source_stamp.size_low32 == stamp.size_low32
    }

    pub fn chunk(&self, key: RpkxKey) -> Option<&RpkxChunk> {
        self.chunks.iter().find(|chunk| chunk.key == key)
    }

    pub fn chunks_for(&self, key: RpkxKey) -> impl Iterator<Item = &RpkxChunk> {
        self.chunks.iter().filter(move |chunk| chunk.key == key)
    }

    /// Set the common-case unique value for a namespace/kind pair.
    pub fn set_chunk(&mut self, chunk: RpkxChunk) {
        let key = chunk.key;
        let first = self.chunks.iter().position(|existing| existing.key == key);
        self.chunks.retain(|existing| existing.key != key);
        let index = first.unwrap_or(self.chunks.len()).min(self.chunks.len());
        self.chunks.insert(index, chunk);
    }

    /// Append a chunk without enforcing key uniqueness.
    pub fn append_chunk(&mut self, chunk: RpkxChunk) {
        self.chunks.push(chunk);
    }

    /// Remove every chunk with this namespace/kind pair.
    pub fn remove_chunks(&mut self, key: RpkxKey) -> usize {
        let before = self.chunks.len();
        self.chunks.retain(|chunk| chunk.key != key);
        before - self.chunks.len()
    }

    /// Encode the canonical packed RPKX v1 layout:
    /// `[header][all directory entries][all payloads]`.
    pub fn encode(&self) -> Result<Vec<u8>> {
        let chunk_count = u32::try_from(self.chunks.len())
            .map_err(|_| ReaPeaksError::InvalidArgument("too many RPKX chunks"))?;
        let directory_len = self
            .chunks
            .len()
            .checked_mul(RPKX_DIRECTORY_ENTRY_SIZE)
            .and_then(|len| len.checked_add(RPKX_HEADER_SIZE))
            .ok_or(ReaPeaksError::InvalidArgument("RPKX directory size overflow"))?;
        let payload_len = self.chunks.iter().try_fold(0usize, |total, chunk| {
            total
                .checked_add(chunk.payload.len())
                .ok_or(ReaPeaksError::InvalidArgument("RPKX payload size overflow"))
        })?;
        let total_len = directory_len
            .checked_add(payload_len)
            .ok_or(ReaPeaksError::InvalidArgument("RPKX size overflow"))?;
        let total_len_u64 = u64::try_from(total_len)
            .map_err(|_| ReaPeaksError::InvalidArgument("RPKX too large"))?;

        let mut out = Vec::with_capacity(total_len);
        out.extend_from_slice(&RPKX_MAGIC);
        out.extend_from_slice(&RPKX_VERSION.to_le_bytes());
        out.extend_from_slice(&(RPKX_HEADER_SIZE as u16).to_le_bytes());
        out.extend_from_slice(&self.flags.to_le_bytes());
        out.extend_from_slice(&chunk_count.to_le_bytes());
        out.extend_from_slice(&total_len_u64.to_le_bytes());
        out.extend_from_slice(&self.source_stamp.mtime_low32.to_le_bytes());
        out.extend_from_slice(&self.source_stamp.size_low32.to_le_bytes());

        let mut payload_offset = u64::try_from(directory_len)
            .map_err(|_| ReaPeaksError::InvalidArgument("RPKX directory too large"))?;
        for chunk in &self.chunks {
            let payload_len = u64::try_from(chunk.payload.len())
                .map_err(|_| ReaPeaksError::InvalidArgument("RPKX payload too large"))?;
            out.extend_from_slice(&chunk.key.namespace);
            out.extend_from_slice(&chunk.key.kind);
            out.extend_from_slice(&chunk.version.to_le_bytes());
            out.extend_from_slice(&chunk.flags.to_le_bytes());
            out.extend_from_slice(&0u32.to_le_bytes());
            out.extend_from_slice(&payload_offset.to_le_bytes());
            out.extend_from_slice(&payload_len.to_le_bytes());
            payload_offset = payload_offset
                .checked_add(payload_len)
                .ok_or(ReaPeaksError::InvalidArgument("RPKX payload offset overflow"))?;
        }
        for chunk in &self.chunks {
            out.extend_from_slice(&chunk.payload);
        }
        debug_assert_eq!(out.len(), total_len);
        Ok(out)
    }

    /// Parse and own every payload in a complete RPKX container.
    pub fn parse(bytes: &[u8]) -> Result<(Self, usize)> {
        let index = RpkxIndex::parse_prefix(bytes)?;
        let container_len = usize::try_from(index.container_len)
            .map_err(|_| ReaPeaksError::InvalidHeader("RPKX length does not fit address space"))?;
        if container_len > bytes.len() {
            return Err(ReaPeaksError::Truncated);
        }
        let mut chunks = Vec::with_capacity(index.entries.len());
        for entry in &index.entries {
            let start = usize::try_from(entry.payload_offset)
                .map_err(|_| ReaPeaksError::InvalidHeader("RPKX payload offset too large"))?;
            let len = usize::try_from(entry.payload_len)
                .map_err(|_| ReaPeaksError::InvalidHeader("RPKX payload length too large"))?;
            let end = start
                .checked_add(len)
                .ok_or(ReaPeaksError::InvalidHeader("RPKX payload offset overflow"))?;
            chunks.push(RpkxChunk {
                key: entry.key,
                version: entry.version,
                flags: entry.flags,
                payload: bytes[start..end].to_vec(),
            });
        }
        Ok((
            Self {
                flags: index.flags,
                source_stamp: index.source_stamp,
                chunks,
            },
            container_len,
        ))
    }
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub enum RpkxAttachPolicy {
    #[default]
    RequireMatchingSourceStamp,
    AllowSourceStampMismatch,
}

#[derive(Debug, Clone, Copy)]
struct RpkxHeaderFields {
    flags: u32,
    chunk_count: u32,
    container_len: u64,
    source_stamp: SourceStamp,
}

fn read_u32(raw: &[u8], offset: usize) -> Result<u32> {
    let end = offset.checked_add(4).ok_or(ReaPeaksError::Truncated)?;
    let bytes: [u8; 4] = raw
        .get(offset..end)
        .ok_or(ReaPeaksError::Truncated)?
        .try_into()
        .map_err(|_| ReaPeaksError::Truncated)?;
    Ok(u32::from_le_bytes(bytes))
}

fn read_i32(raw: &[u8], offset: usize) -> Result<i32> {
    Ok(read_u32(raw, offset)? as i32)
}

fn parse_rpkx_header(bytes: &[u8]) -> Result<RpkxHeaderFields> {
    if bytes.len() < RPKX_HEADER_SIZE {
        return Err(ReaPeaksError::Truncated);
    }
    if bytes.get(0..4) != Some(RPKX_MAGIC.as_slice()) {
        let magic: [u8; 4] = bytes[0..4].try_into().map_err(|_| ReaPeaksError::Truncated)?;
        return Err(ReaPeaksError::InvalidMagic(magic));
    }
    let version = u16::from_le_bytes(bytes[4..6].try_into().unwrap());
    if version != RPKX_VERSION {
        return Err(ReaPeaksError::Unsupported("RPKX container version"));
    }
    let header_size = usize::from(u16::from_le_bytes(bytes[6..8].try_into().unwrap()));
    if header_size != RPKX_HEADER_SIZE {
        return Err(ReaPeaksError::Unsupported("RPKX v1 header size"));
    }
    Ok(RpkxHeaderFields {
        flags: u32::from_le_bytes(bytes[8..12].try_into().unwrap()),
        chunk_count: u32::from_le_bytes(bytes[12..16].try_into().unwrap()),
        container_len: u64::from_le_bytes(bytes[16..24].try_into().unwrap()),
        source_stamp: SourceStamp::new(
            u32::from_le_bytes(bytes[24..28].try_into().unwrap()),
            u32::from_le_bytes(bytes[28..32].try_into().unwrap()),
        ),
    })
}

fn parse_index_prefix(bytes: &[u8], container_offset: u64) -> Result<RpkxIndex> {
    let header = parse_rpkx_header(bytes)?;
    let directory_bytes = u64::from(header.chunk_count)
        .checked_mul(RPKX_DIRECTORY_ENTRY_SIZE as u64)
        .ok_or(ReaPeaksError::InvalidHeader("RPKX directory size overflow"))?;
    let payload_region = (RPKX_HEADER_SIZE as u64)
        .checked_add(directory_bytes)
        .ok_or(ReaPeaksError::InvalidHeader("RPKX directory size overflow"))?;
    if header.container_len < payload_region {
        return Err(ReaPeaksError::InvalidHeader(
            "RPKX container shorter than directory",
        ));
    }
    let prefix_len = usize::try_from(payload_region)
        .map_err(|_| ReaPeaksError::InvalidHeader("RPKX directory too large"))?;
    if bytes.len() < prefix_len {
        return Err(ReaPeaksError::Truncated);
    }

    let chunk_count = usize::try_from(header.chunk_count)
        .map_err(|_| ReaPeaksError::InvalidHeader("RPKX chunk count too large"))?;
    let mut entries = Vec::with_capacity(chunk_count.min(1024));
    let mut expected_payload_offset = payload_region;
    for index in 0..chunk_count {
        let off = RPKX_HEADER_SIZE + index * RPKX_DIRECTORY_ENTRY_SIZE;
        let namespace: [u8; 16] = bytes[off..off + 16].try_into().unwrap();
        let kind: [u8; 4] = bytes[off + 16..off + 20].try_into().unwrap();
        let version = u32::from_le_bytes(bytes[off + 20..off + 24].try_into().unwrap());
        let flags = u32::from_le_bytes(bytes[off + 24..off + 28].try_into().unwrap());
        let reserved = u32::from_le_bytes(bytes[off + 28..off + 32].try_into().unwrap());
        if reserved != 0 {
            return Err(ReaPeaksError::Unsupported(
                "RPKX v1 nonzero directory reserved field",
            ));
        }
        let payload_offset = u64::from_le_bytes(bytes[off + 32..off + 40].try_into().unwrap());
        let payload_len = u64::from_le_bytes(bytes[off + 40..off + 48].try_into().unwrap());
        if payload_offset != expected_payload_offset {
            return Err(ReaPeaksError::InvalidHeader(
                "RPKX v1 payloads are not canonically packed",
            ));
        }
        expected_payload_offset = payload_offset
            .checked_add(payload_len)
            .ok_or(ReaPeaksError::InvalidHeader("RPKX payload offset overflow"))?;
        if expected_payload_offset > header.container_len {
            return Err(ReaPeaksError::Truncated);
        }
        entries.push(RpkxEntry {
            key: RpkxKey::new(namespace, kind),
            version,
            flags,
            payload_offset,
            payload_len,
        });
    }
    if expected_payload_offset != header.container_len {
        return Err(ReaPeaksError::InvalidHeader(
            "RPKX packed payloads do not consume container length",
        ));
    }

    Ok(RpkxIndex {
        flags: header.flags,
        source_stamp: header.source_stamp,
        container_offset,
        container_len: header.container_len,
        entries,
    })
}

fn layer_payload_size(
    magic: [u8; 4],
    channels: usize,
    division: i32,
    count: usize,
) -> Result<usize> {
    let bytes_per_value = if division > 0 {
        if magic == *b"RPKM" {
            2usize
        } else {
            4usize
        }
    } else if matches!(
        division,
        TOKEN_SPECTRAL | TOKEN_SPECTROGRAM | TOKEN_LOUDNESS
    ) {
        4usize
    } else if division == TOKEN_LOUDNESS_OLD {
        return Err(ReaPeaksError::Unsupported(
            "cannot locate EOF extension after legacy loudness layer",
        ));
    } else {
        return Err(ReaPeaksError::Unsupported(
            "cannot locate EOF extension after unknown layer token",
        ));
    };
    count
        .checked_mul(channels)
        .and_then(|value| value.checked_mul(bytes_per_value))
        .ok_or(ReaPeaksError::InvalidHeader("layer payload size overflow"))
}

/// Compute the end of the standard REAPER layer region in an in-memory cache.
pub fn standard_end(raw: &[u8]) -> Result<usize> {
    if raw.len() < 18 {
        return Err(ReaPeaksError::Truncated);
    }
    let magic: [u8; 4] = raw[0..4].try_into().unwrap();
    if magic != *b"RPKM" && magic != *b"RPKN" && magic != *b"RPKL" {
        return Err(ReaPeaksError::InvalidMagic(magic));
    }
    let channels = usize::from(raw[4]);
    if channels == 0 {
        return Err(ReaPeaksError::InvalidHeader("channels=0"));
    }
    if read_u32(raw, 6)? == 0 {
        return Err(ReaPeaksError::InvalidHeader("sample_rate=0"));
    }
    let layer_count = usize::from(raw[5]);
    let table_len = layer_count
        .checked_mul(8)
        .ok_or(ReaPeaksError::InvalidHeader("layer table overflow"))?;
    let mut payload_off = 18usize
        .checked_add(table_len)
        .ok_or(ReaPeaksError::InvalidHeader("layer table overflow"))?;
    if payload_off > raw.len() {
        return Err(ReaPeaksError::Truncated);
    }

    for index in 0..layer_count {
        let header_off = 18 + index * 8;
        let division = read_i32(raw, header_off)?;
        let count = read_u32(raw, header_off + 4)? as usize;
        let payload_len = layer_payload_size(magic, channels, division, count)?;
        payload_off = payload_off
            .checked_add(payload_len)
            .ok_or(ReaPeaksError::InvalidHeader("layer payload offset overflow"))?;
        if payload_off > raw.len() {
            return Err(ReaPeaksError::Truncated);
        }
    }
    Ok(payload_off)
}

fn read_exact_required<R: Read>(reader: &mut R, bytes: &mut [u8]) -> Result<()> {
    match reader.read_exact(bytes) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => {
            Err(ReaPeaksError::Truncated)
        }
        Err(error) => Err(error.into()),
    }
}

/// Compute the standard REAPER end from a seekable stream without reading any
/// standard layer payload bytes.
pub fn standard_end_reader<R: Read + Seek>(reader: &mut R) -> Result<u64> {
    reader.seek(SeekFrom::Start(0))?;
    let mut fixed = [0u8; 18];
    read_exact_required(reader, &mut fixed)?;
    let magic: [u8; 4] = fixed[0..4].try_into().unwrap();
    if magic != *b"RPKM" && magic != *b"RPKN" && magic != *b"RPKL" {
        return Err(ReaPeaksError::InvalidMagic(magic));
    }
    let channels = usize::from(fixed[4]);
    if channels == 0 {
        return Err(ReaPeaksError::InvalidHeader("channels=0"));
    }
    if u32::from_le_bytes(fixed[6..10].try_into().unwrap()) == 0 {
        return Err(ReaPeaksError::InvalidHeader("sample_rate=0"));
    }
    let layer_count = usize::from(fixed[5]);
    let table_len = layer_count
        .checked_mul(8)
        .ok_or(ReaPeaksError::InvalidHeader("layer table overflow"))?;
    let mut table = vec![0u8; table_len];
    read_exact_required(reader, &mut table)?;

    let mut payload_off = u64::try_from(18usize + table_len)
        .map_err(|_| ReaPeaksError::InvalidHeader("layer table overflow"))?;
    for index in 0..layer_count {
        let off = index * 8;
        let division = i32::from_le_bytes(table[off..off + 4].try_into().unwrap());
        let count = u32::from_le_bytes(table[off + 4..off + 8].try_into().unwrap()) as usize;
        let payload_len = layer_payload_size(magic, channels, division, count)?;
        payload_off = payload_off
            .checked_add(payload_len as u64)
            .ok_or(ReaPeaksError::InvalidHeader("layer payload offset overflow"))?;
    }
    Ok(payload_off)
}

pub fn reapeaks_source_stamp(raw: &[u8]) -> Result<SourceStamp> {
    if raw.len() < 18 {
        return Err(ReaPeaksError::Truncated);
    }
    Ok(SourceStamp::new(read_u32(raw, 10)?, read_u32(raw, 14)?))
}

/// Read only the RPKX header and directory from an in-memory complete cache.
pub fn read_rpkx_index(raw: &[u8]) -> Result<Option<RpkxIndex>> {
    let standard = standard_end(raw)?;
    let tail = &raw[standard..];
    if tail.len() < 4 || tail.get(0..4) != Some(RPKX_MAGIC.as_slice()) {
        return Ok(None);
    }
    let index = parse_index_prefix(tail, standard as u64)?;
    let container_len = usize::try_from(index.container_len)
        .map_err(|_| ReaPeaksError::InvalidHeader("RPKX length does not fit address space"))?;
    if container_len > tail.len() {
        return Err(ReaPeaksError::Truncated);
    }
    Ok(Some(index))
}

/// Scan a complete seekable `.reapeaks` stream without reading any RPKX
/// payload. Only fixed metadata plus the RPKX directory is materialized.
pub fn scan_rpkx<R: Read + Seek>(reader: &mut R) -> Result<Option<RpkxIndex>> {
    let standard = standard_end_reader(reader)?;
    reader.seek(SeekFrom::Start(standard))?;
    let mut magic = [0u8; 4];
    let read = reader.read(&mut magic)?;
    if read < 4 || magic != RPKX_MAGIC {
        return Ok(None);
    }
    let mut header = [0u8; RPKX_HEADER_SIZE];
    header[..4].copy_from_slice(&magic);
    read_exact_required(reader, &mut header[4..])?;
    let fields = parse_rpkx_header(&header)?;
    let directory_bytes_u64 = u64::from(fields.chunk_count)
        .checked_mul(RPKX_DIRECTORY_ENTRY_SIZE as u64)
        .ok_or(ReaPeaksError::InvalidHeader("RPKX directory size overflow"))?;
    let prefix_len_u64 = (RPKX_HEADER_SIZE as u64)
        .checked_add(directory_bytes_u64)
        .ok_or(ReaPeaksError::InvalidHeader("RPKX directory size overflow"))?;
    if prefix_len_u64 > fields.container_len {
        return Err(ReaPeaksError::InvalidHeader(
            "RPKX container shorter than directory",
        ));
    }
    let directory_bytes = usize::try_from(directory_bytes_u64)
        .map_err(|_| ReaPeaksError::InvalidHeader("RPKX directory too large"))?;
    let mut prefix = Vec::with_capacity(RPKX_HEADER_SIZE + directory_bytes);
    prefix.extend_from_slice(&header);
    prefix.resize(RPKX_HEADER_SIZE + directory_bytes, 0);
    read_exact_required(reader, &mut prefix[RPKX_HEADER_SIZE..])?;
    let index = parse_index_prefix(&prefix, standard)?;

    let physical_len = reader.seek(SeekFrom::End(0))?;
    let required_end = standard
        .checked_add(index.container_len)
        .ok_or(ReaPeaksError::InvalidHeader("RPKX file offset overflow"))?;
    if required_end > physical_len {
        return Err(ReaPeaksError::Truncated);
    }
    Ok(Some(index))
}

/// Read exactly one selected RPKX payload from a seekable stream.
pub fn read_rpkx_payload<R: Read + Seek>(
    reader: &mut R,
    index: &RpkxIndex,
    entry: &RpkxEntry,
) -> Result<Vec<u8>> {
    if !index.entries.iter().any(|candidate| candidate == entry) {
        return Err(ReaPeaksError::InvalidArgument(
            "RPKX entry does not belong to index",
        ));
    }
    let len = usize::try_from(entry.payload_len)
        .map_err(|_| ReaPeaksError::InvalidArgument("RPKX payload too large"))?;
    let mut out = Vec::new();
    out.try_reserve_exact(len)
        .map_err(|_| ReaPeaksError::InvalidArgument("RPKX payload allocation failed"))?;
    out.resize(len, 0);
    let absolute = index
        .container_offset
        .checked_add(entry.payload_offset)
        .ok_or(ReaPeaksError::InvalidHeader("RPKX file offset overflow"))?;
    reader.seek(SeekFrom::Start(absolute))?;
    read_exact_required(reader, &mut out)?;
    Ok(out)
}

/// Stream exactly one selected RPKX payload without materializing it in memory.
pub fn copy_rpkx_payload<R: Read + Seek, W: Write>(
    reader: &mut R,
    index: &RpkxIndex,
    entry: &RpkxEntry,
    writer: &mut W,
) -> Result<u64> {
    if !index.entries.iter().any(|candidate| candidate == entry) {
        return Err(ReaPeaksError::InvalidArgument(
            "RPKX entry does not belong to index",
        ));
    }
    let absolute = index
        .container_offset
        .checked_add(entry.payload_offset)
        .ok_or(ReaPeaksError::InvalidHeader("RPKX file offset overflow"))?;
    reader.seek(SeekFrom::Start(absolute))?;
    let mut limited = reader.take(entry.payload_len);
    let copied = std::io::copy(&mut limited, writer)?;
    if copied != entry.payload_len {
        return Err(ReaPeaksError::Truncated);
    }
    Ok(copied)
}

/// Read and own the complete RPKX container placed immediately after the
/// standard REAPER region.
pub fn read_rpkx(raw: &[u8]) -> Result<Option<RpkxContainer>> {
    let standard = standard_end(raw)?;
    let tail = &raw[standard..];
    if tail.len() < 4 || tail.get(0..4) != Some(RPKX_MAGIC.as_slice()) {
        return Ok(None);
    }
    let (container, _) = RpkxContainer::parse(tail)?;
    Ok(Some(container))
}

fn split_existing_rpkx(raw: &[u8]) -> Result<(usize, Option<RpkxContainer>, &[u8])> {
    let standard = standard_end(raw)?;
    let tail = &raw[standard..];
    if tail.is_empty() {
        return Ok((standard, None, &[]));
    }
    if tail.len() < 4 || tail.get(0..4) != Some(RPKX_MAGIC.as_slice()) {
        return Err(ReaPeaksError::Unsupported(
            "non-RPKX trailing bytes precede extension container",
        ));
    }
    let (container, container_len) = RpkxContainer::parse(tail)?;
    Ok((standard, Some(container), &tail[container_len..]))
}

/// Attach or replace the packed RPKX container while preserving standard
/// REAPER bytes and any opaque bytes following an existing RPKX container.
pub fn attach_rpkx(
    raw: &[u8],
    container: &RpkxContainer,
    policy: RpkxAttachPolicy,
) -> Result<Vec<u8>> {
    let (standard, _, suffix) = split_existing_rpkx(raw)?;
    let stamp = reapeaks_source_stamp(raw)?;
    if policy == RpkxAttachPolicy::RequireMatchingSourceStamp
        && !container.matches_source_stamp(stamp)
    {
        return Err(ReaPeaksError::InvalidArgument(
            "RPKX source stamp does not match .reapeaks header",
        ));
    }
    let encoded = container.encode()?;
    let capacity = standard
        .checked_add(encoded.len())
        .and_then(|value| value.checked_add(suffix.len()))
        .ok_or(ReaPeaksError::InvalidArgument("RPKX output size overflow"))?;
    let mut out = Vec::with_capacity(capacity);
    out.extend_from_slice(&raw[..standard]);
    out.extend_from_slice(&encoded);
    out.extend_from_slice(suffix);
    Ok(out)
}

/// Remove the RPKX container while preserving standard bytes and any opaque
/// suffix that followed the recognized container.
pub fn strip_rpkx(raw: &[u8]) -> Result<Vec<u8>> {
    let (standard, container, suffix) = split_existing_rpkx(raw)?;
    if container.is_none() {
        return Ok(raw.to_vec());
    }
    let mut out = Vec::with_capacity(standard + suffix.len());
    out.extend_from_slice(&raw[..standard]);
    out.extend_from_slice(suffix);
    Ok(out)
}

pub fn set_rpkx_chunk(raw: &[u8], chunk: RpkxChunk) -> Result<Vec<u8>> {
    let (_, existing, _) = split_existing_rpkx(raw)?;
    let stamp = reapeaks_source_stamp(raw)?;
    let mut container = existing.unwrap_or_else(|| RpkxContainer::new(stamp));
    if !container.matches_source_stamp(stamp) {
        return Err(ReaPeaksError::InvalidArgument(
            "existing RPKX source stamp does not match .reapeaks header",
        ));
    }
    container.set_chunk(chunk);
    attach_rpkx(
        raw,
        &container,
        RpkxAttachPolicy::RequireMatchingSourceStamp,
    )
}

pub fn append_rpkx_chunk(raw: &[u8], chunk: RpkxChunk) -> Result<Vec<u8>> {
    let (_, existing, _) = split_existing_rpkx(raw)?;
    let stamp = reapeaks_source_stamp(raw)?;
    let mut container = existing.unwrap_or_else(|| RpkxContainer::new(stamp));
    if !container.matches_source_stamp(stamp) {
        return Err(ReaPeaksError::InvalidArgument(
            "existing RPKX source stamp does not match .reapeaks header",
        ));
    }
    container.append_chunk(chunk);
    attach_rpkx(
        raw,
        &container,
        RpkxAttachPolicy::RequireMatchingSourceStamp,
    )
}

pub fn remove_rpkx_chunks(raw: &[u8], key: RpkxKey) -> Result<Vec<u8>> {
    let (_, existing, _) = split_existing_rpkx(raw)?;
    let Some(mut container) = existing else {
        return Ok(raw.to_vec());
    };
    let stamp = reapeaks_source_stamp(raw)?;
    if !container.matches_source_stamp(stamp) {
        return Err(ReaPeaksError::InvalidArgument(
            "existing RPKX source stamp does not match .reapeaks header",
        ));
    }
    container.remove_chunks(key);
    if container.chunks.is_empty() {
        strip_rpkx(raw)
    } else {
        attach_rpkx(
            raw,
            &container,
            RpkxAttachPolicy::RequireMatchingSourceStamp,
        )
    }
}
