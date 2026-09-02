use crate::rpkx::{
    append_rpkx_chunk, read_rpkx, remove_rpkx_chunks, set_rpkx_chunk, strip_rpkx, RpkxChunk,
    RpkxKey,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};

fn py_err(error: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(error.to_string())
}

fn key(namespace: &[u8], kind: &[u8]) -> PyResult<RpkxKey> {
    let namespace: [u8; 16] = namespace
        .try_into()
        .map_err(|_| py_err("RPKX namespace must be exactly 16 bytes"))?;
    let kind: [u8; 4] = kind
        .try_into()
        .map_err(|_| py_err("RPKX kind must be exactly 4 bytes"))?;
    Ok(RpkxKey::new(namespace, kind))
}

#[pyclass(name = "RpkxChunk")]
#[derive(Clone)]
pub struct PyRpkxChunk {
    namespace: [u8; 16],
    kind: [u8; 4],
    #[pyo3(get)]
    version: u32,
    #[pyo3(get)]
    flags: u32,
    payload: Vec<u8>,
}

#[pymethods]
impl PyRpkxChunk {
    #[getter]
    pub fn namespace<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.namespace)
    }

    #[getter]
    pub fn kind<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.kind)
    }

    #[getter]
    pub fn payload<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.payload)
    }

    fn __repr__(&self) -> String {
        let kind = String::from_utf8_lossy(&self.kind);
        format!(
            "RpkxChunk(kind={kind:?}, version={}, flags={}, payload_len={})",
            self.version,
            self.flags,
            self.payload.len()
        )
    }
}

#[pyfunction(name = "rpkx_chunks")]
pub fn py_rpkx_chunks(reapeaks: &[u8]) -> PyResult<Vec<PyRpkxChunk>> {
    let Some(container) = read_rpkx(reapeaks).map_err(py_err)? else {
        return Ok(Vec::new());
    };
    Ok(container
        .chunks
        .into_iter()
        .map(|chunk| PyRpkxChunk {
            namespace: chunk.key.namespace,
            kind: chunk.key.kind,
            version: chunk.version,
            flags: chunk.flags,
            payload: chunk.payload,
        })
        .collect())
}

#[pyfunction(name = "rpkx_container_info")]
pub fn py_rpkx_container_info(reapeaks: &[u8]) -> PyResult<Option<(u32, u32, u32, usize)>> {
    Ok(read_rpkx(reapeaks).map_err(py_err)?.map(|container| {
        (
            container.flags,
            container.source_stamp.mtime_low32,
            container.source_stamp.size_low32,
            container.chunks.len(),
        )
    }))
}

#[pyfunction(name = "rpkx_set_chunk")]
#[pyo3(signature=(reapeaks, namespace, kind, version, payload, flags=0))]
pub fn py_rpkx_set_chunk<'py>(
    py: Python<'py>,
    reapeaks: &[u8],
    namespace: &[u8],
    kind: &[u8],
    version: u32,
    payload: &[u8],
    flags: u32,
) -> PyResult<Bound<'py, PyBytes>> {
    let key = key(namespace, kind)?;
    let chunk = RpkxChunk::new(key.namespace, key.kind, version, flags, payload.to_vec());
    let out = set_rpkx_chunk(reapeaks, chunk).map_err(py_err)?;
    Ok(PyBytes::new(py, &out))
}

#[pyfunction(name = "rpkx_append_chunk")]
#[pyo3(signature=(reapeaks, namespace, kind, version, payload, flags=0))]
pub fn py_rpkx_append_chunk<'py>(
    py: Python<'py>,
    reapeaks: &[u8],
    namespace: &[u8],
    kind: &[u8],
    version: u32,
    payload: &[u8],
    flags: u32,
) -> PyResult<Bound<'py, PyBytes>> {
    let key = key(namespace, kind)?;
    let chunk = RpkxChunk::new(key.namespace, key.kind, version, flags, payload.to_vec());
    let out = append_rpkx_chunk(reapeaks, chunk).map_err(py_err)?;
    Ok(PyBytes::new(py, &out))
}

#[pyfunction(name = "rpkx_remove_chunks")]
pub fn py_rpkx_remove_chunks<'py>(
    py: Python<'py>,
    reapeaks: &[u8],
    namespace: &[u8],
    kind: &[u8],
) -> PyResult<Bound<'py, PyBytes>> {
    let out = remove_rpkx_chunks(reapeaks, key(namespace, kind)?).map_err(py_err)?;
    Ok(PyBytes::new(py, &out))
}

#[pyfunction(name = "rpkx_strip")]
pub fn py_rpkx_strip<'py>(py: Python<'py>, reapeaks: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    let out = strip_rpkx(reapeaks).map_err(py_err)?;
    Ok(PyBytes::new(py, &out))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyRpkxChunk>()?;
    m.add_function(wrap_pyfunction!(py_rpkx_chunks, m)?)?;
    m.add_function(wrap_pyfunction!(py_rpkx_container_info, m)?)?;
    m.add_function(wrap_pyfunction!(py_rpkx_set_chunk, m)?)?;
    m.add_function(wrap_pyfunction!(py_rpkx_append_chunk, m)?)?;
    m.add_function(wrap_pyfunction!(py_rpkx_remove_chunks, m)?)?;
    m.add_function(wrap_pyfunction!(py_rpkx_strip, m)?)?;
    Ok(())
}
