use crate::{
    generate_f32_reaper, generate_pcm16_reaper, generate_pcm24_reaper, GenerateOptions,
    ReaperPeakMode,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};

fn py_err(error: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(error.to_string())
}

fn parse_mode(mode: &str) -> PyResult<ReaperPeakMode> {
    match mode {
        "waveform" => Ok(ReaperPeakMode::Waveform),
        "spectral" => Ok(ReaperPeakMode::Spectral),
        "spectrogram" => Ok(ReaperPeakMode::Spectrogram),
        _ => Err(py_err(
            "mode must be 'waveform', 'spectral', or 'spectrogram'",
        )),
    }
}

#[pyfunction(name = "generate_pcm16_reaper")]
#[pyo3(signature=(pcm16le, sample_rate, channels, divisions, mode, source_mtime_low32=0, source_size_low32=0))]
fn py_generate_pcm16_reaper<'py>(
    py: Python<'py>,
    pcm16le: &[u8],
    sample_rate: u32,
    channels: usize,
    divisions: Vec<u32>,
    mode: &str,
    source_mtime_low32: u32,
    source_size_low32: u32,
) -> PyResult<Bound<'py, PyBytes>> {
    if pcm16le.len() % 2 != 0 {
        return Err(py_err("PCM16 byte length must be even"));
    }
    let pcm: Vec<i16> = pcm16le
        .chunks_exact(2)
        .map(|sample| i16::from_le_bytes([sample[0], sample[1]]))
        .collect();
    let options = GenerateOptions {
        sample_rate,
        channels,
        divisions,
        source_mtime_low32,
        source_size_low32,
        spectral: false,
    };
    let mode = parse_mode(mode)?;
    // Cache generation can be CPU-heavy. Detach from the Python interpreter so
    // a Qt/worker-thread frontend can keep pumping UI events and progress.
    let bytes = py
        .detach(move || generate_pcm16_reaper(&pcm, &options, mode))
        .map_err(py_err)?;
    Ok(PyBytes::new(py, &bytes))
}

#[pyfunction(name = "generate_pcm24_reaper")]
#[pyo3(signature=(pcm24le, sample_rate, channels, divisions, mode, source_mtime_low32=0, source_size_low32=0))]
fn py_generate_pcm24_reaper<'py>(
    py: Python<'py>,
    pcm24le: &[u8],
    sample_rate: u32,
    channels: usize,
    divisions: Vec<u32>,
    mode: &str,
    source_mtime_low32: u32,
    source_size_low32: u32,
) -> PyResult<Bound<'py, PyBytes>> {
    if pcm24le.len() % 3 != 0 {
        return Err(py_err(
            "PCM24LE byte length must be a multiple of three",
        ));
    }
    // Detaching requires owned input. Keep the packed 3-byte representation;
    // unlike the old workaround, this does not materialize a whole-file f32
    // buffer before entering the Rust generator.
    let pcm24le = pcm24le.to_vec();
    let options = GenerateOptions {
        sample_rate,
        channels,
        divisions,
        source_mtime_low32,
        source_size_low32,
        spectral: false,
    };
    let mode = parse_mode(mode)?;
    let bytes = py
        .detach(move || generate_pcm24_reaper(&pcm24le, &options, mode))
        .map_err(py_err)?;
    Ok(PyBytes::new(py, &bytes))
}

#[pyfunction(name = "generate_f32_reaper")]
#[pyo3(signature=(pcm_f32le, sample_rate, channels, divisions, large_range, mode, source_mtime_low32=0, source_size_low32=0))]
fn py_generate_f32_reaper<'py>(
    py: Python<'py>,
    pcm_f32le: &[u8],
    sample_rate: u32,
    channels: usize,
    divisions: Vec<u32>,
    large_range: bool,
    mode: &str,
    source_mtime_low32: u32,
    source_size_low32: u32,
) -> PyResult<Bound<'py, PyBytes>> {
    if pcm_f32le.len() % 4 != 0 {
        return Err(py_err("float32 byte length must be a multiple of four"));
    }
    let pcm: Vec<f32> = pcm_f32le
        .chunks_exact(4)
        .map(|sample| f32::from_le_bytes([sample[0], sample[1], sample[2], sample[3]]))
        .collect();
    let options = GenerateOptions {
        sample_rate,
        channels,
        divisions,
        source_mtime_low32,
        source_size_low32,
        spectral: false,
    };
    let mode = parse_mode(mode)?;
    let bytes = py
        .detach(move || generate_f32_reaper(&pcm, &options, large_range, mode))
        .map_err(py_err)?;
    Ok(PyBytes::new(py, &bytes))
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("REAPER_PEAK_MODE_WAVEFORM", "waveform")?;
    m.add("REAPER_PEAK_MODE_SPECTRAL", "spectral")?;
    m.add("REAPER_PEAK_MODE_SPECTROGRAM", "spectrogram")?;
    m.add_function(wrap_pyfunction!(py_generate_pcm16_reaper, m)?)?;
    m.add_function(wrap_pyfunction!(py_generate_pcm24_reaper, m)?)?;
    m.add_function(wrap_pyfunction!(py_generate_f32_reaper, m)?)?;
    crate::python_spectrogram_view::register(m)?;
    Ok(())
}
