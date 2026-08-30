#[cfg(feature = "strict-wdl")]
use std::f64::consts::PI;

#[cfg(feature = "strict-wdl")]
const TONE_EXPECTED: [u32; 61] = [
    509772759,512426978,515605474,519799779,525272036,531497956,536151013,536740837,536740837,536740837,536740837,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536806373,536773605,536806373,536806373,536806373,536806373,536806373,536806373,536806373,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536773605,536806373,536806373,536806373,536806373,536806373,536806373,536806373,536806373,536806373,536806374,536806373,536806374,536806374,
];

#[cfg(feature = "strict-wdl")]
const NOISE_EXPECTED: [u32; 61] = [
    355438949,355144019,356192594,271682438,273877893,365991732,371987313,371331952,341118517,345869859,350555682,353209890,354520610,355143202,357371426,360123938,364580385,202540241,218760400,143425983,160858551,174031287,412749623,411569528,420941581,424054540,426348299,427888394,387746139,388729179,388532582,240091823,233407152,228917937,229933746,232424115,413895903,414518494,414747870,218890825,352520171,350685160,351340518,354256866,416713864,384895296,425659804,426216863,427101602,427920818,428346804,428707253,428379574,420154418,418942001,355698488,356222778,358418235,359696188,362284879,364447568,
];

#[cfg(feature = "strict-wdl")]
fn tone_pcm() -> Vec<i16> {
    let mut pcm = Vec::with_capacity(5000);
    for i in 0..5000 {
        let x = 0.73 * (2.0 * PI * 997.5 * i as f64 / 22051.0).sin();
        let y = x.clamp(-1.0, 1.0) * 32767.0;
        let v = if y >= 0.0 { (y + 0.5).floor() } else { (y - 0.5).ceil() };
        pcm.push(v.clamp(-32768.0, 32767.0) as i16);
    }
    pcm
}

#[cfg(feature = "strict-wdl")]
fn noise_pcm() -> Vec<i16> {
    let mut state = 826_347_269u32 ^ 0x9E37_79B9;
    let mut pcm = Vec::with_capacity(5000);
    for _ in 0..5000 {
        state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
        let x = (((state >> 16) & 0xffff) as i32) - 32768;
        pcm.push((x / 8) as i16);
    }
    pcm
}

#[cfg(feature = "strict-wdl")]
fn metrics(got: &[reapeaks::SpectralPeak], expected: &[u32]) -> (usize, usize, usize, u64, i64) {
    let mut exact = 0usize;
    let mut freq_exact = 0usize;
    let mut first = usize::MAX;
    let mut density_abs = 0u64;
    let mut density_signed = 0i64;
    for (i, (&e, g)) in expected.iter().zip(got.iter()).enumerate() {
        let gc = g.code();
        if gc == e { exact += 1; } else if first == usize::MAX { first = i; }
        let ef = (e & 0x7fff) as i64;
        let ed = ((e >> 15) & 0x3fff) as i64;
        if g.frequency_hz as i64 == ef { freq_exact += 1; }
        let dd = g.density as i64 - ed;
        density_abs += dd.unsigned_abs();
        density_signed += dd;
    }
    (exact, freq_exact, first, density_abs, density_signed)
}

#[cfg(feature = "strict-wdl")]
#[test]
fn sweep_near_unity_feed_block_sizes() {
    let tone = tone_pcm();
    let noise = noise_pcm();
    for block in [32,64,96,128,192,256,384,512,768,1024,1536,2048,3072,4096,5000,6144,8192,16384] {
        unsafe { std::env::set_var("RPK_WDL_BLOCK_FRAMES", block.to_string()); }
        let tg = reapeaks::spectral::build_fine_spectral(&tone, 5000, 1, 22051, 73).unwrap();
        let ng = reapeaks::spectral::build_fine_spectral(&noise, 5000, 1, 22051, 73).unwrap();
        let tm = metrics(&tg, &TONE_EXPECTED);
        let nm = metrics(&ng, &NOISE_EXPECTED);
        eprintln!("BLOCK {block:5} tone len={} exact={} freq={} first={} dabs={} dsum={} | noise len={} exact={} freq={} first={} dabs={} dsum={}", tg.len(), tm.0, tm.1, tm.2, tm.3, tm.4, ng.len(), nm.0, nm.1, nm.2, nm.3, nm.4);
    }
    unsafe { std::env::remove_var("RPK_WDL_BLOCK_FRAMES"); }
}
