use std::f64::consts::PI;

#[cfg(feature = "strict-wdl")]
#[test]
fn print_rate_tone_22051_codes() {
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
    let codes: Vec<u32> = got.iter().map(|p| p.code()).collect();
    eprintln!("SPECTRAL_22051_CODES {codes:?}");
    assert_eq!(codes.len(), 61);
}
