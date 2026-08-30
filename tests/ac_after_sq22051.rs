#![cfg(feature = "strict-wdl")]
mod sequence_common;

#[test]
fn target_is_exact_after_sq22051_stereo() {
    let pcm = sequence_common::sq22051_pcm();
    let _ = sequence_common::generate_i16(&pcm, 22_051, 2, &[73, 1168, 22192]);
    sequence_common::assert_target_exact();
}
