#![cfg(feature = "strict-wdl")]
mod sequence_common;

#[test]
fn target_is_exact_after_tri32000_mono() {
    let pcm = sequence_common::tri32000_pcm();
    let _ = sequence_common::generate_i16(&pcm, 32_000, 1, &[106, 1696, 32224]);
    sequence_common::assert_target_exact();
}
