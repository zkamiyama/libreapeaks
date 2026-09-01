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
    clippy::too_many_arguments
)]

pub mod error;
pub mod ffi;
pub mod format;
pub mod generate;
pub mod loudness;
pub mod pyramid;
pub mod reaper_generate;

// Strict mode reuses low-level DSP/aggregation helpers from spectral.rs, but
// deliberately bypasses its public source-domain scheduler entrypoints. Those
// entrypoints remain live in the normal build; suppress dead-code only on this
// private strict alias so `clippy -D warnings` keeps its normal coverage.
#[cfg(feature = "strict-wdl")]
#[path = "spectral_strict.rs"]
pub mod spectral;
#[cfg(not(feature = "strict-wdl"))]
pub mod spectral;
#[cfg(feature = "strict-wdl")]
#[allow(dead_code)]
#[path = "spectral.rs"]
mod spectral_base;

pub mod spectrogram;
mod spectrogram_generate;
pub mod texture;
pub mod wave;

#[cfg(feature = "python")]
mod python;

pub use error::{ReaPeaksError, Result};
pub use format::{Header, LoudnessLayer, LoudnessPeak, ReaPeaks, SpectralPeak, Version, WaveLayer};
pub use generate::{
    generate_f32, generate_f32_mode3, generate_pcm16, generate_pcm16_mode3,
    generate_pcm16_mode3_with_spectrogram, GenerateOptions,
};
pub use pyramid::{WaveLevelMeta, WavePyramid, WaveTile, WaveTileKey, WaveViewPlan};
pub use reaper_generate::{generate_f32_reaper, generate_pcm16_reaper, ReaperPeakMode};
pub use spectrogram::{
    decode_spectrogram_frame, encode_spectrogram_frame, parse_spectrogram_layers, SpectrogramFrame,
    SpectrogramLayer, SPECTROGRAM_BINS, SPECTROGRAM_BYTES_PER_CHANNEL_FRAME,
    SPECTROGRAM_WORDS_PER_CHANNEL_FRAME,
};
pub use texture::{
    encode_envelope_rgba8, encode_spectral_rgba8, encode_wave_tile_rgba8, render_waveform_rgba8,
    render_waveform_rgba8_scaled, RgbaImage,
};
pub use wave::{
    decode_peak_code, decode_rpkl_code, decode_rpkn_code, default_divisions, quantize_pcm16_peak,
    quantize_rpkl_f32, quantize_rpkn_f32, PeakPair, WaveEncoding,
};
