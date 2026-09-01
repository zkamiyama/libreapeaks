use crate::format::ReaPeaks;
use crate::spectrogram::SPECTROGRAM_BINS;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};

fn py_err(error: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(error.to_string())
}

/// Direct read-only access to decoded `-'g'` spectrogram bins.
///
/// The tile method returns row-major little-endian u16 data suitable for a
/// CPU heatmap or direct upload to a normalized 16-bit GPU texture. Rows are
/// `(channel * 128 + bin)`, columns are time frames.
#[pyclass(name = "SpectrogramView")]
pub struct PySpectrogramView {
    file: ReaPeaks,
}

#[pymethods]
impl PySpectrogramView {
    #[staticmethod]
    pub fn open(path: &str) -> PyResult<Self> {
        Ok(Self {
            file: ReaPeaks::open(path).map_err(py_err)?,
        })
    }

    #[getter]
    pub fn sample_rate(&self) -> u32 {
        self.file.header.sample_rate
    }

    #[getter]
    pub fn channels(&self) -> u8 {
        self.file.header.channels
    }

    /// Return `(mirrored_division, time_frame_count)` for every `-'g'` level.
    pub fn levels(&self) -> Vec<(u32, usize)> {
        let channels = usize::from(self.file.header.channels);
        self.file
            .spectrogram_layers
            .iter()
            .map(|layer| (layer.mirrored_division, layer.frame_count(channels)))
            .collect()
    }

    /// Return one raw u16 spectrogram tile as
    /// `(first_frame, width, height, u16le_bytes)`.
    ///
    /// `height == channels * 128`. Within each channel, row 0 is stored bin 0
    /// and row 127 is stored bin 127. The output is deliberately not colorized:
    /// gain, palette and other display transforms belong in the application or
    /// shader layer.
    #[pyo3(signature=(layer_index, tile_index, tile_frames=256))]
    pub fn tile_u16le<'py>(
        &self,
        py: Python<'py>,
        layer_index: usize,
        tile_index: usize,
        tile_frames: usize,
    ) -> PyResult<(usize, usize, usize, Bound<'py, PyBytes>)> {
        if tile_frames == 0 {
            return Err(py_err("tile_frames must be positive"));
        }
        let channels = usize::from(self.file.header.channels);
        let layer = self
            .file
            .spectrogram_layers
            .get(layer_index)
            .ok_or_else(|| py_err("spectrogram layer index out of range"))?;
        let frame_count = layer.frame_count(channels);
        let first = tile_index
            .checked_mul(tile_frames)
            .ok_or_else(|| py_err("spectrogram tile index overflow"))?;
        if first >= frame_count {
            return Err(py_err("spectrogram tile out of range"));
        }
        let width = tile_frames.min(frame_count - first);
        let height = channels
            .checked_mul(SPECTROGRAM_BINS)
            .ok_or_else(|| py_err("spectrogram tile height overflow"))?;
        let capacity = width
            .checked_mul(height)
            .and_then(|value| value.checked_mul(2))
            .ok_or_else(|| py_err("spectrogram tile size overflow"))?;
        let mut out = Vec::with_capacity(capacity);
        for channel in 0..channels {
            for bin in 0..SPECTROGRAM_BINS {
                for time in first..first + width {
                    let frame = &layer.frames[time * channels + channel];
                    out.extend_from_slice(&frame.bins[bin].to_le_bytes());
                }
            }
        }
        Ok((first, width, height, PyBytes::new(py, &out)))
    }
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PySpectrogramView>()?;
    Ok(())
}
