use crate::error::{ReaPeaksError, Result};

pub(crate) const PCM24_MIN: i32 = -8_388_608;
pub(crate) const PCM24_MAX: i32 = 8_388_607;
const PCM24_SCALE: f32 = 1.0 / 8_388_608.0;

/// Internal normalized-f32 view over decoded interleaved PCM.
///
/// Implementations return exactly one source sample on demand. This lets the
/// analysis pipeline preserve the established f32 arithmetic without forcing
/// callers that cache integer PCM to materialize a second whole-file f32
/// buffer.
pub(crate) trait F32SampleSource {
    fn sample_len(&self) -> usize;
    fn sample_f32(&self, index: usize) -> f32;
}

impl F32SampleSource for [f32] {
    #[inline]
    fn sample_len(&self) -> usize {
        self.len()
    }

    #[inline]
    fn sample_f32(&self, index: usize) -> f32 {
        self[index]
    }
}

#[inline]
pub(crate) fn pcm24_i32_to_f32(sample: i32) -> f32 {
    // Every signed 24-bit integer is exactly representable as f32, and scaling
    // by 2^-23 is exact. This therefore produces the same normalized f32 value
    // as materializing a conventional decoded PCM24 -> f32 buffer first.
    sample as f32 * PCM24_SCALE
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct Pcm24LeSource<'a> {
    bytes: &'a [u8],
}

impl<'a> Pcm24LeSource<'a> {
    pub(crate) fn new(bytes: &'a [u8]) -> Result<Self> {
        if bytes.len() % 3 != 0 {
            return Err(ReaPeaksError::InvalidArgument(
                "PCM24LE byte length must be a multiple of three",
            ));
        }
        Ok(Self { bytes })
    }

    #[inline]
    fn sample_i32(&self, index: usize) -> i32 {
        let offset = index * 3;
        let raw = u32::from(self.bytes[offset])
            | (u32::from(self.bytes[offset + 1]) << 8)
            | (u32::from(self.bytes[offset + 2]) << 16);
        ((raw << 8) as i32) >> 8
    }
}

impl F32SampleSource for Pcm24LeSource<'_> {
    #[inline]
    fn sample_len(&self) -> usize {
        self.bytes.len() / 3
    }

    #[inline]
    fn sample_f32(&self, index: usize) -> f32 {
        pcm24_i32_to_f32(self.sample_i32(index))
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct Pcm24I32Source<'a> {
    samples: &'a [i32],
}

impl<'a> Pcm24I32Source<'a> {
    pub(crate) fn new(samples: &'a [i32]) -> Result<Self> {
        if samples
            .iter()
            .any(|&sample| !(PCM24_MIN..=PCM24_MAX).contains(&sample))
        {
            return Err(ReaPeaksError::InvalidArgument(
                "PCM24 i32 sample is outside signed 24-bit range",
            ));
        }
        Ok(Self { samples })
    }
}

impl F32SampleSource for Pcm24I32Source<'_> {
    #[inline]
    fn sample_len(&self) -> usize {
        self.samples.len()
    }

    #[inline]
    fn sample_f32(&self, index: usize) -> f32 {
        pcm24_i32_to_f32(self.samples[index])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn packed_pcm24_sign_extension_and_scaling_are_exact() {
        let bytes = [
            0x00, 0x00, 0x80, // -8388608
            0xff, 0xff, 0xff, // -1
            0x00, 0x00, 0x00, // 0
            0x01, 0x00, 0x00, // 1
            0xff, 0xff, 0x7f, // +8388607
        ];
        let source = Pcm24LeSource::new(&bytes).unwrap();
        let expected = [
            -1.0f32,
            -1.0 / 8_388_608.0,
            0.0,
            1.0 / 8_388_608.0,
            8_388_607.0 / 8_388_608.0,
        ];
        for (index, expected) in expected.into_iter().enumerate() {
            assert_eq!(source.sample_f32(index).to_bits(), expected.to_bits());
        }
    }

    #[test]
    fn i32_source_rejects_values_outside_pcm24() {
        assert!(Pcm24I32Source::new(&[PCM24_MIN - 1]).is_err());
        assert!(Pcm24I32Source::new(&[PCM24_MAX + 1]).is_err());
    }
}
