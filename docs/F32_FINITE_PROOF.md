# Finite float32 / RPKL compatibility proof

Date: 2026-09-02  
Primary oracle: **REAPER 7.79 x86_64 Linux**  
Scope: IEEE-754 binary32 finite source samples, RPKL waveform quantization, and finite-input REAPER-native mode-3 whole-file validation

This document separates two different kinds of evidence that must not be conflated:

1. an **exhaustive scalar proof** for the RPKL waveform quantizer over every finite f32 bit pattern; and
2. **whole-file live-oracle evidence** for the stateful waveform / `-'s'` / `-'g'` / `-'r'` pipeline.

The first is exhaustive over the finite f32 scalar domain. The second is deliberately adversarial and byte-exact, but it is not a formal proof over every arbitrary-length finite f32 sequence.

## Exact RPKL waveform quantizer

For `x != 0`, define `a = abs(x)` and

```text
if a <= 1:
    q = a * 24576
else:
    q = 24576 + 1024 * log2(a)
```

REAPER 7.79 quantizes the signed transformed value as if by `floor(y + 0.5)`. Written in magnitude form:

```text
x > 0:
    code = floor(q + 0.5), clamped to +32767

x < 0:
    code = -ceil(q - 0.5), magnitude clamped to 32768
```

`+0.0` and `-0.0` both map to zero.

This rule is sign-asymmetric only at an exactly representable half tie. For example:

```text
x =  0.63934326171875  -> q = 15712.5 ->  15713
x = -0.63934326171875  -> q = 15712.5 -> -15712
```

So an implementation that rounds `abs(x)` half-up and applies the sign afterward is not REAPER-compatible at those ties.

## Exhaustive decision-boundary oracle

The live oracle treats REAPER itself as the classifier. Candidate finite f32 values are ordered by their IEEE-754 bit patterns and probed in fresh REAPER processes. Parallel binary search recovers the first f32 value entering every output-code interval.

The recovered transition set is complete:

```text
positive transitions: 32,767
negative transitions: 32,768
total transitions:    65,535
```

For every transition, the oracle rechecks both the boundary value and its immediate finite predecessor. The Rust model must emit the old code immediately before the boundary and the new code at the boundary.

The positive and negative transition magnitudes coincide except at representable exact-half ties. The exhaustive oracle found:

```text
representable exact-half ties: 8,192
```

The decision boundaries partition the complete finite binary32 domain, including signed zero and all subnormals. The number of finite f32 bit patterns covered is:

```text
4,278,190,080
```

Therefore `quantize_rpkl_f32()` is exhaustively matched to the pinned REAPER 7.79 oracle for **every finite f32 bit pattern**.

Permanent evidence:

- `tools/reaper_oracle/rpkl_finite_boundary_oracle.py`
- `tests/reaper_rpkl_finite_boundaries.rs`
- `.github/workflows/reaper-rpkl-finite-boundaries.yml`

## Whole-file finite edge oracle

A separate live oracle writes raw IEEE float32 WAVE media and compares the complete generated RPKL cache, not only waveform records. The comparison covers headers, layer table, positive waveform layers, `-'s'`, `-'g'`, `-'r'`, and final file bytes.

The edge corpus contains 15 targeted cases covering, among other values:

- `+0.0` and `-0.0`;
- minimum and maximum subnormal values;
- minimum normal values;
- `f32::MAX` and its negative;
- values immediately around ±1 and the RPKL saturation region;
- representative and exhaustive exact-half-tie sequences;
- raw finite bit-pattern sequences designed to cross exponent and mantissa boundaries.

Result:

```text
15 / 15 complete RPKL files byte-identical
```

Permanent evidence:

- `tools/reaper_oracle/f32_finite_edge_cases.py`
- `.github/workflows/reaper-f32-finite-edge-whole-file.yml`

## Large finite values and `-'s'`

The edge oracle exposed a finite-input corner that is easy to miss. With values near `f32::MAX`, REAPER's f32 Hann-window multiply can overflow before promotion to f64. At a zero window coefficient this can produce an invalid intermediate such as:

```text
Inf * 0 -> NaN
```

REAPER's `-'s'` path proceeds only when the total spectral magnitude is ordered-greater than zero. A NaN total therefore emits a zero spectral peak. libreapeaks mirrors that ordered comparison; using only `total <= 0` is not equivalent because every comparison with NaN is false.

This behavior matters even though the **source media are finite**: non-finite intermediates can still be produced by finite extreme inputs.

## Broad finite whole-file matrix

The repository also has a fresh-process 128-case finite float32 whole-file workflow:

- `.github/workflows/reaper-f32-finite-whole-file.yml`

It reuses the broad adversarial float32 corpus used for spectrogram validation and compares the complete RPKL output. The corpus spans sample rates, channel counts, `peakcachegenrs` values, scheduler boundaries, finite values above ±1, tiny finite values, tones, chirps, impulses, sparse lanes, noise, long inputs, and deterministic randomized cases.

This gate is complementary to the 15-case edge corpus: the edge suite concentrates on IEEE-754/RPKL boundaries, while the 128-case matrix stresses stateful scheduling and multichannel DSP across a broad operating surface.

## What is proved, and what is not

### Exhaustively proved for the pinned oracle

- the scalar RPKL waveform code for every one of the **4,278,190,080 finite f32 bit patterns**;
- all **65,535** output decision boundaries;
- all **8,192** representable sign-asymmetric exact-half ties;
- finite subnormal handling in the scalar RPKL waveform quantizer.

### Byte-exact live-oracle evidence

- complete RPKL files for the 15-case finite edge corpus;
- the broad 128-case finite whole-file matrix when its dedicated workflow gate is green;
- existing dedicated float32 `-'g'`, `-'s'`, loudness, scheduler, and strict-WDL matrices described in `COMPATIBILITY.md`.

### Still outside the compatibility claim

- source NaN, `+Inf`, and `-Inf` behavior as a claim of exact REAPER policy;
- REAPER versions, platforms, or architectures outside the named oracle matrices;
- a mathematical/formal proof over every possible arbitrary-length finite f32 sequence through all stateful `-'s'`, `-'g'`, and `-'r'` transforms.

The last point does **not** weaken the exhaustive scalar quantizer result. It only distinguishes a finite scalar state space from an unbounded sequence/state-space proof.

## Compatibility wording

The intended compatibility statement is:

> Against REAPER 7.79 x86_64 Linux, libreapeaks' RPKL waveform quantizer is exhaustive for all finite IEEE float32 bit patterns. Finite subnormals and all representable exact-half ties are included. Complete mode-3 RPKL output is additionally locked by dedicated finite-edge and broad finite whole-file live-oracle gates. Exact source NaN/Inf policy remains outside the claim.
