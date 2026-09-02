use crate::format::ReaPeaks;
use crate::generate::{generate_f32, generate_pcm16, GenerateOptions};
use crate::pyramid::{WavePyramid, WaveTileKey};
use crate::source::SourceStamp;
use crate::texture::{
    encode_envelope_rgba8, encode_spectral_rgba8, encode_wave_tile_rgba8,
    render_waveform_rgba8_scaled,
};
use crate::wave::{default_divisions, WaveEncoding};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};

fn py_err(e: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(e.to_string())
}

#[pyclass(name = "ReaPeaks")]
pub struct PyReaPeaks {
    file: ReaPeaks,
    pyramid: WavePyramid,
}

#[pymethods]
impl PyReaPeaks {
    #[staticmethod]
    pub fn open(path: &str) -> PyResult<Self> {
        let file = ReaPeaks::open(path).map_err(py_err)?;
        let pyramid = WavePyramid::from_reapeaks(&file, 4);
        Ok(Self { file, pyramid })
    }

    #[getter]
    pub fn sample_rate(&self) -> u32 {
        self.file.header.sample_rate
    }

    #[getter]
    pub fn channels(&self) -> u8 {
        self.file.header.channels
    }

    #[getter]
    pub fn source_mtime_low32(&self) -> u32 {
        self.file.header.source_mtime_low32
    }

    #[getter]
    pub fn source_size_low32(&self) -> u32 {
        self.file.header.source_size_low32
    }

    pub fn source_stamp(&self) -> (u32, u32) {
        let stamp = self.file.source_stamp();
        (stamp.mtime_low32, stamp.size_low32)
    }

    pub fn matches_source_stamp(&self, source_mtime_low32: u32, source_size_low32: u32) -> bool {
        self.file.matches_source_stamp(SourceStamp::new(
            source_mtime_low32,
            source_size_low32,
        ))
    }

    pub fn matches_source(&self, path: &str) -> PyResult<bool> {
        self.file.matches_source_path(path).map_err(py_err)
    }

    #[getter]
    pub fn wave_encoding(&self) -> &'static str {
        match self.pyramid.encoding {
            WaveEncoding::Rpkn => "RPKN",
            WaveEncoding::Rpkl => "RPKL",
        }
    }

    #[getter]
    pub fn tile_peaks(&self) -> usize {
        self.pyramid.tile_peaks
    }

    pub fn levels(&self) -> Vec<(u64, usize, bool)> {
        self.pyramid
            .levels
            .iter()
            .map(|l| (l.division, l.peak_count, l.native))
            .collect()
    }

    pub fn plan_view(
        &self,
        start_frame: u64,
        end_frame: u64,
        width: usize,
    ) -> PyResult<(usize, u64, usize, usize, f64)> {
        let p = self
            .pyramid
            .choose_level(start_frame, end_frame, width)
            .ok_or_else(|| py_err("empty view"))?;
        Ok((
            p.level_index,
            p.division,
            p.first_peak,
            p.peak_count,
            p.peaks_per_pixel,
        ))
    }

    pub fn tiles_for_view(
        &self,
        start_frame: u64,
        end_frame: u64,
        width: usize,
    ) -> PyResult<Vec<(usize, u64)>> {
        let p = self
            .pyramid
            .choose_level(start_frame, end_frame, width)
            .ok_or_else(|| py_err("empty view"))?;
        Ok(self
            .pyramid
            .tiles_for_plan(p)
            .into_iter()
            .map(|k| (k.level_index as usize, k.tile_index))
            .collect())
    }

    /// Returns (first_peak, width, height, RGBA8 bytes).
    pub fn tile_texture<'py>(
        &self,
        py: Python<'py>,
        level_index: usize,
        tile_index: u64,
    ) -> PyResult<(usize, usize, usize, Bound<'py, PyBytes>)> {
        let level_index =
            u16::try_from(level_index).map_err(|_| py_err("level index out of range"))?;
        let tile = self
            .pyramid
            .tile(WaveTileKey {
                level_index,
                tile_index,
            })
            .ok_or_else(|| py_err("tile out of range"))?;
        let first = tile.first_peak;
        let img = encode_wave_tile_rgba8(&tile, self.pyramid.channels);
        Ok((first, img.width, img.height, PyBytes::new(py, &img.data)))
    }

    /// Materializes a complete level. Prefer tile_texture() for long media.
    pub fn envelope_texture<'py>(
        &self,
        py: Python<'py>,
        level_index: usize,
    ) -> PyResult<(usize, usize, Bound<'py, PyBytes>)> {
        let meta = self
            .pyramid
            .levels
            .get(level_index)
            .copied()
            .ok_or_else(|| py_err("level index out of range"))?;
        let peaks = self
            .pyramid
            .read_range(level_index, 0, meta.peak_count)
            .ok_or_else(|| py_err("cannot materialize level"))?;
        let img = encode_envelope_rgba8(&peaks, self.pyramid.channels);
        Ok((img.width, img.height, PyBytes::new(py, &img.data)))
    }

    /// Returns a lossless spectral code tile as (first_peak,width,height,bytes).
    pub fn spectral_tile_texture<'py>(
        &self,
        py: Python<'py>,
        layer_index: usize,
        tile_index: u64,
    ) -> PyResult<(usize, usize, usize, Bound<'py, PyBytes>)> {
        let layer = self
            .file
            .spectral_layers
            .get(layer_index)
            .ok_or_else(|| py_err("spectral layer index out of range"))?;
        let ch = self.file.header.channels as usize;
        let n = if ch == 0 { 0 } else { layer.peaks.len() / ch };
        let tile = self.pyramid.tile_peaks.max(1);
        let tile_index =
            usize::try_from(tile_index).map_err(|_| py_err("spectral tile out of range"))?;
        let first = tile_index
            .checked_mul(tile)
            .ok_or_else(|| py_err("spectral tile out of range"))?;
        if first >= n {
            return Err(py_err("spectral tile out of range"));
        }
        let count = tile.min(n - first);
        let img = encode_spectral_rgba8(&layer.peaks[first * ch..(first + count) * ch], ch);
        Ok((first, img.width, img.height, PyBytes::new(py, &img.data)))
    }

    #[pyo3(signature=(width, height, start_frame, end_frame, vertical_full_scale=1.0, background=(0,0,0,0), waveform=(255,255,255,255)))]
    pub fn render_rgba<'py>(
        &self,
        py: Python<'py>,
        width: usize,
        height: usize,
        start_frame: u64,
        end_frame: u64,
        vertical_full_scale: f32,
        background: (u8, u8, u8, u8),
        waveform: (u8, u8, u8, u8),
    ) -> PyResult<Bound<'py, PyBytes>> {
        let img = render_waveform_rgba8_scaled(
            &self.pyramid,
            width,
            height,
            start_frame,
            end_frame,
            vertical_full_scale,
            [background.0, background.1, background.2, background.3],
            [waveform.0, waveform.1, waveform.2, waveform.3],
        );
        Ok(PyBytes::new(py, &img.data))
    }
}

