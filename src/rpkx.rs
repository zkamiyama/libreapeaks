use crate::error::{ReaPeaksError, Result};
use crate::format::{TOKEN_LOUDNESS, TOKEN_LOUDNESS_OLD, TOKEN_SPECTRAL, TOKEN_SPECTROGRAM};
use crate::source::SourceStamp;

pub const RPKX_MAGIC: [u8; 4] = *b"RPKX";
pub const RPKX_VERSION: u16 = 1;
pub const RPKX_HEADER_SIZE: usize = 32;
pub const RPKX_CHUNK_HEADER_SIZE: usize = 40;

/// Opaque 128-bit application namespace.
///
/// RPKX does not assign semantic meaning to namespace bytes. Applications
/// should use a stable UUID (RFC 4122/9562 byte order) so independently
/// developed chunk types do not collide.
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

/// One RPKX v1 container.
///
/// libreapeaks owns only framing, source binding, and safe coexistence. Chunk
/// payloads, schemas, timebases, compression, and application semantics remain
/// private to the namespace that defines them.
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
    ///
    /// All existing chunks with the same key are removed. The replacement is
    /// inserted at the first previous position so unrelated chunk ordering is
    /// stable; if the key did not exist it is appended.
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

    pub fn encode(&self) -> Result<Vec<u8>> {
        let chunk_count = u32::try_from(self.chunks.len())
            .map_err(|_| ReaPeaksError::InvalidArgument("too many RPKX chunks"))?;
        let chunks_len = self.chunks.iter().try_fold(0usize, |total, chunk| {
            let chunk_len = RPKX_CHUNK_HEADER_SIZE
                .checked_add(chunk.payload.len())
                .ok_or(ReaPeaksError::InvalidArgument("RPKX chunk size overflow"))?;
            total
                .checked_add(chunk_len)
                .ok_or(ReaPeaksError::InvalidArgument("RPKX size overflow"))
        })?;
        let total_len = RPKX_HEADER_SIZE
            .checked_add(chunks_len)
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

        for chunk in &self.chunks {
            let payload_len = u64::try_from(chunk.payload.len())
                .map_err(|_| ReaPeaksError::InvalidArgument("RPKX payload too large"))?;
            out.extend_from_slice(&chunk.key.namespace);
            out.extend_from_slice(&chunk.key.kind);
            out.extend_from_slice(&chunk.version.to_le_bytes());
            out.extend_from_slice(&chunk.flags.to_le_bytes());
            out.extend_from_slice(&0u32.to_le_bytes());
            out.extend_from_slice(&payload_len.to_le_bytes());
            out.extend_from_slice(&chunk.payload);
        }
        Ok(out)
    }

    /// Parse an RPKX container at the beginning of `bytes`.
    ///
    /// The returned length is the container length, allowing callers to retain
    /// unrelated bytes that may follow the RPKX container at physical EOF.
    pub fn parse(bytes: &[u8]) -> Result<(Self, usize)> {
        if bytes.len() < RPKX_HEADER_SIZE {
            return Err(ReaPeaksError::Truncated);
        }
        if bytes.get(0..4) != Some(RPKX_MAGIC.as_slice()) {
            return Err(ReaPeaksError::InvalidMagic(bytes[0..4].try_into().unwrap()));
        }
        let version = u16::from_le_bytes(bytes[4..6].try_into().unwrap());
        if version != RPKX_VERSION {
            return Err(ReaPeaksError::Unsupported("RPKX container version"));
        }
        let header_size = usize::from(u16::from_le_bytes(bytes[6..8].try_into().unwrap()));
        if header_size != RPKX_HEADER_SIZE {
            return Err(ReaPeaksError::Unsupported("RPKX v1 header size"));
        }
        let flags = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
        let chunk_count = u32::from_le_bytes(bytes[12..16].try_into().unwrap()) as usize;
        let container_len_u64 = u64::from_le_bytes(bytes[16..24].try_into().unwrap());
        let container_len = usize::try_from(container_len_u64)
            .map_err(|_| ReaPeaksError::InvalidHeader("RPKX length does not fit address space"))?;
        if container_len < RPKX_HEADER_SIZE || container_len > bytes.len() {
            return Err(ReaPeaksError::Truncated);
        }
        let source_stamp = SourceStamp::new(
            u32::from_le_bytes(bytes[24..28].try_into().unwrap()),
            u32::from_le_bytes(bytes[28..32].try_into().unwrap()),
        );

        let mut off = RPKX_HEADER_SIZE;
        let mut chunks = Vec::with_capacity(chunk_count.min(1024));
        for _ in 0..chunk_count {
            let header_end = off
                .checked_add(RPKX_CHUNK_HEADER_SIZE)
                .ok_or(ReaPeaksError::InvalidHeader("RPKX chunk offset overflow"))?;
            if header_end > container_len {
                return Err(ReaPeaksError::Truncated);
            }
            let namespace: [u8; 16] = bytes[off..off + 16].try_into().unwrap();
            let kind: [u8; 4] = bytes[off + 16..off + 20].try_into().unwrap();
            let version = u32::from_le_bytes(bytes[off + 20..off + 24].try_into().unwrap());
            let chunk_flags = u32::from_le_bytes(bytes[off + 24..off + 28].try_into().unwrap());
            let reserved = u32::from_le_bytes(bytes[off + 28..off + 32].try_into().unwrap());
            if reserved != 0 {
                return Err(ReaPeaksError::Unsupported("RPKX v1 nonzero chunk reserved field"));
            }
            let payload_len_u64 =
                u64::from_le_bytes(bytes[off + 32..off + 40].try_into().unwrap());
            let payload_len = usize::try_from(payload_len_u64)
                .map_err(|_| ReaPeaksError::InvalidHeader("RPKX payload length too large"))?;
            let payload_start = header_end;
            let payload_end = payload_start
                .checked_add(payload_len)
                .ok_or(ReaPeaksError::InvalidHeader("RPKX payload offset overflow"))?;
            if payload_end > container_len {
                return Err(ReaPeaksError::Truncated);
            }
            chunks.push(RpkxChunk {
                key: RpkxKey::new(namespace, kind),
                version,
                flags: chunk_flags,
                payload: bytes[payload_start..payload_end].to_vec(),
            });
            off = payload_end;
        }
        if off != container_len {
            return Err(ReaPeaksError::InvalidHeader(
                "RPKX chunk count does not consume container length",
            ));
        }
        Ok((
            Self {
                flags,
                source_stamp,
                chunks,
            },
            container_len,
        ))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RpkxAttachPolicy {
    RequireMatchingSourceStamp,
    AllowSourceStampMismatch,
}

impl Default for RpkxAttachPolicy {
    fn default() -> Self {
        Self::RequireMatchingSourceStamp
    }
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

/// Compute the end of the standard REAPER layer region.
///
/// This is intentionally independent of physical EOF. Modern RPKN/RPKL/RPKM
/// layer sizes are derivable from the table. Terminal legacy `-'l'` payloads
/// remain ambiguous and therefore cannot safely host a discoverable EOF RPKX.
pub fn standard_end(raw: &[u8]) -> Result<usize> {
    if raw.len() < 18 {
        return Err(ReaPeaksError::Truncated);
    }
    let wave_bytes_per_channel_peak = match raw.get(0..4) {
        Some(b"RPKM") => 2usize,
        Some(b"RPKN") | Some(b"RPKL") => 4usize,
        Some(bytes) => {
            let magic: [u8; 4] = bytes.try_into().map_err(|_| ReaPeaksError::Truncated)?;
            return Err(ReaPeaksError::InvalidMagic(magic));
        }
        None => return Err(ReaPeaksError::Truncated),
    };
    let channels = usize::from(raw[4]);
    if channels == 0 {
        return Err(ReaPeaksError::InvalidHeader("channels=0"));
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
        let bytes_per_value = if division > 0 {
            wave_bytes_per_channel_peak
        } else if matches!(division, TOKEN_SPECTRAL | TOKEN_SPECTROGRAM | TOKEN_LOUDNESS) {
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
        let payload_len = count
            .checked_mul(channels)
            .and_then(|value| value.checked_mul(bytes_per_value))
            .ok_or(ReaPeaksError::InvalidHeader("layer payload size overflow"))?;
        payload_off = payload_off
            .checked_add(payload_len)
            .ok_or(ReaPeaksError::InvalidHeader("layer payload offset overflow"))?;
        if payload_off > raw.len() {
            return Err(ReaPeaksError::Truncated);
        }
    }
    Ok(payload_off)
}

pub fn reapeaks_source_stamp(raw: &[u8]) -> Result<SourceStamp> {
    if raw.len() < 18 {
        return Err(ReaPeaksError::Truncated);
    }
    Ok(SourceStamp::new(read_u32(raw, 10)?, read_u32(raw, 14)?))
}

/// Read the RPKX container placed immediately after the standard REAPER region.
pub fn read_rpkx(raw: &[u8]) -> Result<Option<RpkxContainer>> {
    let standard_end = standard_end(raw)?;
    let tail = &raw[standard_end..];
    if tail.len() < 4 || tail.get(0..4) != Some(RPKX_MAGIC.as_slice()) {
        return Ok(None);
    }
    let (container, _) = RpkxContainer::parse(tail)?;
    Ok(Some(container))
}

fn split_existing_rpkx(raw: &[u8]) -> Result<(usize, Option<RpkxContainer>, &[u8])> {
    let standard_end = standard_end(raw)?;
    let tail = &raw[standard_end..];
    if tail.is_empty() {
        return Ok((standard_end, None, &[]));
    }
    if tail.len() < 4 || tail.get(0..4) != Some(RPKX_MAGIC.as_slice()) {
        return Err(ReaPeaksError::Unsupported(
            "non-RPKX trailing bytes precede extension container",
        ));
    }
    let (container, container_len) = RpkxContainer::parse(tail)?;
    Ok((standard_end, Some(container), &tail[container_len..]))
}

/// Attach or replace the RPKX container while preserving standard REAPER bytes
/// and any opaque bytes following an existing RPKX container.
pub fn attach_rpkx(
    raw: &[u8],
    container: &RpkxContainer,
    policy: RpkxAttachPolicy,
) -> Result<Vec<u8>> {
    let (standard_end, _, suffix) = split_existing_rpkx(raw)?;
    let stamp = reapeaks_source_stamp(raw)?;
    if policy == RpkxAttachPolicy::RequireMatchingSourceStamp
        && !container.matches_source_stamp(stamp)
    {
        return Err(ReaPeaksError::InvalidArgument(
            "RPKX source stamp does not match .reapeaks header",
        ));
    }
    let encoded = container.encode()?;
    let capacity = standard_end
        .checked_add(encoded.len())
        .and_then(|value| value.checked_add(suffix.len()))
        .ok_or(ReaPeaksError::InvalidArgument("RPKX output size overflow"))?;
    let mut out = Vec::with_capacity(capacity);
    out.extend_from_slice(&raw[..standard_end]);
    out.extend_from_slice(&encoded);
    out.extend_from_slice(suffix);
    Ok(out)
}

/// Remove the RPKX container while preserving standard bytes and any opaque
/// bytes that followed the recognized container.
pub fn strip_rpkx(raw: &[u8]) -> Result<Vec<u8>> {
    let (standard_end, container, suffix) = split_existing_rpkx(raw)?;
    if container.is_none() {
        return Ok(raw.to_vec());
    }
    let mut out = Vec::with_capacity(standard_end + suffix.len());
    out.extend_from_slice(&raw[..standard_end]);
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
    attach_rpkx(raw, &container, RpkxAttachPolicy::RequireMatchingSourceStamp)
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
    attach_rpkx(raw, &container, RpkxAttachPolicy::RequireMatchingSourceStamp)
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
        attach_rpkx(raw, &container, RpkxAttachPolicy::RequireMatchingSourceStamp)
    }
}
