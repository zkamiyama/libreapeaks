#[cfg(target_endian = "little")]
use std::mem::align_of;

#[inline]
fn aligned_pcm16_le(bytes: &[u8]) -> Option<&[i16]> {
    #[cfg(target_endian = "little")]
    {
        if bytes.as_ptr().align_offset(align_of::<i16>()) == 0 {
            // SAFETY: i16 accepts every 16-bit pattern, the byte count is
            // checked by the callers, and the pointer alignment is verified
            // immediately above. The returned slice cannot outlive `bytes`.
            return Some(unsafe {
                std::slice::from_raw_parts(bytes.as_ptr().cast::<i16>(), bytes.len() / 2)
            });
        }
    }
    None
}

#[inline]
fn aligned_f32_le(bytes: &[u8]) -> Option<&[f32]> {
    #[cfg(target_endian = "little")]
    {
        if bytes.as_ptr().align_offset(align_of::<f32>()) == 0 {
            // SAFETY: Rust f32 permits every IEEE-754 bit pattern, the byte
            // count is checked by the callers, and alignment is verified.
            return Some(unsafe {
                std::slice::from_raw_parts(bytes.as_ptr().cast::<f32>(), bytes.len() / 4)
            });
        }
    }
    None
}

pub(crate) fn with_pcm16_le<R>(bytes: &[u8], f: impl FnOnce(&[i16]) -> R) -> R {
    if let Some(samples) = aligned_pcm16_le(bytes) {
        return f(samples);
    }
    let decoded: Vec<i16> = bytes
        .chunks_exact(2)
        .map(|sample| i16::from_le_bytes([sample[0], sample[1]]))
        .collect();
    f(&decoded)
}

pub(crate) fn with_f32_le<R>(bytes: &[u8], f: impl FnOnce(&[f32]) -> R) -> R {
    if let Some(samples) = aligned_f32_le(bytes) {
        return f(samples);
    }
    let decoded: Vec<f32> = bytes
        .chunks_exact(4)
        .map(|sample| f32::from_le_bytes([sample[0], sample[1], sample[2], sample[3]]))
        .collect();
    f(&decoded)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pcm16_view_or_fallback_decodes_exact_bits() {
        let bytes = [0x00, 0x80, 0xff, 0xff, 0x34, 0x12, 0xff, 0x7f];
        let values = with_pcm16_le(&bytes, |samples| samples.to_vec());
        assert_eq!(values, vec![i16::MIN, -1, 0x1234, i16::MAX]);
    }

    #[test]
    fn f32_view_or_fallback_decodes_exact_bits() {
        let words = [0x0000_0000u32, 0x8000_0000, 0x3f80_0000, 0x7fc0_1234];
        let bytes: Vec<u8> = words.iter().flat_map(|word| word.to_le_bytes()).collect();
        let values = with_f32_le(&bytes, |samples| {
            samples.iter().map(|v| v.to_bits()).collect::<Vec<_>>()
        });
        assert_eq!(values, words);
    }
}
