#[cfg(feature = "strict-wdl")]
use std::f64::consts::PI;

#[cfg(feature = "strict-wdl")]
fn fnv_i16(pcm: &[i16]) -> u64 {
    let mut h = 0xcbf2_9ce4_8422_2325u64;
    for &sample in pcm {
        for byte in sample.to_le_bytes() {
            h ^= byte as u64;
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    h
}

#[cfg(feature = "strict-wdl")]
fn tone_pcm() -> Vec<i16> {
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
    pcm
}

#[cfg(feature = "strict-wdl")]
fn noise_pcm() -> Vec<i16> {
    let frames = 5_000usize;
    let seed = 826_347_269u32;
    let mut state = seed ^ 0x9E37_79B9;
    let mut pcm = Vec::with_capacity(frames);
    for _ in 0..frames {
        state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
        let x = (((state >> 16) & 0xffff) as i32) - 32768;
        pcm.push((x / 8) as i16);
    }
    pcm
}

#[cfg(feature = "strict-wdl")]
#[test]
fn reconstructed_22051_pcm_matches_reaper_input_wavs() {
    // FNV-1a over the little-endian PCM data chunks of the exact WAV files
    // used to generate the fresh-process REAPER 7.79 oracle.
    assert_eq!(fnv_i16(&tone_pcm()), 0x6929_98ab_c735_b04a);
    assert_eq!(fnv_i16(&noise_pcm()), 0x6b28_12c2_c1a0_6ab4);
}

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
    let got = reapeaks::spectral::build_fine_spectral(&tone_pcm(), 5000, 1, 22_051, 73)
        .unwrap();
    assert_eq!(got.len(), EXPECTED.len());
    for (index, (got_peak, expected)) in got.iter().zip(EXPECTED).enumerate() {
        assert_eq!(
            got_peak.code(),
            expected,
            "tone mismatch at point {index}: got freq={} density={}, expected freq={} density={}",
            got_peak.frequency_hz,
            got_peak.density,
            expected & 0x7fff,
            (expected >> 15) & 0x3fff,
        );
    }
}

#[cfg(feature = "strict-wdl")]
#[test]
fn rate_noise_22051_matches_pointwise_oracle() {
    const EXPECTED: [u32; 61] = [
        355438949, 355144019, 356192594, 271682438, 273877893, 365991732,
        371987313, 371331952, 341118517, 345869859, 350555682, 353209890,
        354520610, 355143202, 357371426, 360123938, 364580385, 202540241,
        218760400, 143425983, 160858551, 174031287, 412749623, 411569528,
        420941581, 424054540, 426348299, 427888394, 387746139, 388729179,
        388532582, 240091823, 233407152, 228917937, 229933746, 232424115,
        413895903, 414518494, 414747870, 218890825, 352520171, 350685160,
        351340518, 354256866, 416713864, 384895296, 425659804, 426216863,
        427101602, 427920818, 428346804, 428707253, 428379574, 420154418,
        418942001, 355698488, 356222778, 358418235, 359696188, 362284879,
        364447568,
    ];
    let got = reapeaks::spectral::build_fine_spectral(&noise_pcm(), 5000, 1, 22_051, 73)
        .unwrap();
    assert_eq!(got.len(), EXPECTED.len());
    for (index, (got_peak, expected)) in got.iter().zip(EXPECTED).enumerate() {
        assert_eq!(
            got_peak.code(),
            expected,
            "noise mismatch at point {index}: got freq={} density={}, expected freq={} density={}",
            got_peak.frequency_hz,
            got_peak.density,
            expected & 0x7fff,
            (expected >> 15) & 0x3fff,
        );
    }
}
