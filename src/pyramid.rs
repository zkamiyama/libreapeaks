use crate::format::{ReaPeaks, WaveLayer};
use crate::wave::{PeakPair, WaveEncoding};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WaveLevelMeta {
    pub division: u64,
    pub peak_count: usize,
    pub native: bool,
}

#[derive(Debug, Clone)]
struct NativeWaveLevel {
    division: u64,
    peaks: Vec<PeakPair>, // [peak][channel]
}

#[derive(Debug, Clone, Copy)]
enum LevelSource {
    Native { native_index: usize },
    DerivedFromFine { factor: usize },
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct WaveViewPlan {
    pub level_index: usize,
    pub division: u64,
    pub first_peak: usize,
    pub peak_count: usize,
    pub peaks_per_pixel: f64,
}

/// Stable cache key for GUI-side CPU/GPU LRU caches.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct WaveTileKey {
    pub level_index: u16,
    pub tile_index: u64,
}

#[derive(Debug, Clone)]
pub struct WaveTile {
    pub key: WaveTileKey,
    pub first_peak: usize,
    pub peak_count: usize,
    pub peaks: Vec<PeakPair>, // [peak][channel]
}

/// Multiresolution waveform index optimized for GUI zooming.
///
/// Native REAPER mipmaps are kept exactly. Additional geometric display levels
/// are metadata-only: their peaks are aggregated lazily from the finest native
/// level per requested range/tile. This avoids the ~33% RAM overhead of an
/// eagerly materialized ratio-4 pyramid while giving smooth zoom transitions.
///
/// Tiles are fixed at 4096 peaks by default. Frontends can use WaveTileKey as a
/// stable identity for an LRU of CPU buffers or GPU textures.
#[derive(Debug, Clone)]
pub struct WavePyramid {
    pub channels: usize,
    pub source_frames: u64,
    pub encoding: WaveEncoding,
    pub levels: Vec<WaveLevelMeta>,
    pub tile_peaks: usize,
    native_levels: Vec<NativeWaveLevel>,
    sources: Vec<LevelSource>,
    finest_native_index: usize,
}

impl WavePyramid {
    pub fn from_reapeaks(file: &ReaPeaks, display_ratio: usize) -> Self {
        let channels = file.header.channels as usize;
        // .ReaPeaks does not store exact source frame count. The finest wave
        // layer provides a safe upper bound (at most division-1 frames long).
        let source_frames = file
            .wave_layers
            .first()
            .map(|l| l.peak_count(channels) as u64 * l.division as u64)
            .unwrap_or(0);
        Self::from_native(
            channels,
            source_frames,
            &file.wave_layers,
            display_ratio,
            WaveEncoding::from_version(file.header.version),
        )
    }

    pub fn from_native(
        channels: usize,
        source_frames: u64,
        native: &[WaveLayer],
        display_ratio: usize,
        encoding: WaveEncoding,
    ) -> Self {
        let mut native_levels: Vec<NativeWaveLevel> = native
            .iter()
            .map(|x| NativeWaveLevel {
                division: x.division as u64,
                peaks: x.peaks.clone(),
            })
            .collect();
        native_levels.sort_by_key(|x| x.division);

        let finest_native_index = 0usize;
        let mut desc: Vec<(WaveLevelMeta, LevelSource)> = Vec::new();
        for (i, n) in native_levels.iter().enumerate() {
            let count = if channels == 0 { 0 } else { n.peaks.len() / channels };
            desc.push((
                WaveLevelMeta {
                    division: n.division,
                    peak_count: count,
                    native: true,
                },
                LevelSource::Native { native_index: i },
            ));
        }

        if let Some(fine) = native_levels.first() {
            let ratio = display_ratio.max(2);
            let fine_count = if channels == 0 { 0 } else { fine.peaks.len() / channels };
            let mut factor = ratio;
            loop {
                let div = fine.division.saturating_mul(factor as u64);
                let count = if fine_count == 0 {
                    0
                } else {
                    (fine_count + factor - 1) / factor
                };
                if !desc.iter().any(|(m, _)| m.division == div) {
                    desc.push((
                        WaveLevelMeta {
                            division: div,
                            peak_count: count,
                            native: false,
                        },
                        LevelSource::DerivedFromFine { factor },
                    ));
                }
                if count <= 1 || div >= source_frames.max(1) {
                    break;
                }
                let Some(next) = factor.checked_mul(ratio) else { break };
                factor = next;
            }
        }

        desc.sort_by_key(|x| x.0.division);
        let (levels, sources): (Vec<_>, Vec<_>) = desc.into_iter().unzip();
        Self {
            channels,
            source_frames,
            encoding,
            levels,
            tile_peaks: 4096,
            native_levels,
            sources,
            finest_native_index,
        }
    }

