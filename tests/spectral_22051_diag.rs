#[cfg(feature = "strict-wdl")]
use std::f64::consts::PI;

#[cfg(feature = "strict-wdl")]
#[test]
fn rate_tone_22051_matches_pointwise_oracle() {
    const EXPECTED: [u32; 61] = [
        509772759, 512426978, 515605474, 519799779, 525272036, 531497956,
        536151013, 536740837, 536740837, 536740837, 536740837, 536773605,
        536773605, 536773605, 536773605, 536773605, 536773605, 536773605,
        536773605, 536773605, 536773605, 536773605, 536773605, 536773605,
        536773605, 536773605, 536773605, 536773605, 536773605, 536773605,
        536773605, 536806373, 536773605, 536806373, 536806373, 536806373,
        536806373, 536806373, 536806373, 536806373, 536773605, 536773605,
        536773605, 536773605, 536773605, 536773605, 536773605, 536773605,
        536806373, 536806373, 536806373, 536806373, 536806373, 536806373,
        536806373, 536806373, 536806373, 536806374, 536806373, 536806374,
        536806374,
    ];

    let sr = 22_051u32;
    let frames = 5_000usize;
    let mut pcm = Vec::with_capacity(frames);
    for i in 0..frames {
        let x = 0.73 * (2.0 * PI * 997.5 * i as f64 / sr as f64).sin();
        let y = x.clamp(-1.0, 1.0) * 32767.0;
        let v = if y >= 0.0 {
            (y + 0.5).floor()
        } else {
            (y - 0.5).ceil()
        };
        pcm.push(v.clamp(-32768.0, 32767.0) as i16);
    }

    let got = reapeaks::spectral::build_fine_spectral(&pcm, frames, 1, sr, 73).unwrap();
    assert_eq!(got.len(), EXPECTED.len());
    for (index, (got_peak, expected)) in got.iter().zip(EXPECTED).enumerate() {
        assert_eq!(
            got_peak.code(),
            expected,
            "spectral 22051 mismatch at point {index}: got freq={} density={}, expected freq={} density={}",
            got_peak.frequency_hz,
            got_peak.density,
            expected & 0x7fff,
            (expected >> 15) & 0x3fff,
        );
    }
}
