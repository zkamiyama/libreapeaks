use reapeaks::quantize_rpkl_f32;
use std::env;
use std::fs;
use std::path::PathBuf;

const MAX_FINITE_MAG_BITS: u32 = 0x7f7f_ffff;
const POS_TRANSITIONS: usize = 32_767;
const NEG_TRANSITIONS: usize = 32_768;

fn required_path(name: &str) -> PathBuf {
    env::var_os(name)
        .map(PathBuf::from)
        .unwrap_or_else(|| panic!("{name} must be set by the RPKL finite-boundary oracle"))
}

fn read_u32le(path: PathBuf) -> Vec<u32> {
    let bytes = fs::read(&path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    assert_eq!(bytes.len() % 4, 0, "{} is not u32-aligned", path.display());
    bytes
        .as_chunks::<4>()
        .0
        .iter()
        .map(|word| u32::from_le_bytes(*word))
        .collect()
}

fn code_magnitude(bits: u32, negative: bool) -> u32 {
    let magnitude = f32::from_bits(bits);
    assert!(magnitude.is_finite());
    assert!(!magnitude.is_sign_negative());
    let value = if negative { -magnitude } else { magnitude };
    let code = quantize_rpkl_f32(value);
    if code == i16::MIN {
        32_768
    } else {
        u32::from(code.unsigned_abs())
    }
}

fn first_model_bits_for_code(target: u32, negative: bool) -> u32 {
    assert!(target > 0);
    assert!(target <= if negative { 32_768 } else { 32_767 });
    assert!(code_magnitude(0, negative) < target);
    assert!(code_magnitude(MAX_FINITE_MAG_BITS, negative) >= target);
    let mut lo = 0u32;
    let mut hi = MAX_FINITE_MAG_BITS;
    while hi - lo > 1 {
        let mid = lo + (hi - lo) / 2;
        if code_magnitude(mid, negative) >= target {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    hi
}

#[test]
#[ignore = "requires pinned REAPER-generated exhaustive decision-boundary evidence"]
fn reaper779_rpkl_quantizer_matches_every_finite_f32_decision_boundary() {
    let positive = read_u32le(required_path("REAPEAKS_RPKL_POS_BOUNDARIES"));
    let negative = read_u32le(required_path("REAPEAKS_RPKL_NEG_BOUNDARIES"));
    assert_eq!(positive.len(), POS_TRANSITIONS);
    assert_eq!(negative.len(), NEG_TRANSITIONS);

    assert_eq!(quantize_rpkl_f32(0.0), 0);
    assert_eq!(quantize_rpkl_f32(-0.0), 0);

    for (lower_code, &oracle_boundary) in positive.iter().enumerate() {
        let target = lower_code as u32 + 1;
        assert!(oracle_boundary > 0);
        assert_eq!(
            code_magnitude(oracle_boundary - 1, false),
            lower_code as u32,
            "positive predecessor differs for transition {lower_code}->{target} at 0x{oracle_boundary:08x}"
        );
        assert_eq!(
            code_magnitude(oracle_boundary, false),
            target,
            "positive boundary differs for transition {lower_code}->{target} at 0x{oracle_boundary:08x}"
        );
        assert_eq!(
            first_model_bits_for_code(target, false),
            oracle_boundary,
            "positive first-bit boundary differs for transition {lower_code}->{target}"
        );
    }

    for (lower_code, &oracle_boundary) in negative.iter().enumerate() {
        let target = lower_code as u32 + 1;
        assert!(oracle_boundary > 0);
        assert_eq!(
            code_magnitude(oracle_boundary - 1, true),
            lower_code as u32,
            "negative predecessor differs for transition {lower_code}->{target} at 0x{oracle_boundary:08x}"
        );
        assert_eq!(
            code_magnitude(oracle_boundary, true),
            target,
            "negative boundary differs for transition {lower_code}->{target} at 0x{oracle_boundary:08x}"
        );
        assert_eq!(
            first_model_bits_for_code(target, true),
            oracle_boundary,
            "negative first-bit boundary differs for transition {lower_code}->{target}"
        );
    }

    println!(
        "RPKL_ALL_FINITE_BOUNDARIES_EXACT positive={} negative={} finite_bit_patterns_covered={}",
        positive.len(),
        negative.len(),
        2u64 * (u64::from(MAX_FINITE_MAG_BITS) + 1)
    );
}
