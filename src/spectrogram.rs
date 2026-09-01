use crate::error::{ReaPeaksError, Result};
use crate::format::{Version, TOKEN_LOUDNESS, TOKEN_SPECTRAL, TOKEN_SPECTROGRAM};

pub const SPECTROGRAM_BINS: usize = 128;
pub const SPECTROGRAM_BYTES_PER_CHANNEL_FRAME: usize = 192;
pub const SPECTROGRAM_WORDS_PER_CHANNEL_FRAME: usize =
    SPECTROGRAM_BYTES_PER_CHANNEL_FRAME / std::mem::size_of::<u32>();

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpectrogramFrame {
    pub bins: [u16; SPECTROGRAM_BINS],
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpectrogramLayer {
    pub mirrored_division: u32,
    pub frames: Vec<SpectrogramFrame>, // [time][channel]
}

impl SpectrogramLayer {
    pub fn frame_count(&self, channels: usize) -> usize {
        if channels == 0 {
            0
        } else {
            self.frames.len() / channels
        }
    }
}

/// Decode one REAPER spectrogram channel frame.
///
/// Cockos documents 128 12-bit bins packed into 192 bytes. Two bin values use
/// three bytes: `MSB1, (LSN1 << 4) | LSN2, MSB2`.
pub fn decode_spectrogram_frame(bytes: &[u8]) -> Result<SpectrogramFrame> {
    if bytes.len() != SPECTROGRAM_BYTES_PER_CHANNEL_FRAME {
        return Err(ReaPeaksError::InvalidArgument(
            "spectrogram frame must contain exactly 192 bytes",
        ));
    }
    let mut bins = [0u16; SPECTROGRAM_BINS];
    for pair in 0..(SPECTROGRAM_BINS / 2) {
        let offset = pair * 3;
        let msb1 = u16::from(bytes[offset]);
        let low_nibbles = bytes[offset + 1];
        let msb2 = u16::from(bytes[offset + 2]);
        bins[pair * 2] = (msb1 << 4) | u16::from(low_nibbles >> 4);
        bins[pair * 2 + 1] = (msb2 << 4) | u16::from(low_nibbles & 0x0f);
    }
    Ok(SpectrogramFrame { bins })
}

/// Encode one REAPER spectrogram channel frame.
///
/// This is the exact inverse of [`decode_spectrogram_frame`]. Each bin must
/// already fit in the on-disk 12-bit range; values above 4095 are rejected
/// rather than silently truncated.
pub fn encode_spectrogram_frame(
    frame: &SpectrogramFrame,
) -> Result<[u8; SPECTROGRAM_BYTES_PER_CHANNEL_FRAME]> {
    let mut out = [0u8; SPECTROGRAM_BYTES_PER_CHANNEL_FRAME];
    for pair in 0..(SPECTROGRAM_BINS / 2) {
        let first = frame.bins[pair * 2];
        let second = frame.bins[pair * 2 + 1];
        if first > 0x0fff || second > 0x0fff {
            return Err(ReaPeaksError::InvalidArgument(
                "spectrogram bins must fit in 12 bits",
            ));
        }
        let offset = pair * 3;
        out[offset] = (first >> 4) as u8;
        out[offset + 1] = (((first & 0x0f) << 4) | (second & 0x0f)) as u8;
        out[offset + 2] = (second >> 4) as u8;
    }
    Ok(out)
}

fn checked_end(offset: usize, size: usize) -> Result<usize> {
    offset.checked_add(size).ok_or(ReaPeaksError::InvalidHeader(
        "spectrogram layer offset overflow",
    ))
}

fn read_u32(bytes: &[u8], offset: usize) -> Result<u32> {
    let end = checked_end(offset, 4)?;
    let raw: [u8; 4] = bytes
        .get(offset..end)
        .ok_or(ReaPeaksError::Truncated)?
        .try_into()
        .map_err(|_| ReaPeaksError::Truncated)?;
    Ok(u32::from_le_bytes(raw))
}

fn read_i32(bytes: &[u8], offset: usize) -> Result<i32> {
    Ok(read_u32(bytes, offset)? as i32)
}

/// Extract `-'g'` spectrogram layers directly from a `.reapeaks` byte slice.
///
/// REAPER 7.79 uses the `-'g'` header count as a count of 32-bit words per
/// channel, not a count of time frames. One time frame is 192 bytes = 48 words
/// per channel. The first `-'g'` layer mirrors the second positive waveform
/// division, and subsequent layers mirror following positive divisions.
pub fn parse_spectrogram_layers(bytes: &[u8]) -> Result<(u8, u32, Vec<SpectrogramLayer>)> {
    if bytes.len() < 18 {
        return Err(ReaPeaksError::Truncated);
    }
    let version = match bytes.get(0..4) {
        Some(b"RPKM") => Version::Rpkm,
        Some(b"RPKN") => Version::Rpkn,
        Some(b"RPKL") => Version::Rpkl,
        Some(raw) => {
            let magic: [u8; 4] = raw.try_into().map_err(|_| ReaPeaksError::Truncated)?;
            return Err(ReaPeaksError::InvalidMagic(magic));
        }
        None => return Err(ReaPeaksError::Truncated),
    };
    let channels = bytes[4];
    if channels == 0 {
        return Err(ReaPeaksError::InvalidHeader("channels=0"));
    }
    let layer_count = usize::from(bytes[5]);
    let sample_rate = read_u32(bytes, 6)?;
    if sample_rate == 0 {
        return Err(ReaPeaksError::InvalidHeader("sample_rate=0"));
    }
    let table_bytes = layer_count
        .checked_mul(8)
        .ok_or(ReaPeaksError::InvalidHeader("layer table overflow"))?;
    let mut payload_offset = checked_end(18, table_bytes)?;
    if payload_offset > bytes.len() {
        return Err(ReaPeaksError::Truncated);
    }

    let mut headers = Vec::with_capacity(layer_count);
    let mut positive_divisions = Vec::new();
    for index in 0..layer_count {
        let offset = 18 + index * 8;
        let division = read_i32(bytes, offset)?;
        let count = read_u32(bytes, offset + 4)?;
        headers.push((division, count));
        if division > 0 {
            positive_divisions.push(division as u32);
        }
    }

    let channels_usize = usize::from(channels);
    let mut spectrogram_index = 0usize;
    let mut layers = Vec::new();

    for (index, (division, count)) in headers.iter().copied().enumerate() {
        let count_usize = count as usize;
        let payload_size = if division > 0 {
            let bytes_per_peak = match version {
                Version::Rpkm => 2usize,
                Version::Rpkn | Version::Rpkl => 4usize,
            };
            count_usize
                .checked_mul(channels_usize)
                .and_then(|value| value.checked_mul(bytes_per_peak))
                .ok_or(ReaPeaksError::InvalidHeader("wave layer size overflow"))?
        } else if division == TOKEN_SPECTRAL || division == TOKEN_LOUDNESS {
            count_usize
                .checked_mul(channels_usize)
                .and_then(|value| value.checked_mul(4))
                .ok_or(ReaPeaksError::InvalidHeader(
                    "auxiliary layer size overflow",
                ))?
        } else if division == TOKEN_SPECTROGRAM {
            if count_usize % SPECTROGRAM_WORDS_PER_CHANNEL_FRAME != 0 {
                return Err(ReaPeaksError::InvalidHeader(
                    "spectrogram word count is not divisible by 48",
                ));
            }
            count_usize
                .checked_mul(channels_usize)
                .and_then(|value| value.checked_mul(4))
                .ok_or(ReaPeaksError::InvalidHeader(
                    "spectrogram layer size overflow",
                ))?
        } else {
            // The legacy -'l' payload size is not established. We cannot walk
            // safely past it to locate later layers.
            return Err(ReaPeaksError::Unsupported(
                "layer before/among spectrograms has unknown payload layout",
            ));
        };

        let payload_end = checked_end(payload_offset, payload_size)?;
        if payload_end > bytes.len() {
            return Err(ReaPeaksError::Truncated);
        }

        if division == TOKEN_SPECTROGRAM {
            let wave_index =
                spectrogram_index
                    .checked_add(1)
                    .ok_or(ReaPeaksError::InvalidHeader(
                        "spectrogram layer index overflow",
                    ))?;
            let mirrored_division =
                positive_divisions
                    .get(wave_index)
                    .copied()
                    .ok_or(ReaPeaksError::InvalidHeader(
                        "spectrogram layer without matching waveform layer",
                    ))?;
            let time_frames = count_usize / SPECTROGRAM_WORDS_PER_CHANNEL_FRAME;
            let value_count =
                time_frames
                    .checked_mul(channels_usize)
                    .ok_or(ReaPeaksError::InvalidHeader(
                        "spectrogram frame count overflow",
                    ))?;
            let mut frames = Vec::with_capacity(value_count);
            for value_index in 0..value_count {
                let start = payload_offset
                    .checked_add(
                        value_index
                            .checked_mul(SPECTROGRAM_BYTES_PER_CHANNEL_FRAME)
                            .ok_or(ReaPeaksError::InvalidHeader(
                                "spectrogram frame offset overflow",
                            ))?,
                    )
                    .ok_or(ReaPeaksError::InvalidHeader(
                        "spectrogram frame offset overflow",
                    ))?;
                let end = checked_end(start, SPECTROGRAM_BYTES_PER_CHANNEL_FRAME)?;
                frames.push(decode_spectrogram_frame(&bytes[start..end])?);
            }
            layers.push(SpectrogramLayer {
                mirrored_division,
                frames,
            });
            spectrogram_index += 1;
        }
        payload_offset = payload_end;

        if index + 1 == headers.len() && payload_offset != bytes.len() {
            return Err(ReaPeaksError::InvalidHeader("trailing bytes after layers"));
        }
    }

    Ok((channels, sample_rate, layers))
}
