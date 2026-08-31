#![allow(clippy::too_many_lines)]
use reapeaks::spectral::build_fine_spectral;
use std::f64::consts::PI;

#[derive(Clone, Copy, Debug)]
enum Kind {
    Tone {
        freq: f64,
        amp: f64,
        phase: f64,
    },
    AltFull,
    AltLsb,
    DcPos,
    DcNeg,
    Square8,
    Saw31,
    Impulse {
        pos: usize,
        amp: i16,
    },
    Noise {
        shift: u32,
        seed: u32,
    },
    ToneNoise {
        freq: f64,
        noise_div: i32,
        seed: u32,
    },
}

#[derive(Clone, Copy, Debug)]
struct Case {
    sr: u32,
    frames: usize,
    div: u32,
    kind: Kind,
}

fn q16(x: f64) -> i16 {
    let y = x.clamp(-1.0, 1.0) * 32767.0;
    let v = if y >= 0.0 {
        (y + 0.5).floor()
    } else {
        (y - 0.5).ceil()
    };
    v.clamp(-32768.0, 32767.0) as i16
}

fn pcm(c: Case) -> Vec<i16> {
    match c.kind {
        Kind::Tone { freq, amp, phase } => (0..c.frames)
            .map(|i| q16(amp * (2.0 * PI * freq * i as f64 / c.sr as f64 + phase).sin()))
            .collect(),
        Kind::AltFull => (0..c.frames)
            .map(|i| if i & 1 == 1 { 32767 } else { -32768 })
            .collect(),
        Kind::AltLsb => (0..c.frames)
            .map(|i| if i & 1 == 1 { 1 } else { -1 })
            .collect(),
        Kind::DcPos => vec![16384; c.frames],
        Kind::DcNeg => vec![-16384; c.frames],
        Kind::Square8 => (0..c.frames)
            .map(|i| if (i / 8) & 1 == 1 { 24000 } else { -24000 })
            .collect(),
        Kind::Saw31 => (0..c.frames)
            .map(|i| {
                ((((i % 31) as f64 / 30.0 * 2.0 - 1.0) * 24000.0) as i32).clamp(-32768, 32767)
                    as i16
            })
            .collect(),
        Kind::Impulse { pos, amp } => {
            let mut v = vec![0; c.frames];
            v[pos] = amp;
            v
        }
        Kind::Noise { shift, mut seed } => {
            let mut v = Vec::with_capacity(c.frames);
            for _ in 0..c.frames {
                seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
                let x = (((seed >> 16) & 0xffff) as i32) - 32768;
                let x = if shift == 0 { x } else { x / (1i32 << shift) };
                v.push(x.clamp(-32768, 32767) as i16);
            }
            v
        }
        Kind::ToneNoise {
            freq,
            noise_div,
            mut seed,
        } => {
            let mut v = Vec::with_capacity(c.frames);
            for i in 0..c.frames {
                seed = seed.wrapping_mul(1103515245).wrapping_add(12345);
                let nv = ((((seed >> 16) & 0xffff) as i32) - 32768).div_euclid(noise_div);
                let tv = q16(0.55 * (2.0 * PI * freq * i as f64 / c.sr as f64 + 0.21).sin()) as i32;
                v.push((tv + nv).clamp(-32768, 32767) as i16);
            }
            v
        }
    }
}

