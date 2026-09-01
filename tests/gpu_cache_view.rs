use reapeaks::{
    decode_spectrogram_frame, default_divisions, generate_pcm16_reaper, GenerateOptions,
    GpuCacheView, GpuLayerKind, ReaPeaks, ReaperPeakMode,
};

fn fixture() -> (Vec<u8>, usize) {
    let sample_rate = 48_000u32;
    let channels = 2usize;
    let frames = sample_rate as usize * 2 + 137;
    let mut pcm = Vec::with_capacity(frames * channels);
    for frame in 0..frames {
        let left = (((frame * 97) % 65_535) as i32 - 32_768) as i16;
        let right = (((frame * 313) % 65_535) as i32 - 32_768) as i16;
        pcm.extend_from_slice(&[left, right]);
    }
    let options = GenerateOptions {
        sample_rate,
        channels,
        divisions: default_divisions(sample_rate, 300).to_vec(),
        source_mtime_low32: 0x1234_5678,
        source_size_low32: 0x9abc_def0,
        spectral: false,
    };
    (
        generate_pcm16_reaper(&pcm, &options, ReaperPeakMode::Spectrogram).unwrap(),
        channels,
    )
}

#[test]
fn direct_gpu_view_indexes_observed_mode3_layers_without_transforming_payloads() {
    let (bytes, channels) = fixture();
    let decoded = ReaPeaks::parse(bytes.clone()).unwrap();
    let gpu = GpuCacheView::parse(bytes).unwrap();

    assert_eq!(gpu.layers(GpuLayerKind::Waveform).len(), 3);
    assert_eq!(gpu.layers(GpuLayerKind::Spectral).len(), 3);
    assert_eq!(gpu.layers(GpuLayerKind::Spectrogram).len(), 2);
    assert_eq!(gpu.layers(GpuLayerKind::Loudness).len(), 2);
    assert_eq!(
        gpu.layers(GpuLayerKind::Waveform)[0].bytes_per_channel_record,
        4
    );
    assert_eq!(
        gpu.layers(GpuLayerKind::Spectral)[0].bytes_per_channel_record,
        4
    );
    assert_eq!(
        gpu.layers(GpuLayerKind::Spectrogram)[0].bytes_per_channel_record,
        192
    );
    assert_eq!(
        gpu.layers(GpuLayerKind::Loudness)[0].bytes_per_channel_record,
        8
    );

    let wave = gpu.tile(GpuLayerKind::Waveform, 0, 0, 11).unwrap();
    for record in 0..wave.record_count {
        for channel in 0..channels {
            let offset = (record * channels + channel) * 4;
            let max = i16::from_le_bytes([wave.bytes[offset], wave.bytes[offset + 1]]);
            let min = i16::from_le_bytes([wave.bytes[offset + 2], wave.bytes[offset + 3]]);
            let expected = decoded.wave_layers[0].peaks[record * channels + channel];
            assert_eq!((max, min), (expected.max, expected.min));
        }
    }

    let spectral = gpu.tile(GpuLayerKind::Spectral, 0, 0, 11).unwrap();
    for record in 0..spectral.record_count {
        for channel in 0..channels {
            let offset = (record * channels + channel) * 4;
            let code = u32::from_le_bytes(spectral.bytes[offset..offset + 4].try_into().unwrap());
            assert_eq!(
                code,
                decoded.spectral_layers[0].peaks[record * channels + channel].code()
            );
        }
    }

    let spectrogram = gpu.tile(GpuLayerKind::Spectrogram, 0, 0, 7).unwrap();
    for record in 0..spectrogram.record_count {
        for channel in 0..channels {
            let offset = (record * channels + channel) * 192;
            let frame = decode_spectrogram_frame(&spectrogram.bytes[offset..offset + 192]).unwrap();
            assert_eq!(
                frame,
                decoded.spectrogram_layers[0].frames[record * channels + channel]
            );
        }
    }

    let loudness = gpu.tile(GpuLayerKind::Loudness, 0, 0, 7).unwrap();
    for record in 0..loudness.record_count {
        for channel in 0..channels {
            let offset = (record * channels + channel) * 8;
            let momentary =
                f32::from_le_bytes(loudness.bytes[offset..offset + 4].try_into().unwrap());
            let short_term =
                f32::from_le_bytes(loudness.bytes[offset + 4..offset + 8].try_into().unwrap());
            let expected = &decoded.loudness_layers[0].peaks[record * channels + channel];
            assert_eq!(momentary.to_bits(), expected.momentary_energy.to_bits());
            assert_eq!(short_term.to_bits(), expected.short_term_energy.to_bits());
        }
    }
}

#[test]
fn direct_gpu_view_rejects_out_of_range_and_zero_record_requests() {
    let (bytes, _channels) = fixture();
    let gpu = GpuCacheView::parse(bytes).unwrap();
    assert!(gpu.tile(GpuLayerKind::Waveform, 0, 0, 0).is_err());
    assert!(gpu.tile(GpuLayerKind::Waveform, usize::MAX, 0, 1).is_err());
    let count = gpu.layers(GpuLayerKind::Waveform)[0].record_count;
    assert!(gpu.tile(GpuLayerKind::Waveform, 0, count, 1).is_err());
}
