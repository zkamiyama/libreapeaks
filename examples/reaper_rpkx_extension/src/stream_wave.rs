use reapeaks::format::{encode, GeneratedLayer, LayerHeader, Version};
use reapeaks::{default_divisions, quantize_pcm16_peak};
use std::io;

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct I16Extrema {
    pub max: i16,
    pub min: i16,
}

fn err(s: impl ToString) -> io::Error {
    io::Error::other(s.to_string())
}

fn bucket_count(frames: usize, division: usize, fine: usize, finest: bool) -> usize {
    if frames == 0 {
        0
    } else if finest || division % fine != 0 || frames % fine != 0 {
        frames.div_ceil(division)
    } else {
        frames / division
    }
}

fn encoded_layer(div: u32, peaks: &[I16Extrema], channels: usize) -> io::Result<GeneratedLayer> {
    if channels == 0 || peaks.len() % channels != 0 {
        return Err(err("invalid streamed waveform/channel layout"));
    }
    let peak_count = u32::try_from(peaks.len() / channels).map_err(err)?;
    let mut bytes = Vec::with_capacity(peaks.len().saturating_mul(4));
    for peak in peaks {
        bytes.extend_from_slice(&peak.max.to_le_bytes());
        bytes.extend_from_slice(&peak.min.to_le_bytes());
    }
    Ok(GeneratedLayer {
        header: LayerHeader {
            division: div as i32,
            peak_count,
        },
        bytes,
    })
}

fn aggregate(
    fine: &[I16Extrema],
    channels: usize,
    factor: usize,
    output_count: usize,
) -> io::Result<Vec<I16Extrema>> {
    if channels == 0 || factor == 0 {
        return Err(err("invalid streamed waveform aggregation geometry"));
    }
    let records = fine.len() / channels;
    let mut out = Vec::with_capacity(output_count.saturating_mul(channels));
    for p in 0..output_count {
        let a = p.saturating_mul(factor);
        let b = a.saturating_add(factor).min(records);
        if a >= b {
            return Err(err("streamed waveform aggregation requested an empty source group"));
        }
        for c in 0..channels {
            let mut max = i16::MIN;
            let mut min = i16::MAX;
            for i in a..b {
                let q = fine[i * channels + c];
                max = max.max(q.max);
                min = min.min(q.min);
            }
            out.push(I16Extrema { max, min });
        }
    }
    Ok(out)
}

/// Encode ordinary PCM16 waveform peaks from fine-bucket raw extrema.
///
/// The caller decodes media incrementally and retains only one max/min pair per
/// fine bucket. This reproduces REAPER's nested waveform mipmaps without a
/// whole-file PCM allocation. Spectral/spectrogram modes deliberately keep the
/// existing batch path until their windowed DSP has an incremental equivalent.
pub fn generate_pcm16(
    raw_fine: &[I16Extrema],
    frames: usize,
    channels: usize,
    sample_rate: u32,
    peaks_per_second: u32,
    source_mtime_low32: u32,
    source_size_low32: u32,
) -> io::Result<Vec<u8>> {
    if channels == 0 || channels > u8::MAX as usize || sample_rate == 0 || peaks_per_second == 0 {
        return Err(err("invalid streamed waveform geometry"));
    }
    let divisions = default_divisions(sample_rate, peaks_per_second);
    let fine_division = divisions[0] as usize;
    let fine_count = bucket_count(frames, fine_division, fine_division, true);
    let expected = fine_count
        .checked_mul(channels)
        .ok_or_else(|| err("streamed waveform pair count overflow"))?;
    if raw_fine.len() != expected {
        return Err(err(format!(
            "streamed waveform fine pair mismatch: got {}, expected {expected}",
            raw_fine.len()
        )));
    }

    // PCM16's REAPER peak quantizer is monotonic, so quantizing each bucket's
    // raw extrema is byte-identical to quantizing every sample before min/max.
    let fine: Vec<I16Extrema> = raw_fine
        .iter()
        .map(|p| {
            if p.max < p.min {
                return Err(err("invalid streamed PCM16 extrema"));
            }
            Ok(I16Extrema {
                max: quantize_pcm16_peak(p.max),
                min: quantize_pcm16_peak(p.min),
            })
        })
        .collect::<io::Result<_>>()?;

    let mut layers = Vec::with_capacity(divisions.len());
    layers.push(encoded_layer(divisions[0], &fine, channels)?);
    for &div in divisions.iter().skip(1) {
        let division = div as usize;
        let count = bucket_count(frames, division, fine_division, false);
        let factor = division / fine_division;
        let peaks = aggregate(&fine, channels, factor, count)?;
        layers.push(encoded_layer(div, &peaks, channels)?);
    }

    encode(
        Version::Rpkn,
        channels as u8,
        sample_rate,
        source_mtime_low32,
        source_size_low32,
        &layers,
    )
    .map_err(err)
}

#[cfg(test)]
mod tests {
    use super::*;
    use reapeaks::{generate_pcm16_reaper, GenerateOptions, ReaperPeakMode};

    fn raw_fine(pcm: &[i16], frames: usize, channels: usize, fine: usize) -> Vec<I16Extrema> {
        let mut out = Vec::new();
        for p in 0..frames.div_ceil(fine) {
            let a = p * fine;
            let b = (a + fine).min(frames);
            for c in 0..channels {
                let mut max = i16::MIN;
                let mut min = i16::MAX;
                for f in a..b {
                    let v = pcm[f * channels + c];
                    max = max.max(v);
                    min = min.min(v);
                }
                out.push(I16Extrema { max, min });
            }
        }
        out
    }

    #[test]
    fn streamed_waveform_matches_batch_across_eof_boundaries() {
        let rate = 1200u32;
        let pps = 300u32;
        let divisions = default_divisions(rate, pps).to_vec();
        for frames in 1usize..=1103 {
            let channels = 2usize;
            let pcm: Vec<i16> = (0..frames * channels)
                .map(|i| ((i as i32 * 7919 + 1237) % 65536 - 32768) as i16)
                .collect();
            let options = GenerateOptions {
                sample_rate: rate,
                channels,
                divisions: divisions.clone(),
                source_mtime_low32: 123,
                source_size_low32: 456,
                spectral: false,
            };
            let batch = generate_pcm16_reaper(&pcm, &options, ReaperPeakMode::Waveform).unwrap();
            let streamed = generate_pcm16(
                &raw_fine(&pcm, frames, channels, divisions[0] as usize),
                frames,
                channels,
                rate,
                pps,
                123,
                456,
            )
            .unwrap();
            assert_eq!(streamed, batch, "frames={frames}");
        }
    }

    #[test]
    fn twenty_five_minute_stereo_geometry_is_peak_bounded() {
        let rate = 48_000u32;
        let pps = 300u32;
        let frames = rate as usize * 25 * 60;
        let channels = 2usize;
        let fine = default_divisions(rate, pps)[0] as usize;
        let count = frames.div_ceil(fine);
        let peaks = vec![I16Extrema { max: 1234, min: -2345 }; count * channels];
        assert!(peaks.len() * std::mem::size_of::<I16Extrema>() < 4 * 1024 * 1024);
        let image = generate_pcm16(&peaks, frames, channels, rate, pps, 1, 2).unwrap();
        assert!(image.len() < 5 * 1024 * 1024);
    }
}