fn cases() -> Vec<Case> {
    let mut v = Vec::with_capacity(188);
    let freqs = [
        43.49, 43.50, 43.51, 99.49, 99.50, 99.51, 439.49, 439.50, 439.51, 997.0, 999.49, 999.50,
        999.51, 1234.5, 5000.5, 10000.5,
    ];
    for sr in [22050u32, 44100, 48000, 96000] {
        for freq in freqs {
            if freq < sr as f64 / 2.0 - 50.0 {
                v.push(Case {
                    sr,
                    frames: ((sr as usize) * 22 / 100).max(4096),
                    div: sr / 300,
                    kind: Kind::Tone {
                        freq,
                        amp: 0.8,
                        phase: 0.0,
                    },
                });
            }
        }
    }
    for (sr, freq) in [
        (44100u32, 1000.0),
        (48000, 440.0),
        (96000, 997.0),
        (22050, 1234.5),
    ] {
        for phase in [0.123, 1.7] {
            v.push(Case {
                sr,
                frames: ((sr as usize) * 22 / 100).max(4096),
                div: sr / 300,
                kind: Kind::Tone {
                    freq,
                    amp: 0.8,
                    phase,
                },
            });
        }
        for amp in [1.0 / 32767.0, 0.01, 0.1] {
            v.push(Case {
                sr,
                frames: ((sr as usize) * 22 / 100).max(4096),
                div: sr / 300,
                kind: Kind::Tone {
                    freq,
                    amp,
                    phase: 0.37,
                },
            });
        }
    }
    for sr in [22050u32, 44100, 48000, 96000] {
        let frames = ((sr as usize) * 18 / 100).max(4096);
        for kind in [
            Kind::AltFull,
            Kind::AltLsb,
            Kind::DcPos,
            Kind::DcNeg,
            Kind::Square8,
            Kind::Saw31,
        ] {
            v.push(Case {
                sr,
                frames,
                div: sr / 300,
                kind,
            });
        }
    }
    for sr in [22050u32, 44100, 48000, 96000] {
        let frames = ((sr as usize) * 16 / 100).max(5000);
        for pos in [
            0usize, 1, 72, 73, 74, 146, 147, 148, 511, 512, 1023, 1024, 1025, 2047,
        ] {
            if pos < frames {
                v.push(Case {
                    sr,
                    frames,
                    div: sr / 300,
                    kind: Kind::Impulse { pos, amp: 30000 },
                });
            }
        }
    }
    for sr in [22050u32, 44100, 48000, 96000] {
        let frames = ((sr as usize) * 20 / 100).max(5000);
        for shift in [0u32, 6, 12] {
            v.push(Case {
                sr,
                frames,
                div: sr / 300,
                kind: Kind::Noise {
                    shift,
                    seed: 0x12345678 ^ sr ^ shift,
                },
            });
        }
    }
    for (sr, freq) in [
        (44100u32, 1000.0),
        (48000, 440.0),
        (96000, 997.0),
        (22050, 1234.5),
    ] {
        let frames = ((sr as usize) * 24 / 100).max(5000);
        for noise_div in [8i32, 32, 128] {
            v.push(Case {
                sr,
                frames,
                div: sr / 300,
                kind: Kind::ToneNoise {
                    freq,
                    noise_div,
                    seed: 0xC001D00D ^ sr ^ noise_div as u32,
                },
            });
        }
    }
    assert_eq!(v.len(), 188);
    v
}

fn expected_count(frames: usize, sr: u32, div: u32) -> usize {
    if frames == 0 || div == 0 {
        return 0;
    }
    if sr <= 22050 {
        return frames.saturating_sub(512) / div as usize;
    }
    let num = (frames as u128) * 22050u128;
    let margin = 512u128 * sr as u128;
    if num <= margin {
        0
    } else {
        ((num - margin) / (div as u128 * 22050u128)) as usize
    }
}

