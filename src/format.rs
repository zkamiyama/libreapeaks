use crate::error::{ReaPeaksError, Result};
use crate::wave::PeakPair;
use std::fs;
use std::path::Path;

pub const TOKEN_SPECTRAL: i32 = -('s' as i32);
pub const TOKEN_SPECTROGRAM: i32 = -('g' as i32);
pub const TOKEN_LOUDNESS: i32 = -('r' as i32);
pub const TOKEN_LOUDNESS_OLD: i32 = -('l' as i32);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Version {
    Rpkm,
    Rpkn,
    Rpkl,
}

impl Version {
    pub fn magic(self) -> [u8; 4] {
        match self {
            Self::Rpkm => *b"RPKM",
            Self::Rpkn => *b"RPKN",
            Self::Rpkl => *b"RPKL",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub version: Version,
    pub channels: u8,
    pub mipmap_count: u8,
    pub sample_rate: u32,
    pub source_mtime_low32: u32,
    pub source_size_low32: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LayerKind {
    Wave,
    Spectral,
    Spectrogram,
    Loudness,
    LoudnessOld,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LayerHeader {
    pub division: i32,
    pub peak_count: u32,
}

impl LayerHeader {
    pub fn kind(self) -> LayerKind {
        match self.division {
            d if d > 0 => LayerKind::Wave,
            TOKEN_SPECTRAL => LayerKind::Spectral,
            TOKEN_SPECTROGRAM => LayerKind::Spectrogram,
            TOKEN_LOUDNESS => LayerKind::Loudness,
            TOKEN_LOUDNESS_OLD => LayerKind::LoudnessOld,
            _ => LayerKind::Wave,
        }
    }
}

#[derive(Debug, Clone)]
pub struct WaveLayer {
    pub division: u32,
    pub peaks: Vec<PeakPair>, // [peak][channel]
}

impl WaveLayer {
    pub fn peak_count(&self, channels: usize) -> usize {
        if channels == 0 {
            0
        } else {
            self.peaks.len() / channels
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct SpectralPeak {
    pub frequency_hz: u16,
    pub density: u16,
}

impl SpectralPeak {
    pub fn from_code(code: u32) -> Self {
        Self {
            frequency_hz: (code & 0x7fff) as u16,
            density: ((code >> 15) & 0x3fff) as u16,
        }
    }

    pub fn code(self) -> u32 {
        (self.frequency_hz.min(0x7fff) as u32) | ((self.density.min(0x3fff) as u32) << 15)
    }
}

#[derive(Debug, Clone)]
pub struct SpectralLayer {
    pub mirrored_division: u32,
    pub peaks: Vec<SpectralPeak>, // [peak][channel]
}

#[derive(Debug, Clone)]
pub struct ReaPeaks {
    pub header: Header,
    pub layer_headers: Vec<LayerHeader>,
    pub wave_layers: Vec<WaveLayer>,
    pub spectral_layers: Vec<SpectralLayer>,
    pub raw: Vec<u8>,
}

fn get<const N: usize>(b: &[u8], o: usize) -> Result<[u8; N]> {
    let end = o.checked_add(N).ok_or(ReaPeaksError::Truncated)?;
    b.get(o..end)
        .ok_or(ReaPeaksError::Truncated)?
        .try_into()
        .map_err(|_| ReaPeaksError::Truncated)
}

fn is_known_layer_division(division: i32) -> bool {
    division > 0
        || matches!(
            division,
            TOKEN_SPECTRAL | TOKEN_SPECTROGRAM | TOKEN_LOUDNESS | TOKEN_LOUDNESS_OLD
        )
}

fn checked_end(off: usize, size: usize) -> Result<usize> {
    off.checked_add(size)
        .ok_or(ReaPeaksError::InvalidHeader("layer offset overflow"))
}

fn layer_payload_size(version: Version, h: LayerHeader, channels: usize) -> Option<usize> {
    let n = h.peak_count as usize;
    match h.kind() {
        LayerKind::Wave => match version {
            Version::Rpkm => n.checked_mul(channels)?.checked_mul(2),
            Version::Rpkn | Version::Rpkl => n.checked_mul(channels)?.checked_mul(4),
        },
        LayerKind::Spectral => n.checked_mul(channels)?.checked_mul(4),
        LayerKind::Spectrogram => n.checked_mul(channels)?.checked_mul(192),
        // Official documentation says two floats (8 bytes), while REAPER 7.79
        // fixtures encountered in this project use 4 bytes/sample for the final
        // 'r' layers. Treat a terminal loudness layer as "consume remainder".
        LayerKind::Loudness | LayerKind::LoudnessOld => None,
    }
}

impl ReaPeaks {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        Self::parse(fs::read(path)?)
    }

    pub fn parse(raw: Vec<u8>) -> Result<Self> {
        if raw.len() < 18 {
            return Err(ReaPeaksError::Truncated);
        }
        let magic = get::<4>(&raw, 0)?;
        let version = match &magic {
            b"RPKM" => Version::Rpkm,
            b"RPKN" => Version::Rpkn,
            b"RPKL" => Version::Rpkl,
            _ => return Err(ReaPeaksError::InvalidMagic(magic)),
        };
        let channels = raw[4];
        let mipmap_count = raw[5];
        if channels == 0 {
            return Err(ReaPeaksError::InvalidHeader("channels=0"));
        }
        let sample_rate = u32::from_le_bytes(get::<4>(&raw, 6)?);
        if sample_rate == 0 {
            return Err(ReaPeaksError::InvalidHeader("sample_rate=0"));
        }
        let header = Header {
            version,
            channels,
            mipmap_count,
            sample_rate,
            source_mtime_low32: u32::from_le_bytes(get::<4>(&raw, 10)?),
            source_size_low32: u32::from_le_bytes(get::<4>(&raw, 14)?),
        };
        let mut off = 18usize;
        let mut hs = Vec::with_capacity(mipmap_count as usize);
        for _ in 0..mipmap_count {
            let division = i32::from_le_bytes(get::<4>(&raw, off)?);
            let peak_count = u32::from_le_bytes(get::<4>(&raw, checked_end(off, 4)?)?);
            off = checked_end(off, 8)?;
            hs.push(LayerHeader {
                division,
                peak_count,
            });
        }

        let channels_usize = channels as usize;
        let positive_divs: Vec<u32> = hs
            .iter()
            .filter_map(|h| (h.division > 0).then_some(h.division as u32))
            .collect();
        let mut spectral_index = 0usize;
        let mut wave_layers = Vec::new();
        let mut spectral_layers = Vec::new();

        for (idx, h) in hs.iter().copied().enumerate() {
            if !is_known_layer_division(h.division) {
                return Err(ReaPeaksError::Unsupported("unknown layer token"));
            }
            match h.kind() {
                LayerKind::Wave => {
                    let n = h.peak_count as usize;
                    let sample_count = n
                        .checked_mul(channels_usize)
                        .ok_or(ReaPeaksError::InvalidHeader("layer size overflow"))?;
                    match version {
                        Version::Rpkm => {
                            let size = sample_count
                                .checked_mul(2)
                                .ok_or(ReaPeaksError::InvalidHeader("layer size overflow"))?;
                            let end = checked_end(off, size)?;
                            if end > raw.len() {
                                return Err(ReaPeaksError::Truncated);
                            }
                            off = end;
                        }
                        Version::Rpkn | Version::Rpkl => {
                            let size = sample_count
                                .checked_mul(4)
                                .ok_or(ReaPeaksError::InvalidHeader("layer size overflow"))?;
                            let end = checked_end(off, size)?;
                            if end > raw.len() {
                                return Err(ReaPeaksError::Truncated);
                            }
                            let mut peaks = Vec::with_capacity(sample_count);
                            for i in 0..sample_count {
                                let p = off + i * 4;
                                peaks.push(PeakPair {
                                    max: i16::from_le_bytes(get::<2>(&raw, p)?),
                                    min: i16::from_le_bytes(get::<2>(&raw, p + 2)?),
                                });
                            }
                            off = end;
                            wave_layers.push(WaveLayer {
                                division: h.division as u32,
                                peaks,
                            });
                        }
                    }
                }
                LayerKind::Spectral => {
                    let n = h.peak_count as usize;
                    let sample_count = n
                        .checked_mul(channels_usize)
                        .ok_or(ReaPeaksError::InvalidHeader("spectral size overflow"))?;
                    let size = sample_count
                        .checked_mul(4)
                        .ok_or(ReaPeaksError::InvalidHeader("spectral size overflow"))?;
                    let end = checked_end(off, size)?;
                    if end > raw.len() {
                        return Err(ReaPeaksError::Truncated);
                    }
                    let mirrored_division = positive_divs.get(spectral_index).copied().ok_or(
                        ReaPeaksError::InvalidHeader(
                            "spectral layer without matching waveform layer",
                        ),
                    )?;
                    let mut peaks = Vec::with_capacity(sample_count);
                    for i in 0..sample_count {
                        let code = u32::from_le_bytes(get::<4>(&raw, off + i * 4)?);
                        peaks.push(SpectralPeak::from_code(code));
                    }
                    off = end;
                    spectral_layers.push(SpectralLayer {
                        mirrored_division,
                        peaks,
                    });
                    spectral_index += 1;
                }
                LayerKind::Spectrogram => {
                    let size = layer_payload_size(version, h, channels_usize)
                        .ok_or(ReaPeaksError::Unsupported("spectrogram layout"))?;
                    let end = checked_end(off, size)?;
                    if end > raw.len() {
                        return Err(ReaPeaksError::Truncated);
                    }
                    off = end;
                }
                LayerKind::Loudness | LayerKind::LoudnessOld => {
                    // REAPER 7.79 fixtures use at least 4 bytes/channel/sample.
                    // A final loudness layer may contain a larger opaque payload,
                    // but it may not be shorter than the observed minimum.
                    let size = (h.peak_count as usize)
                        .checked_mul(channels_usize)
                        .and_then(|x| x.checked_mul(4))
                        .ok_or(ReaPeaksError::InvalidHeader("loudness size overflow"))?;
                    let end = checked_end(off, size)?;
                    if end > raw.len() {
                        return Err(ReaPeaksError::Truncated);
                    }
                    off = if idx + 1 == hs.len() { raw.len() } else { end };
                }
            }
        }

        if off != raw.len() {
            return Err(ReaPeaksError::InvalidHeader("trailing bytes after layers"));
        }

        Ok(Self {
            header,
            layer_headers: hs,
            wave_layers,
            spectral_layers,
            raw,
        })
    }
}

#[derive(Debug, Clone)]
pub struct GeneratedLayer {
    pub header: LayerHeader,
    pub bytes: Vec<u8>,
}

pub fn encode(
    version: Version,
    channels: u8,
    sample_rate: u32,
    source_mtime_low32: u32,
    source_size_low32: u32,
    layers: &[GeneratedLayer],
) -> Result<Vec<u8>> {
    if channels == 0 {
        return Err(ReaPeaksError::InvalidArgument("channels=0"));
    }
    if sample_rate == 0 {
        return Err(ReaPeaksError::InvalidArgument("sample_rate=0"));
    }
    if version == Version::Rpkm {
        return Err(ReaPeaksError::Unsupported("RPKM writer"));
    }
    if layers.len() > u8::MAX as usize {
        return Err(ReaPeaksError::InvalidArgument("too many layers"));
    }
    for layer in layers {
        if !is_known_layer_division(layer.header.division) {
            return Err(ReaPeaksError::InvalidArgument("unknown layer token"));
        }
        if let Some(expected) = layer_payload_size(version, layer.header, channels as usize) {
            if expected != layer.bytes.len() {
                return Err(ReaPeaksError::InvalidArgument(
                    "layer payload length mismatch",
                ));
            }
        }
    }
    let total_payload = layers
        .iter()
        .try_fold(0usize, |total, layer| total.checked_add(layer.bytes.len()))
        .ok_or(ReaPeaksError::InvalidArgument("encoded size overflow"))?;
    let capacity = layers
        .len()
        .checked_mul(8)
        .and_then(|x| x.checked_add(18))
        .and_then(|x| x.checked_add(total_payload))
        .filter(|&x| x <= isize::MAX as usize)
        .ok_or(ReaPeaksError::InvalidArgument("encoded file too large"))?;
    let mut out = Vec::with_capacity(capacity);
    out.extend_from_slice(&version.magic());
    out.push(channels);
    out.push(layers.len() as u8);
    out.extend_from_slice(&sample_rate.to_le_bytes());
    out.extend_from_slice(&source_mtime_low32.to_le_bytes());
    out.extend_from_slice(&source_size_low32.to_le_bytes());
    for layer in layers {
        out.extend_from_slice(&layer.header.division.to_le_bytes());
        out.extend_from_slice(&layer.header.peak_count.to_le_bytes());
    }
    for layer in layers {
        out.extend_from_slice(&layer.bytes);
    }
    Ok(out)
}

pub fn encode_rpkn(
    channels: u8,
    sample_rate: u32,
    source_mtime_low32: u32,
    source_size_low32: u32,
    layers: &[GeneratedLayer],
) -> Result<Vec<u8>> {
    encode(
        Version::Rpkn,
        channels,
        sample_rate,
        source_mtime_low32,
        source_size_low32,
        layers,
    )
}
