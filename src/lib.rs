pub mod error;
pub mod ffi;
pub mod format;
pub mod generate;
pub mod pyramid;
pub mod spectral;
pub mod texture;
pub mod wave;

#[cfg(feature = "python")]
mod python;

pub use error::{ReaPeaksError, Result};
pub use format::{Header, ReaPeaks, SpectralPeak, Version, WaveLayer};
pub use generate::{generate_f32, generate_pcm16, GenerateOptions};
pub use pyramid::{WaveLevelMeta, WavePyramid, WaveTile, WaveTileKey, WaveViewPlan};
pub use texture::{
    encode_envelope_rgba8, encode_spectral_rgba8, encode_wave_tile_rgba8,
    render_waveform_rgba8, render_waveform_rgba8_scaled, RgbaImage,
};
pub use wave::{
    decode_peak_code, decode_rpkl_code, decode_rpkn_code, default_divisions,
    quantize_pcm16_peak, quantize_rpkl_f32, quantize_rpkn_f32, PeakPair, WaveEncoding,
};