fn fnv64(codes: impl IntoIterator<Item = u32>) -> u64 {
    let mut h = 0xcbf29ce484222325u64;
    for code in codes {
        for b in code.to_le_bytes() {
            h ^= b as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
    }
    h
}

const COUNTS: [usize; 188] = [
    59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59,
    59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59,
    59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59,
    59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 59, 49, 49, 49, 49, 49, 49, 47, 47, 47, 47, 47, 47,
    47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 61, 61, 61, 61, 61, 61, 61, 61, 61, 61, 61, 61,
    61, 61, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41,
    41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41, 61, 61, 61, 53,
    53, 53, 53, 53, 53, 53, 53, 53, 65, 65, 65, 65, 65, 65, 65, 65, 65, 65, 65, 65,
];

const HASHES: [u64; 188] = [
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x6f2f21dee1aab6e5,
    0x4197f9bc544a4df5,
    0x3f6cc2fb403cca95,
    0x01f1b5f4026d2c2c,
    0x65861c2a8abf42e0,
    0xbb15ff73140bddd4,
    0xcbf742bc8250c225,
    0x8ffa425d72ff6a0e,
    0x7b2779335c0f49c2,
    0x057e19a0268f86b0,
    0x0cd1840919913492,
    0x06f74631347a899a,
    0xe68b45c7ff87606d,
    0x8c816f19093c413b,
    0x98cfb88d29da5cb3,
    0x5fbaa60dd5aeee7f,
    0xb90b3e14d5beca27,
    0xe96b4fdab397e3a7,
    0x9d3f864738729a97,
    0xbf3916aa438ad24a,
    0x86a701c468f7a30e,
    0x6eac41a4de65c562,
    0x833cf35da0cfb28b,
    0xd1125300ac47c1ef,
    0xec41fd8a1589f11d,
    0x77c662bc2e5267e5,
    0x9277cd4f8acc7564,
    0x5ea654c3c51732b4,
    0x0554e55cc4e5ed4b,
    0x057bf229d5b0a2f5,
    0x781c79c03d2835f6,
    0x5d76a9d1ea53e46c,
    0x8c88d709130e3777,
    0xf90d311d638c5227,
    0x35c7f682238e3cfe,
    0xa0d2e1b1d1638435,
    0x58cafe3bee4ad110,
    0xf0dc38a99bbfa65d,
    0x510149b8cce4a9e7,
    0xd10221faa01f932b,
    0x52685d46a0f8b880,
    0x8fd67e33cbabf391,
    0x8930f101167e5970,
    0x50ad574998d7dfbe,
    0x60f889986107d13f,
    0x128115b1e345392d,
    0x4fed865840b08916,
    0x26b04063878ae234,
    0x951cb2704fe53733,
    0x310da72f5976e663,
    0x4a23102eebabc907,
    0xcd42354b7d5d6cb4,
    0x5a6f554892e59239,
    0x2ef8eec075ebae80,
    0x661c7e510de30a0d,
    0x16ee29fa792e8309,
    0xbe2db50d2f6bf32b,
    0x7e71769f5058fc56,
    0x983b9612a1141351,
    0xc73225f9b3487ea9,
    0x4ba8796bf8f37702,
    0xd1d54822af7cb05b,
    0x27a14030a9508c96,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0x9bfb50efdee56e15,
    0xa589da5d8aa432f5,
    0xa589da5d8aa432f5,
    0xa589da5d8aa432f5,
    0xa589da5d8aa432f5,
    0xa589da5d8aa432f5,
    0xa589da5d8aa432f5,
    0x85aeed426a83f8ac,
    0x64e6ff6bc2e615e8,
    0xdc7978b069386129,
    0xd8dbb1607fa6d4da,
    0x7fedc605b193858e,
    0x7e36a4ec83be5beb,
    0x12cfc779cfc9b188,
    0x4a0d8ddeb84faff4,
    0x506062b16b33e9a1,
    0xdfd763f96caab57e,
    0x48b410a79deb1fac,
    0x6c894d9c82418da5,
    0x9721abae4f266e05,
    0x57f3470de0e89071,
    0x6641c3f2f5091b52,
    0xefa7ef30243c0021,
    0xdddb84b8c8c01cbc,
    0x4e6b08f8c029a0a5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0x1c640f4b57f86260,
    0x8df8573d9eb69a7f,
    0x099f4ec13b3258b9,
    0x4c97a692506224b1,
    0x1ae3f0a75cea7ef9,
    0x018ceac84a531406,
    0xe767cb2338f95aff,
    0x5748415e918e3ec7,
    0x2bb1cd9aff130ae5,
    0x084be1d0f798c1e0,
    0x0f3ac84ccbf121c9,
    0x37c5b786506782ca,
    0x9351aaabf8862626,
    0x5f22d33482beb2d0,
    0xa0f462c604a0669b,
    0xb96a57cbba4da825,
    0x014cfce38ebf97aa,
    0x1e4a48e6d285e064,
    0x6493e22fd8f56f1e,
    0x3745765cd9afe2e4,
    0x9f058183feec3f0f,
    0x521879e2c4771fec,
    0xd30aab1c3e88d324,
    0x938d59ddab881843,
    0x401c6d0712cbaaaa,
    0xc52d5d6d7457cc04,
    0x249e948a8e18ff38,
    0xba628dd1c9eb42a3,
    0xf7cd1bea82840f80,
    0x84d89a58f6702998,
    0x24935a5a1620adfe,
    0x5bc4836049883fc1,
    0xa2de8c5d0e70fcb7,
    0x60aa10799929398d,
    0x849125537daea450,
    0xd3665d4c523d91e0,
    0x537c40355bd6ed0e,
    0x0e1d293dba3d22f3,
    0x60c34c084e6abd04,
    0x02b5089c6d93effb,
    0xc7f32b224c90492e,
    0x705e954600dfd552,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd4960595a50042b5,
    0xd1e1c95682e39978,
    0xaad113b29767b3b9,
    0xaadfd9d92c0db1ea,
    0x7b8cb1576c930252,
    0x7ed29a07242fe4eb,
    0x5a8e65aa80594ed0,
    0xe40edb3d03f10663,
    0xe13eb819e5b28e33,
    0x2b6a51ca9c2b940f,
    0xd3050c7776c6ab72,
    0x8c478bb7629b0555,
    0x3d54da6302bd047a,
    0xf7e0784b1078c9a3,
    0xbf7d083d91922e24,
    0xa03182aafd815c22,
    0xaa2a1b8fe62ca76c,
    0xd454d8499a7166d3,
    0x0b678d813e8d032c,
    0xde65f6d7d32ae7f5,
    0xde65f6d7d32ae7f5,
    0xde65f6d7d32ae7f5,
];

#[test]
#[cfg(feature = "strict-wdl")]
fn reaper779_fresh_process_spectral_corpus_diagnostic() {
    let cs = cases();
    let mut oracle_points = 0usize;
    let mut count_mismatch = 0usize;
    let mut hash_mismatch = 0usize;
    let mut exact_cases = 0usize;
    let mut exact_points = 0usize;
    for (i, c) in cs.into_iter().enumerate() {
        let oracle_count = COUNTS[i];
        let model_count = expected_count(c.frames, c.sr, c.div);
        let got = build_fine_spectral(&pcm(c), c.frames, 1, c.sr, c.div).unwrap();
        let h = fnv64(got.iter().map(|p| p.code()));
        let count_ok = got.len() == oracle_count;
        let hash_ok = count_ok && h == HASHES[i];
        if !count_ok {
            count_mismatch += 1;
            eprintln!(
                "COUNT case={i:03} sr={} div={} frames={} kind={:?} formula={} got={} oracle={}",
                c.sr,
                c.div,
                c.frames,
                c.kind,
                model_count,
                got.len(),
                oracle_count
            );
        } else if !hash_ok {
            hash_mismatch += 1;
            eprintln!(
                "HASH  case={i:03} sr={} div={} frames={} kind={:?} got={h:016x} oracle={:016x}",
                c.sr, c.div, c.frames, c.kind, HASHES[i]
            );
        } else {
            exact_cases += 1;
            exact_points += oracle_count;
        }
        oracle_points += oracle_count;
    }
    eprintln!("SPECTRAL_CORPUS summary cases=188 oracle_points={oracle_points} exact_cases={exact_cases} exact_points={exact_points} count_mismatch={count_mismatch} hash_mismatch={hash_mismatch}");
}
