from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text()
    if old not in text:
        raise SystemExit(f"{path}: expected patch context not found")
    file_path.write_text(text.replace(old, new, 1))


# Real REAPER -'r' count is not required to equal the mirrored waveform count.
replace_once(
    "src/format.rs",
    '''                    let expected_value_count =
                        mirrored
                            .peak_count
                            .checked_mul(2)
                            .ok_or(ReaPeaksError::InvalidHeader(
                                "loudness value count overflow",
                            ))?;
                    if h.peak_count != expected_value_count {
                        return Err(ReaPeaksError::InvalidHeader(
                            "loudness count does not match waveform layer",
                        ));
                    }

''',
    '''                    // REAPER's raw -'r' cadence is independent from the
                    // mirrored waveform bucket count at EOF and for some peak
                    // rates. The count therefore cannot be validated by
                    // equality with the waveform header. Evenness, checked
                    // payload sizing, and truncation checks below are the
                    // structural invariants observed in real files.

''',
)

# REAPER streams nested waveform mipmaps: an incomplete upper bucket is only
# flushed if EOF also leaves the finest bucket incomplete.
p = Path("src/wave.rs")
s = p.read_text()
anchor = "pub fn build_wave_layers(\n"
helper = '''fn reaper_wave_bucket_count(
    frames: usize,
    division: usize,
    fine_division: usize,
    is_finest: bool,
) -> usize {
    if is_finest || division % fine_division != 0 || frames % fine_division != 0 {
        frames.div_ceil(division)
    } else {
        frames / division
    }
}

'''
if helper not in s:
    if anchor not in s:
        raise SystemExit("src/wave.rs: build_wave_layers anchor not found")
    s = s.replace(anchor, helper + anchor, 1)
old_loop = '''    let mut layers = Vec::with_capacity(divisions.len());
    for &div in divisions {
'''
new_loop = '''    let mut layers = Vec::with_capacity(divisions.len());
    let fine_division = divisions.first().copied().unwrap_or(1) as usize;
    for (division_index, &div) in divisions.iter().enumerate() {
'''
if s.count(old_loop) != 2:
    raise SystemExit(f"src/wave.rs: expected two layer loops, got {s.count(old_loop)}")
s = s.replace(old_loop, new_loop, 2)
old_count = '''        let d = div as usize;
        let count = frames.div_ceil(d);
'''
new_count = '''        let d = div as usize;
        let count = reaper_wave_bucket_count(frames, d, fine_division, division_index == 0);
'''
if s.count(old_count) != 2:
    raise SystemExit(f"src/wave.rs: expected two count sites, got {s.count(old_count)}")
s = s.replace(old_count, new_count, 2)
p.write_text(s)

# Coarse -'r' layers contain full groups only; no final partial group is stored.
replace_once(
    "src/loudness.rs",
    '''        let division_usize = usize::try_from(division)
            .map_err(|_| ReaPeaksError::InvalidArgument("division is too large"))?;
        let record_count = frames.div_ceil(division_usize);
''',
    '''        let record_count = base_record_count / group;
''',
)
replace_once(
    "src/loudness.rs",
    '''            let end = start.saturating_add(group).min(base_record_count);
            if start >= end {
                return Err(ReaPeaksError::InvalidArgument(
                    "empty loudness aggregation group",
                ));
            }
            let divisor = (end - start) as f64;
''',
    '''            let end = start
                .checked_add(group)
                .ok_or(ReaPeaksError::InvalidArgument(
                    "loudness group end overflow",
                ))?;
            debug_assert!(end <= base_record_count);
            let divisor = group as f64;
''',
)
