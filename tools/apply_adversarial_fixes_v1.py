from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text()
    if old not in text:
        raise SystemExit(f"{path}: expected patch context not found")
    file_path.write_text(text.replace(old, new, 1))


def replace_between(path: str, start: str, end: str, replacement: str) -> None:
    file_path = Path(path)
    text = file_path.read_text()
    start_index = text.find(start)
    if start_index < 0:
        raise SystemExit(f"{path}: start marker not found: {start!r}")
    end_index = text.find(end, start_index + len(start))
    if end_index < 0:
        raise SystemExit(f"{path}: end marker not found: {end!r}")
    file_path.write_text(text[:start_index] + replacement + text[end_index:])


# Real REAPER -'r' count is not required to equal the mirrored waveform count.
# Loudness always uses ceil(frames / base_division), while nested waveform
# mipmaps have a different EOF flush rule. Coarse loudness also stores only
# complete aggregation groups, so equality is not a structural invariant.
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

# REAPER streams nested waveform mipmaps. The finest layer always flushes a
# partial bucket. Upper layers only flush their partial bucket when EOF also
# leaves the finest bucket incomplete; at an exact fine-bucket boundary they
# contain complete upper buckets only.
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

# Reproduce libebur128's exact convolution expression tree. At 88.2/96 kHz,
# reversing the two outer terms in a[2] changes one f64 ulp and survives into
# REAPER's raw f32 loudness tail.
replace_once(
    "src/loudness.rs",
    '''        a: [
            1.0,
            first.a1 + second.a1,
            first.a2 + first.a1 * second.a1 + second.a2,
            first.a1 * second.a2 + first.a2 * second.a1,
            first.a2 * second.a2,
        ],
''',
    '''        a: [
            1.0,
            second.a1 + first.a1,
            second.a2 + first.a1 * second.a1 + first.a2,
            first.a1 * second.a2 + first.a2 * second.a1,
            first.a2 * second.a2,
        ],
''',
)

# REAPER uses floor(sample_rate / 40) samples per raw 25 ms energy block. The
# 400 ms and 3 s normalization divisors are independently based on libebur128's
# rounded samples_in_100ms. These are deliberately not forced to agree at rates
# such as 44.1 kHz and 22.05 kHz.
replace_once(
    "src/loudness.rs",
    '''fn loudness_block_frames(sample_rate: u32) -> Result<usize> {
    let frames = (u64::from(sample_rate) + 20) / 40;
    usize::try_from(frames.max(1))
        .map_err(|_| ReaPeaksError::InvalidArgument("loudness block is too large"))
}
''',
    '''fn loudness_block_frames(sample_rate: u32) -> Result<usize> {
    let frames = u64::from(sample_rate) / 40;
    usize::try_from(frames.max(1))
        .map_err(|_| ReaPeaksError::InvalidArgument("loudness block is too large"))
}
''',
)
replace_once(
    "src/loudness.rs",
    '''    fn normalized(&self, block_frames: usize) -> f64 {
        self.sum / (self.values.len() * block_frames) as f64
    }
''',
    '''    fn normalized(&self, normalization_frames: usize) -> f64 {
        self.sum / normalization_frames as f64
    }
''',
)

# The sample-granular fallback was only an approximation. Real REAPER keeps
# the fixed block-energy rings for every cadence, snapshots the completed
# blocks at each output bucket, and does not flush an incomplete 25 ms block at
# EOF. This single path is byte-exact for the full adversarial oracle matrix.
replace_between(
    "src/loudness.rs",
    "#[derive(Debug, Clone)]\nstruct SlidingEnergy {",
    "#[derive(Debug, Clone)]\nstruct BlockEnergyRing {",
    "",
)
replace_once(
    "src/loudness.rs",
    '''    let block_frames = loudness_block_frames(sample_rate)?;
    let block_aligned = base_division % block_frames == 0 && frames % block_frames == 0;
    let fallback_windows = (!block_aligned)
        .then(|| loudness_windows(sample_rate))
        .transpose()?;
''',
    '''    let block_frames = loudness_block_frames(sample_rate)?;
    let (momentary_window, short_term_window) = loudness_windows(sample_rate)?;
''',
)
replace_between(
    "src/loudness.rs",
    "        if block_aligned {\n",
    "\n        if record_index != base_record_count {",
    '''        let mut momentary = BlockEnergyRing::new(MOMENTARY_BLOCKS_25MS)?;
        let mut short_term = BlockEnergyRing::new(SHORT_TERM_BLOCKS_25MS)?;
        let mut block_energy = 0.0f64;
        let mut block_fill = 0usize;

        for frame in 0..frames {
            let filtered = filter.process(sample(frame * channels + channel));
            block_energy += filtered * filtered;
            block_fill += 1;

            let completed_frame = frame + 1;
            if block_fill == block_frames {
                momentary.push(block_energy);
                short_term.push(block_energy);
                block_energy = 0.0;
                block_fill = 0;
            }

            if completed_frame % base_division == 0 || completed_frame == frames {
                base_records[record_index * channels + channel] = LoudnessPeak {
                    momentary_energy: momentary.normalized(momentary_window) as f32,
                    short_term_energy: short_term.normalized(short_term_window) as f32,
                };
                record_index += 1;
            }
        }
''',
)

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

# Rust 1.98 promotes chunks_exact(constant) to a deny-by-warnings clippy lint.
replace_once(
    "tests/reaper_adversarial_oracle.rs",
    '''    bytes.chunks_exact(2)
        .map(|sample| i16::from_le_bytes([sample[0], sample[1]]))
        .collect()
''',
    '''    bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|sample| i16::from_le_bytes(*sample))
        .collect()
''',
)
