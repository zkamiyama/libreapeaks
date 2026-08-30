// REAPER-compatibility code intentionally preserves recovered control flow,
// arithmetic spelling, and C ABI shapes. Keep these exceptions explicit so
// `clippy -D warnings` still rejects every other diagnostic.
#![allow(
    clippy::chunks_exact_to_as_chunks,
    clippy::manual_checked_ops,
    clippy::manual_contains,
    clippy::manual_div_ceil,
    clippy::manual_is_multiple_of,
    clippy::missing_safety_doc,
    clippy::needless_range_loop,
    clippy::too_many_arguments,
)]

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
