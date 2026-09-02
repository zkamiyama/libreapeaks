use reapeaks::gpu_cache::{GpuCacheView, GpuLayerKind};
use reapeaks::spectrogram::parse_spectrogram_layers;
use reapeaks::ReaPeaks;

fn cache_with_rpkx_tail() -> Vec<u8> {
    let mut raw = Vec::new();
    raw.extend_from_slice(b"RPKN");
    raw.push(1); // mono
    raw.push(1); // one waveform layer
    raw.extend_from_slice(&48_000u32.to_le_bytes());
    raw.extend_from_slice(&0u32.to_le_bytes());
    raw.extend_from_slice(&0u32.to_le_bytes());
    raw.extend_from_slice(&160i32.to_le_bytes());
    raw.extend_from_slice(&1u32.to_le_bytes());
    raw.extend_from_slice(&123i16.to_le_bytes());
    raw.extend_from_slice(&(-456i16).to_le_bytes());
    raw.extend_from_slice(b"RPKX");
    raw.extend_from_slice(&1u32.to_le_bytes());
    raw.extend_from_slice(&4u32.to_le_bytes());
    raw.extend_from_slice(b"META");
    raw
}

#[test]
fn decoded_parser_accepts_and_preserves_eof_extension() {
    let raw = cache_with_rpkx_tail();
    let parsed = ReaPeaks::parse(raw.clone()).unwrap();
    assert_eq!(parsed.raw, raw);
    assert_eq!(parsed.wave_layers.len(), 1);
    assert_eq!(parsed.wave_layers[0].peaks[0].max, 123);
    assert_eq!(parsed.wave_layers[0].peaks[0].min, -456);
}

#[test]
fn gpu_view_accepts_eof_extension_without_exposing_it_as_peak_data() {
    let raw = cache_with_rpkx_tail();
    let view = GpuCacheView::parse(raw.clone()).unwrap();
    assert_eq!(view.raw_len(), raw.len());
    assert_eq!(view.layers(GpuLayerKind::Waveform).len(), 1);
    let tile = view.tile(GpuLayerKind::Waveform, 0, 0, 1).unwrap();
    assert_eq!(tile.bytes, &raw[26..30]);
}

#[test]
fn spectrogram_extractor_ignores_eof_extension() {
    let raw = cache_with_rpkx_tail();
    let (channels, sample_rate, layers) = parse_spectrogram_layers(&raw).unwrap();
    assert_eq!(channels, 1);
    assert_eq!(sample_rate, 48_000);
    assert!(layers.is_empty());
}