#[pyfunction(name = "source_stamp")]
pub fn py_source_stamp(path: &str) -> PyResult<(u32, u32)> {
    let stamp = SourceStamp::from_path(path).map_err(py_err)?;
    Ok((stamp.mtime_low32, stamp.size_low32))
}

#[pyfunction(name = "source_stamp_from_unix_seconds")]
pub fn py_source_stamp_from_unix_seconds(mtime_seconds: i64, size: u64) -> (u32, u32) {
    let stamp = SourceStamp::from_unix_seconds_and_size(mtime_seconds, size);
    (stamp.mtime_low32, stamp.size_low32)
}

#[pyfunction(name = "default_divisions")]
#[pyo3(signature=(sample_rate, fine_peaks_per_second=300))]
pub fn py_default_divisions(sample_rate: u32, fine_peaks_per_second: u32) -> Vec<u32> {
    default_divisions(sample_rate, fine_peaks_per_second).to_vec()
}

#[pyfunction(name = "generate_pcm16")]
#[pyo3(signature=(pcm16le, sample_rate, channels, divisions, source_mtime_low32=0, source_size_low32=0, spectral=true))]
pub fn py_generate_pcm16<'py>(
    py: Python<'py>,
    pcm16le: &[u8],
    sample_rate: u32,
    channels: usize,
    divisions: Vec<u32>,
    source_mtime_low32: u32,
    source_size_low32: u32,
    spectral: bool,
) -> PyResult<Bound<'py, PyBytes>> {
    if pcm16le.len() % 2 != 0 {
        return Err(py_err("PCM16 byte length must be even"));
    }
    let pcm: Vec<i16> = pcm16le
        .chunks_exact(2)
        .map(|x| i16::from_le_bytes([x[0], x[1]]))
        .collect();
    let opt = GenerateOptions {
        sample_rate,
        channels,
        divisions,
        source_mtime_low32,
        source_size_low32,
        spectral,
    };
    let out = generate_pcm16(&pcm, &opt).map_err(py_err)?;
    Ok(PyBytes::new(py, &out))
}

#[pyfunction(name = "generate_f32")]
#[pyo3(signature=(pcm_f32le, sample_rate, channels, divisions, large_range, source_mtime_low32=0, source_size_low32=0, spectral=true))]
pub fn py_generate_f32<'py>(
    py: Python<'py>,
    pcm_f32le: &[u8],
    sample_rate: u32,
    channels: usize,
    divisions: Vec<u32>,
    large_range: bool,
    source_mtime_low32: u32,
    source_size_low32: u32,
    spectral: bool,
) -> PyResult<Bound<'py, PyBytes>> {
    if pcm_f32le.len() % 4 != 0 {
        return Err(py_err("float32 byte length must be a multiple of four"));
    }
    let pcm: Vec<f32> = pcm_f32le
        .chunks_exact(4)
        .map(|x| f32::from_le_bytes([x[0], x[1], x[2], x[3]]))
        .collect();
    let opt = GenerateOptions {
        sample_rate,
        channels,
        divisions,
        source_mtime_low32,
        source_size_low32,
        spectral,
    };
    let out = generate_f32(&pcm, &opt, large_range).map_err(py_err)?;
    Ok(PyBytes::new(py, &out))
}

#[pymodule]
pub fn reapeaks(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyReaPeaks>()?;
    m.add_function(wrap_pyfunction!(py_source_stamp, m)?)?;
    m.add_function(wrap_pyfunction!(py_source_stamp_from_unix_seconds, m)?)?;
    m.add_function(wrap_pyfunction!(py_default_divisions, m)?)?;
    m.add_function(wrap_pyfunction!(py_generate_pcm16, m)?)?;
    m.add_function(wrap_pyfunction!(py_generate_f32, m)?)?;
    crate::python_reaper_generate::register(m)?;
    crate::python_gpu_cache::register(m)?;
    Ok(())
}
