#![cfg(feature = "strict-wdl")]
mod sequence_common;

#[test]
fn target_is_exact_after_n22051_mono_noise() {
    let pcm = sequence_common::n22051_pcm();
    let _ = sequence_common::generate_i16(&pcm, 22_051, 1, &[73, 1168, 22192]);

    let target = sequence_common::target_pcm();
    let first = sequence_common::generate_i16(&target, 32_000, 2, &[106, 1696, 32224]);
    let first_hash = sequence_common::fnv64(first);
    let second = sequence_common::generate_i16(&target, 32_000, 2, &[106, 1696, 32224]);
    let second_hash = sequence_common::fnv64(second);

    assert_eq!(
        first_hash,
        sequence_common::TARGET_HASH,
        "first={first_hash:#018x} second={second_hash:#018x}",
    );
}
