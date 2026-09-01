use crate::error::{ReaPeaksError, Result};
use crate::format::{Version, TOKEN_LOUDNESS, TOKEN_SPECTRAL, TOKEN_SPECTROGRAM};
use crate::spectrogram::{SPECTROGRAM_BYTES_PER_CHANNEL_FRAME, SPECTROGRAM_WORDS_PER_CHANNEL_FRAME};
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GpuLayerKind {
    Waveform,
    Spectral,
    Spectrogram,
    Loudness,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GpuLayerMeta {
    pub kind: GpuLayerKind,
    pub mirrored_division: u32,
    pub record_count: usize,
    pub bytes_per_channel_record: usize,
    pub payload_offset: usize,
    pub payload_len: usize,
}

#[derive(Debug, Clone, Copy)]
pub struct GpuRawTile<'a> {
    pub first_record: usize,
    pub record_count: usize,
    pub channels: usize,
    pub bytes_per_channel_record: usize,
    pub bytes: &'a [u8],
}

/// Lightweight index over the raw RPKN/RPKL payload for direct GPU upload.
///
/// Unlike [`crate::format::ReaPeaks`], this view deliberately does not decode
/// waveform, `-'s'`, `-'g'`, or `-'r'` payloads. A GUI can therefore upload the
/// exact on-disk bytes to integer/float textures and perform all display-domain
/// decoding, gain, palette, and overlay work in a shader.
#[derive(Debug, Clone)]
pub struct GpuCacheView {
    raw: Vec<u8>,
    pub version: Version,
    pub channels: u8,
    pub sample_rate: u32,
    waveform_layers: Vec<GpuLayerMeta>,
    spectral_layers: Vec<GpuLayerMeta>,
    spectrogram_layers: Vec<GpuLayerMeta>,
    loudness_layers: Vec<GpuLayerMeta>,
}

fn checked_end(offset: usize, size: usize) -> Result<usize> {
    offset
        .checked_add(size)
        .ok_or(ReaPeaksError::InvalidHeader("GPU cache offset overflow"))
}

fn read_u32(raw: &[u8], offset: usize) -> Result<u32> {
    let end = checked_end(offset, 4)?;
    let bytes: [u8; 4] = raw
        .get(offset..end)
        .ok_or(ReaPeaksError::Truncated)?
        .try_into()
        .map_err(|_| ReaPeaksError::Truncated)?;
    Ok(u32::from_le_bytes(bytes))
}

