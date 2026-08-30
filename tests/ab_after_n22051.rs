#![cfg(feature = "strict-wdl")]
mod sequence_common;

#[test]
fn target_is_exact_after_n22051_mono_noise() {
    let target = sequence_common::target_pcm();
    let before = sequence_common::generate_i16(&target, 32_000, 2, &[106, 1696, 32224]);
    let before_hash = sequence_common::fnv64(before);

    let pcm = sequence_common::n22051_pcm();
    let _ = sequence_common::generate_i16(&pcm, 22_051, 1, &[73, 1168, 22192]);

    let after = sequence_common::generate_i16(&target, 32_000, 2, &[106, 1696, 32224]);
    let after_hash = sequence_common::fnv64(after);

    assert_eq!(
        before_hash,
        sequence_common::TARGET_HASH,
        "before={before_hash:#018x} after={after_hash:#018x}",
    );
    assert_eq!(
        after_hash,
        sequence_common::TARGET_HASH,
        "before={before_hash:#018x} after={after_hash:#018x}",
    );
}
