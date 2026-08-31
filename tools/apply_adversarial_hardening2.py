from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"{path}: patch context not found")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


# Path-query JSON comes from another process. Treat shape/type mismatches as
# configuration errors rather than leaking AttributeError or stringifying
# attacker-controlled objects.
replace_once(
    "examples/reaper_config.py",
    '''    raw_write = payload.get("write")
    if not isinstance(raw_write, str) or not raw_write:
        raise ReaperConfigError("REAPER returned an empty peak-cache write path")
    raw_read = payload.get("read", "")
    if not isinstance(raw_read, str):
        raise ReaperConfigError("REAPER returned a non-string peak-cache read path")
    return ReaperPeakPaths(
        media=media_path,
        read=_resolved(raw_read) if raw_read else None,
        write=_resolved(raw_write),
        source_type=str(payload.get("source_type", "")),
        origin="GetPeakFileNameEx",
    )
''',
    '''    if not isinstance(payload, dict):
        raise ReaperConfigError("REAPER path-query result must be a JSON object")
    raw_write = payload.get("write")
    if not isinstance(raw_write, str) or not raw_write:
        raise ReaperConfigError("REAPER returned an empty peak-cache write path")
    raw_read = payload.get("read", "")
    if not isinstance(raw_read, str):
        raise ReaperConfigError("REAPER returned a non-string peak-cache read path")
    source_type = payload.get("source_type", "")
    if not isinstance(source_type, str):
        raise ReaperConfigError("REAPER returned a non-string media source type")
    return ReaperPeakPaths(
        media=media_path,
        read=_resolved(raw_read) if raw_read else None,
        write=_resolved(raw_write),
        source_type=source_type,
        origin="GetPeakFileNameEx",
    )
''',
)

# v1 and v2 are intentionally supported. Any explicitly declared future or
# malformed version must fail closed instead of being interpreted as v2.
replace_once(
    "examples/reaper_config.py",
    '''    elif isinstance(payload, dict):
        raw = payload.get("entries", payload)
''',
    '''    elif isinstance(payload, dict):
        if "version" in payload:
            version = payload["version"]
            if type(version) is not int or version not in (1, CACHE_MAP_VERSION):
                raise ReaperConfigError(
                    f"unsupported cache-map version: {version!r}"
                )
        raw = payload.get("entries", payload)
''',
)

# Restore the public C division helper that the docs/application contract
# already promises. Keep invalid zero rates explicit at the ABI boundary even
# though the internal Rust helper is defensive and clamps them.
replace_once(
    "src/ffi.rs",
    '''use crate::texture::{
    encode_envelope_rgba8, encode_spectral_rgba8, encode_wave_tile_rgba8,
    render_waveform_rgba8_scaled,
};
''',
    '''use crate::texture::{
    encode_envelope_rgba8, encode_spectral_rgba8, encode_wave_tile_rgba8,
    render_waveform_rgba8_scaled,
};
use crate::wave::default_divisions;
''',
)
replace_once(
    "src/ffi.rs",
    '''#[no_mangle]
pub unsafe extern "C" fn rpk_generate_pcm16(
''',
    '''#[no_mangle]
pub unsafe extern "C" fn rpk_default_divisions(
    sample_rate: u32,
    fine_peaks_per_second: u32,
    out_divisions: *mut u32,
) -> i32 {
    if sample_rate == 0 || fine_peaks_per_second == 0 || out_divisions.is_null() {
        return -1;
    }
    let divisions = default_divisions(sample_rate, fine_peaks_per_second);
    std::ptr::copy_nonoverlapping(divisions.as_ptr(), out_divisions, divisions.len());
    0
}

#[no_mangle]
pub unsafe extern "C" fn rpk_generate_pcm16(
''',
)

replace_once(
    "include/reapeaks.h",
    '''/* RPKN generator for decoded PCM16. */
''',
    '''/* Calculate REAPER-style three-level divisions for peakcachegenrs. */
int32_t rpk_default_divisions(uint32_t sample_rate,
                              uint32_t fine_peaks_per_second,
                              uint32_t out_divisions[3]);

/* RPKN generator for decoded PCM16. */
''',
)
