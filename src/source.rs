use crate::format::{Header, ReaPeaks};
use crate::generate::GenerateOptions;
use crate::Result;
use std::fs::{self, Metadata};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

/// The source-file freshness fields stored in a `.reapeaks` header.
///
/// REAPER stores only the low 32 bits of the source modification time in whole
/// Unix seconds and the low 32 bits of the source byte size. This is a cache
/// freshness stamp, not a collision-resistant file identity. Applications that
/// need to detect replacement while a source is offline should additionally
/// keep their own stronger runtime fingerprint (for example file-id/inode,
/// nanosecond mtime, and full size).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct SourceStamp {
    pub mtime_low32: u32,
    pub size_low32: u32,
}

impl SourceStamp {
    pub const fn new(mtime_low32: u32, size_low32: u32) -> Self {
        Self {
            mtime_low32,
            size_low32,
        }
    }

    /// Build the REAPER header stamp from whole Unix seconds and a full byte size.
    ///
    /// Both values are reduced modulo 2^32, matching the two fields available in
    /// the `.reapeaks` header.
    pub const fn from_unix_seconds_and_size(mtime_seconds: i64, size: u64) -> Self {
        Self {
            mtime_low32: mtime_seconds as u32,
            size_low32: size as u32,
        }
    }

    /// Build a stamp from a `SystemTime` and full byte size.
    ///
    /// Sub-second precision is intentionally discarded because the REAPER cache
    /// header stores whole seconds only.
    pub fn from_system_time_and_size(modified: SystemTime, size: u64) -> Self {
        let mtime_low32 = match modified.duration_since(UNIX_EPOCH) {
            Ok(duration) => duration.as_secs() as u32,
            Err(error) => {
                let duration = error.duration();
                let seconds_before_epoch = duration.as_secs()
                    + u64::from(duration.subsec_nanos() != 0);
                0u32.wrapping_sub(seconds_before_epoch as u32)
            }
        };
        Self {
            mtime_low32,
            size_low32: size as u32,
        }
    }

    /// Build the REAPER-compatible cache stamp from filesystem metadata.
    pub fn from_metadata(metadata: &Metadata) -> Result<Self> {
        Ok(Self::from_system_time_and_size(
            metadata.modified()?,
            metadata.len(),
        ))
    }

    /// Stat a source path and build the REAPER-compatible cache stamp.
    pub fn from_path(path: impl AsRef<Path>) -> Result<Self> {
        Self::from_metadata(&fs::metadata(path)?)
    }

    /// Exact comparison with the two source fields stored in a cache header.
    ///
    /// This is deliberately stricter than REAPER's documented acceptance of
    /// small mtime offsets (including offsets near one hour). It is a safe,
    /// conservative application freshness check, not an emulation of that
    /// tolerance policy.
    pub const fn matches_header(self, header: &Header) -> bool {
        self.mtime_low32 == header.source_mtime_low32 && self.size_low32 == header.source_size_low32
    }
}

impl Header {
    /// Return the source stamp encoded in this `.reapeaks` header.
    pub const fn source_stamp(&self) -> SourceStamp {
        SourceStamp::new(self.source_mtime_low32, self.source_size_low32)
    }

    /// Exactly compare this header with an already captured source stamp.
    pub const fn matches_source_stamp(&self, stamp: SourceStamp) -> bool {
        stamp.matches_header(self)
    }

    /// Exactly compare this header with filesystem metadata.
    pub fn matches_source_metadata(&self, metadata: &Metadata) -> Result<bool> {
        Ok(self.matches_source_stamp(SourceStamp::from_metadata(metadata)?))
    }

    /// Stat a source path and exactly compare it with this header's source stamp.
    pub fn matches_source_path(&self, path: impl AsRef<Path>) -> Result<bool> {
        Ok(self.matches_source_stamp(SourceStamp::from_path(path)?))
    }
}

impl ReaPeaks {
    /// Return the source stamp encoded in this cache.
    pub const fn source_stamp(&self) -> SourceStamp {
        self.header.source_stamp()
    }

    /// Exactly compare this cache with an already captured source stamp.
    pub const fn matches_source_stamp(&self, stamp: SourceStamp) -> bool {
        self.header.matches_source_stamp(stamp)
    }

    /// Stat a source path and exactly compare it with this cache's source stamp.
    pub fn matches_source_path(&self, path: impl AsRef<Path>) -> Result<bool> {
        self.header.matches_source_path(path)
    }
}

impl GenerateOptions {
    /// Replace the source metadata fields with one coherent REAPER source stamp.
    pub fn set_source_stamp(&mut self, stamp: SourceStamp) {
        self.source_mtime_low32 = stamp.mtime_low32;
        self.source_size_low32 = stamp.size_low32;
    }

    /// Builder-style variant of [`GenerateOptions::set_source_stamp`].
    pub fn with_source_stamp(mut self, stamp: SourceStamp) -> Self {
        self.set_source_stamp(stamp);
        self
    }
}