    pub fn choose_level(
        &self,
        start_frame: u64,
        end_frame: u64,
        pixel_width: usize,
    ) -> Option<WaveViewPlan> {
        if self.levels.is_empty() || pixel_width == 0 || end_frame <= start_frame {
            return None;
        }
        let span = end_frame - start_frame;
        let frames_per_pixel = span as f64 / pixel_width as f64;
        // Around 1.5 source extrema per pixel is a useful balance: enough data
        // for antialiased/filled drawing without oversampling large zoom-outs.
        let target_div = (frames_per_pixel / 1.5).max(1.0);

        let mut best_i = 0usize;
        let mut best_score = f64::INFINITY;
        for (i, level) in self.levels.iter().enumerate() {
            let score = ((level.division as f64 / target_div).ln()).abs();
            if score < best_score {
                best_score = score;
                best_i = i;
            }
        }
        let level = self.levels[best_i];
        let first = (start_frame / level.division) as usize;
        let last_exclusive = ((end_frame + level.division - 1) / level.division) as usize;
        let a = first.min(level.peak_count);
        let b = last_exclusive.min(level.peak_count).max(a);
        Some(WaveViewPlan {
            level_index: best_i,
            division: level.division,
            first_peak: a,
            peak_count: b - a,
            peaks_per_pixel: frames_per_pixel / level.division as f64,
        })
    }

    pub fn read_range(
        &self,
        level_index: usize,
        first_peak: usize,
        peak_count: usize,
    ) -> Option<Vec<PeakPair>> {
        let meta = *self.levels.get(level_index)?;
        let source = *self.sources.get(level_index)?;
        let first = first_peak.min(meta.peak_count);
        let count = peak_count.min(meta.peak_count.saturating_sub(first));
        if count == 0 || self.channels == 0 {
            return Some(Vec::new());
        }
        match source {
            LevelSource::Native { native_index } => {
                let level = self.native_levels.get(native_index)?;
                let a = first * self.channels;
                let b = (first + count) * self.channels;
                Some(level.peaks.get(a..b)?.to_vec())
            }
            LevelSource::DerivedFromFine { factor } => {
                let fine = self.native_levels.get(self.finest_native_index)?;
                let fine_n = fine.peaks.len() / self.channels;
                let mut out = Vec::with_capacity(count * self.channels);
                for p in first..first + count {
                    let a = p.saturating_mul(factor).min(fine_n);
                    let b = a.saturating_add(factor).min(fine_n);
                    for c in 0..self.channels {
                        let mut mx = i16::MIN;
                        let mut mn = i16::MAX;
                        for i in a..b {
                            let q = fine.peaks[i * self.channels + c];
                            mx = mx.max(q.max);
                            mn = mn.min(q.min);
                        }
                        if a == b {
                            mx = 0;
                            mn = 0;
                        }
                        out.push(PeakPair { max: mx, min: mn });
                    }
                }
                Some(out)
            }
        }
    }

    pub fn read_plan(&self, plan: WaveViewPlan) -> Option<Vec<PeakPair>> {
        self.read_range(plan.level_index, plan.first_peak, plan.peak_count)
    }

    pub fn tiles_for_plan(&self, plan: WaveViewPlan) -> Vec<WaveTileKey> {
        if plan.peak_count == 0 {
            return Vec::new();
        }
        let tile = self.tile_peaks.max(1);
        let a = plan.first_peak / tile;
        let b = (plan.first_peak + plan.peak_count - 1) / tile;
        (a..=b)
            .map(|t| WaveTileKey {
                level_index: plan.level_index as u16,
                tile_index: t as u64,
            })
            .collect()
    }

    pub fn tile(&self, key: WaveTileKey) -> Option<WaveTile> {
        let meta = *self.levels.get(key.level_index as usize)?;
        let first = (key.tile_index as usize).saturating_mul(self.tile_peaks);
        if first >= meta.peak_count {
            return None;
        }
        let count = self.tile_peaks.min(meta.peak_count - first);
        let peaks = self.read_range(key.level_index as usize, first, count)?;
        Some(WaveTile {
            key,
            first_peak: first,
            peak_count: count,
            peaks,
        })
    }

    pub fn tile_count(&self, level_index: usize) -> Option<usize> {
        let meta = *self.levels.get(level_index)?;
        let t = self.tile_peaks.max(1);
        Some((meta.peak_count + t - 1) / t)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn derived_levels_are_lazy_and_range_correct() {
        let native = vec![WaveLayer {
            division: 4,
            peaks: vec![
                PeakPair { max: 1, min: -1 },
                PeakPair { max: 4, min: -2 },
                PeakPair { max: 3, min: -7 },
                PeakPair { max: 2, min: -3 },
                PeakPair { max: 9, min: -1 },
                PeakPair { max: 5, min: -6 },
                PeakPair { max: 2, min: -4 },
                PeakPair { max: 1, min: -2 },
            ],
        }];
        let p = WavePyramid::from_native(1, 32, &native, 2, WaveEncoding::Rpkn);
        let idx = p.levels.iter().position(|x| x.division == 8).unwrap();
        assert!(!p.levels[idx].native);
        let x = p.read_range(idx, 0, 4).unwrap();
        assert_eq!(x[0], PeakPair { max: 4, min: -2 });
        assert_eq!(x[1], PeakPair { max: 3, min: -7 });
        assert_eq!(x[2], PeakPair { max: 9, min: -6 });
        assert_eq!(x[3], PeakPair { max: 2, min: -4 });
    }
}