impl GpuCacheView {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        Self::parse(fs::read(path)?)
    }

    pub fn parse(raw: Vec<u8>) -> Result<Self> {
        if raw.len() < 18 {
            return Err(ReaPeaksError::Truncated);
        }
        let version = match raw.get(0..4) {
            Some(b"RPKN") => Version::Rpkn,
            Some(b"RPKL") => Version::Rpkl,
            Some(b"RPKM") => {
                return Err(ReaPeaksError::Unsupported(
                    "RPKM direct GPU payload view",
                ))
            }
            Some(bytes) => {
                let magic: [u8; 4] = bytes.try_into().map_err(|_| ReaPeaksError::Truncated)?;
                return Err(ReaPeaksError::InvalidMagic(magic));
            }
            None => return Err(ReaPeaksError::Truncated),
        };
        let channels = raw[4];
        if channels == 0 {
            return Err(ReaPeaksError::InvalidHeader("channels=0"));
        }
        let sample_rate = read_u32(&raw, 6)?;
        if sample_rate == 0 {
            return Err(ReaPeaksError::InvalidHeader("sample_rate=0"));
        }
        let layer_count = usize::from(raw[5]);
        let table_bytes = layer_count
            .checked_mul(8)
            .ok_or(ReaPeaksError::InvalidHeader("layer table overflow"))?;
        let payload_start = checked_end(18, table_bytes)?;
        if payload_start > raw.len() {
            return Err(ReaPeaksError::Truncated);
        }

        let mut headers = Vec::with_capacity(layer_count);
        let mut positive_divisions = Vec::new();
        for index in 0..layer_count {
            let offset = 18 + index * 8;
            let division = read_u32(&raw, offset)? as i32;
            let count = read_u32(&raw, offset + 4)?;
            headers.push((division, count));
            if division > 0 {
                positive_divisions.push(division as u32);
            }
        }

        let channels_usize = usize::from(channels);
        let mut waveform_layers = Vec::new();
        let mut spectral_layers = Vec::new();
        let mut spectrogram_layers = Vec::new();
        let mut loudness_layers = Vec::new();
        let mut spectral_index = 0usize;
        let mut spectrogram_index = 0usize;
        let mut loudness_index = 0usize;
        let mut offset = payload_start;

        for (division, count) in headers {
            let count = count as usize;
            let (kind, mirrored_division, record_count, bytes_per_channel_record) =
                if division > 0 {
                    (GpuLayerKind::Waveform, division as u32, count, 4usize)
                } else if division == TOKEN_SPECTRAL {
                    let mirrored = positive_divisions.get(spectral_index).copied().ok_or(
                        ReaPeaksError::InvalidHeader(
                            "spectral layer without matching waveform layer",
                        ),
                    )?;
                    spectral_index += 1;
                    (GpuLayerKind::Spectral, mirrored, count, 4usize)
                } else if division == TOKEN_SPECTROGRAM {
                    if count % SPECTROGRAM_WORDS_PER_CHANNEL_FRAME != 0 {
                        return Err(ReaPeaksError::InvalidHeader(
                            "spectrogram word count must be divisible by 48",
                        ));
                    }
                    let mirrored = positive_divisions
                        .get(spectrogram_index + 1)
                        .copied()
                        .ok_or(ReaPeaksError::InvalidHeader(
                            "spectrogram layer without matching waveform layer",
                        ))?;
                    spectrogram_index += 1;
                    (
                        GpuLayerKind::Spectrogram,
                        mirrored,
                        count / SPECTROGRAM_WORDS_PER_CHANNEL_FRAME,
                        SPECTROGRAM_BYTES_PER_CHANNEL_FRAME,
                    )
                } else if division == TOKEN_LOUDNESS {
                    if count % 2 != 0 {
                        return Err(ReaPeaksError::InvalidHeader(
                            "loudness value count must be even",
                        ));
                    }
                    let mirrored = positive_divisions
                        .get(loudness_index + 1)
                        .copied()
                        .ok_or(ReaPeaksError::InvalidHeader(
                            "loudness layer without matching waveform layer",
                        ))?;
                    loudness_index += 1;
                    (GpuLayerKind::Loudness, mirrored, count / 2, 8usize)
                } else {
                    return Err(ReaPeaksError::Unsupported(
                        "unknown layer in direct GPU payload view",
                    ));
                };

            let payload_len = record_count
                .checked_mul(channels_usize)
                .and_then(|value| value.checked_mul(bytes_per_channel_record))
                .ok_or(ReaPeaksError::InvalidHeader("GPU layer size overflow"))?;
            let end = checked_end(offset, payload_len)?;
            if end > raw.len() {
                return Err(ReaPeaksError::Truncated);
            }
            let meta = GpuLayerMeta {
                kind,
                mirrored_division,
                record_count,
                bytes_per_channel_record,
                payload_offset: offset,
                payload_len,
            };
            match kind {
                GpuLayerKind::Waveform => waveform_layers.push(meta),
                GpuLayerKind::Spectral => spectral_layers.push(meta),
                GpuLayerKind::Spectrogram => spectrogram_layers.push(meta),
                GpuLayerKind::Loudness => loudness_layers.push(meta),
            }
            offset = end;
        }

        if offset != raw.len() {
            return Err(ReaPeaksError::InvalidHeader("trailing bytes after layers"));
        }

        Ok(Self {
            raw,
            version,
            channels,
            sample_rate,
            waveform_layers,
            spectral_layers,
            spectrogram_layers,
            loudness_layers,
        })
    }

    pub fn raw_len(&self) -> usize {
        self.raw.len()
    }

    pub fn layers(&self, kind: GpuLayerKind) -> &[GpuLayerMeta] {
        match kind {
            GpuLayerKind::Waveform => &self.waveform_layers,
            GpuLayerKind::Spectral => &self.spectral_layers,
            GpuLayerKind::Spectrogram => &self.spectrogram_layers,
            GpuLayerKind::Loudness => &self.loudness_layers,
        }
    }

    pub fn tile(
        &self,
        kind: GpuLayerKind,
        layer_index: usize,
        first_record: usize,
        max_records: usize,
    ) -> Result<GpuRawTile<'_>> {
        if max_records == 0 {
            return Err(ReaPeaksError::InvalidArgument(
                "GPU tile record count must be positive",
            ));
        }
        let layer = self
            .layers(kind)
            .get(layer_index)
            .ok_or(ReaPeaksError::InvalidArgument("GPU layer index out of range"))?;
        if first_record >= layer.record_count {
            return Err(ReaPeaksError::InvalidArgument("GPU tile out of range"));
        }
        let record_count = max_records.min(layer.record_count - first_record);
        let stride = usize::from(self.channels)
            .checked_mul(layer.bytes_per_channel_record)
            .ok_or(ReaPeaksError::InvalidHeader("GPU record stride overflow"))?;
        let start = layer
            .payload_offset
            .checked_add(
                first_record
                    .checked_mul(stride)
                    .ok_or(ReaPeaksError::InvalidHeader("GPU tile offset overflow"))?,
            )
            .ok_or(ReaPeaksError::InvalidHeader("GPU tile offset overflow"))?;
        let len = record_count
            .checked_mul(stride)
            .ok_or(ReaPeaksError::InvalidHeader("GPU tile size overflow"))?;
        let end = checked_end(start, len)?;
        let bytes = self.raw.get(start..end).ok_or(ReaPeaksError::Truncated)?;
        Ok(GpuRawTile {
            first_record,
            record_count,
            channels: usize::from(self.channels),
            bytes_per_channel_record: layer.bytes_per_channel_record,
            bytes,
        })
    }
}
