use reapeaks::{
    default_divisions, generate_f32_reaper, generate_pcm16_reaper, GenerateOptions, ReaPeaks,
    ReaperPeakMode,
};

fn options(sample_rate: u32, channels: usize) -> GenerateOptions {
    GenerateOptions {
        sample_rate,
        channels,
        divisions: default_divisions(sample_rate, 300).to_vec(),
        source_mtime_low32: 0x1122_3344,
        source_size_low32: 0x5566_7788,
        spectral: false,
    }
}

fn pcm16(frames: usize, channels: usize) -> Vec<i16> {
    let mut out = Vec::with_capacity(frames * channels);
    for frame in 0..frames {
        for channel in 0..channels {
            let phase = ((frame * (channel + 3) * 97) % 65_535) as i32 - 32_768;
            out.push(phase.clamp(i16::MIN as i32, i16::MAX as i32) as i16);
        }
    }
    out
}

#[test]
fn pcm16_reaper_modes_have_only_observed_layer_shapes() {
    let opt = options(48_000, 2);
    let pcm = pcm16(96_137, 2);

    let waveform = ReaPeaks::parse(
        generate_pcm16_reaper(&pcm, &opt, ReaperPeakMode::Waveform).unwrap(),
    )
    .unwrap();
    assert_eq!(waveform.wave_layers.len(), 3);
    assert!(waveform.spectral_layers.is_empty());
    assert!(waveform.spectrogram_layers.is_empty());
    assert!(waveform.loudness_layers.is_empty());

    let spectral = ReaPeaks::parse(
        generate_pcm16_reaper(&pcm, &opt, ReaperPeakMode::Spectral).unwrap(),
    )
    .unwrap();
    assert_eq!(spectral.wave_layers.len(), 3);
    assert_eq!(spectral.spectral_layers.len(), 3);
    assert!(spectral.spectrogram_layers.is_empty());
    assert_eq!(spectral.loudness_layers.len(), 2);

    let spectrogram = ReaPeaks::parse(
        generate_pcm16_reaper(&pcm, &opt, ReaperPeakMode::Spectrogram).unwrap(),
    )
    .unwrap();
    assert_eq!(spectrogram.wave_layers.len(), 3);
    assert_eq!(spectrogram.spectral_layers.len(), 3);
    assert_eq!(spectrogram.spectrogram_layers.len(), 2);
    assert_eq!(spectrogram.loudness_layers.len(), 2);
}

#[test]
fn mode_api_overrides_legacy_spectral_flag() {
    let mut opt = options(48_000, 1);
    let pcm = pcm16(48_137, 1);

    opt.spectral = true;
    let waveform = ReaPeaks::parse(
        generate_pcm16_reaper(&pcm, &opt, ReaperPeakMode::Waveform).unwrap(),
    )
    .unwrap();
    assert!(waveform.spectral_layers.is_empty());

    opt.spectral = false;
    let spectral = ReaPeaks::parse(
        generate_pcm16_reaper(&pcm, &opt, ReaperPeakMode::Spectral).unwrap(),
    )
    .unwrap();
    assert_eq!(spectral.spectral_layers.len(), 3);
    assert_eq!(spectral.loudness_layers.len(), 2);
}

#[test]
fn float_reaper_spectrogram_fails_closed() {
    let opt = options(48_000, 1);
    let pcm = vec![0.0f32; 48_000];
    let err = generate_f32_reaper(&pcm, &opt, true, ReaperPeakMode::Spectrogram).unwrap_err();
    assert!(err.to_string().contains("float32 REAPER spectrogram"));
}

#[test]
fn reaper_peak_mode_u8_conversion_is_closed() {
    assert_eq!(ReaperPeakMode::try_from(0).unwrap(), ReaperPeakMode::Waveform);
    assert_eq!(ReaperPeakMode::try_from(1).unwrap(), ReaperPeakMode::Spectral);
    assert_eq!(
        ReaperPeakMode::try_from(2).unwrap(),
        ReaperPeakMode::Spectrogram
    );
    assert!(ReaperPeakMode::try_from(3).is_err());
    assert!(ReaperPeakMode::try_from(u8::MAX).is_err());
}
