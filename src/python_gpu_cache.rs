use crate::format::Version;
use crate::{GpuCacheView, GpuLayerKind};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};

fn py_err(error: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(error.to_string())
}

fn parse_kind(kind: &str) -> PyResult<GpuLayerKind> {
    match kind {
        "waveform" => Ok(GpuLayerKind::Waveform),
        "spectral" => Ok(GpuLayerKind::Spectral),
        "spectrogram" => Ok(GpuLayerKind::Spectrogram),
        "loudness" => Ok(GpuLayerKind::Loudness),
        _ => Err(py_err(
            "kind must be waveform, spectral, spectrogram, or loudness",
        )),
    }
}

/// Index-only `.reapeaks` view for direct GPU texture upload.
///
/// Returned tiles preserve the exact on-disk layout. In particular, `-'g'`
/// remains packed as 192 bytes per channel/time record instead of being
/// expanded to 128 u16 bins on the CPU.
#[pyclass(name = "GpuCacheView")]
pub struct PyGpuCacheView {
    view: GpuCacheView,
}

#[pymethods]
impl PyGpuCacheView {
    #[staticmethod]
    pub fn open(path: &str) -> PyResult<Self> {
        Ok(Self {
            view: GpuCacheView::open(path).map_err(py_err)?,
        })
    }

    #[getter]
    pub fn sample_rate(&self) -> u32 {
        self.view.sample_rate
    }

    #[getter]
    pub fn channels(&self) -> u8 {
        self.view.channels
    }

    #[getter]
    pub fn wave_encoding(&self) -> &'static str {
        match self.view.version {
            Version::Rpkn => "RPKN",
            Version::Rpkl => "RPKL",
            Version::Rpkm => "RPKM",
        }
    }

    #[getter]
    pub fn raw_bytes(&self) -> usize {
        self.view.raw_len()
    }

    /// Return `(mirrored_division, record_count, bytes_per_channel_record)`.
    pub fn levels(&self, kind: &str) -> PyResult<Vec<(u32, usize, usize)>> {
        let kind = parse_kind(kind)?;
        Ok(self
            .view
            .layers(kind)
            .iter()
            .map(|layer| {
                (
                    layer.mirrored_division,
                    layer.record_count,
                    layer.bytes_per_channel_record,
                )
            })
            .collect())
    }

    /// Return a contiguous raw record range as
    /// `(first_record, record_count, channels, bytes_per_channel_record, bytes)`.
    #[pyo3(signature=(kind, layer_index, first_record, record_count))]
    pub fn records<'py>(
        &self,
        py: Python<'py>,
        kind: &str,
        layer_index: usize,
        first_record: usize,
        record_count: usize,
    ) -> PyResult<(usize, usize, usize, usize, Bound<'py, PyBytes>)> {
        let tile = self
            .view
            .tile(parse_kind(kind)?, layer_index, first_record, record_count)
            .map_err(py_err)?;
        Ok((
            tile.first_record,
            tile.record_count,
            tile.channels,
            tile.bytes_per_channel_record,
            PyBytes::new(py, tile.bytes),
        ))
    }

    /// Fixed-size convenience wrapper over `records()`.
    #[pyo3(signature=(kind, layer_index, tile_index, tile_records=256))]
    pub fn tile<'py>(
        &self,
        py: Python<'py>,
        kind: &str,
        layer_index: usize,
        tile_index: usize,
        tile_records: usize,
    ) -> PyResult<(usize, usize, usize, usize, Bound<'py, PyBytes>)> {
        if tile_records == 0 {
            return Err(py_err("tile_records must be positive"));
        }
        let first = tile_index
            .checked_mul(tile_records)
            .ok_or_else(|| py_err("GPU tile index overflow"))?;
        self.records(py, kind, layer_index, first, tile_records)
    }
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyGpuCacheView>()?;
    Ok(())
}
