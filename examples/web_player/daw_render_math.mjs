export function clamp(value, lo, hi) {
  return Math.max(lo, Math.min(hi, value));
}

export function signedI16(lo, hi) {
  const value = (lo | (hi << 8)) >>> 0;
  return value & 0x8000 ? value - 0x10000 : value;
}

export function decodeWave(code, encoding = 'RPKN') {
  if (encoding === 'RPKN') return code / (code < 0 ? 32768 : 32767);
  const negative = code < 0;
  const magnitude = Math.abs(code);
  const amplitude = magnitude <= 24576
    ? magnitude / 24576
    : 2 ** ((magnitude - 24576) / 1024);
  return negative ? -amplitude : amplitude;
}

export function u32le(bytes, offset) {
  return (
    bytes[offset]
    | (bytes[offset + 1] << 8)
    | (bytes[offset + 2] << 16)
    | (bytes[offset + 3] << 24)
  ) >>> 0;
}

export function chooseLevel(levels, desiredDivision) {
  if (!Array.isArray(levels) || !levels.length) return null;
  const desired = Math.max(1, Number(desiredDivision) || 1);
  const normalized = levels.map((level, index) => ({
    ...level,
    layer_index: Number(level.layer_index ?? index),
    division: Math.max(1, Number(level.division) || 1),
  }));
  const eligible = normalized.filter(level => level.division <= desired);
  if (eligible.length) {
    return eligible.reduce((best, level) => level.division > best.division ? level : best);
  }
  return normalized.reduce((best, level) => level.division < best.division ? level : best);
}

function hsvToRgb(h, s, v) {
  const i = Math.floor(h * 6);
  const f = h * 6 - i;
  const p = v * (1 - s);
  const q = v * (1 - f * s);
  const t = v * (1 - (1 - f) * s);
  const table = [
    [v, t, p], [q, v, p], [p, v, t],
    [p, q, v], [t, p, v], [v, p, q],
  ];
  return table[((i % 6) + 6) % 6];
}

export function spectralCodeColor(code, {
  lowHz = 20,
  highHz = 10000,
  rangeMode = 0,
  reverse = false,
  fadeNoise = true,
  nyquist = 24000,
} = {}) {
  const normal = [0.43, 0.92, 0.67];
  const frequency = Number(code & 0x7fff);
  const tonality = Number((code >>> 15) & 0x3fff) / 16383;
  if (!(frequency > 0)) return normal;
  const low = Math.max(10, Number(lowHz) || 20);
  const high = Math.min(Math.max(low + 1, Number(highHz) || 10000), Math.max(low + 1, nyquist));
  const f = clamp(frequency, low, high);
  let phase = rangeMode
    ? ((Math.log(f / low) / Math.log(2)) % 1 + 1) % 1
    : clamp(Math.log(f / low) / Math.max(1e-9, Math.log(high / low)), 0, 1);
  if (reverse) phase = 1 - phase;
  const hue = hsvToRgb(phase * 0.78, 0.92, 1);
  const tonalMix = clamp(tonality, 0, 1) ** 0.65;
  const neutral = fadeNoise ? normal : [0.58, 0.58, 0.58];
  return neutral.map((value, index) => value + (hue[index] - value) * tonalMix);
}

function smoothstep(edge0, edge1, value) {
  if (edge0 === edge1) return value < edge0 ? 0 : 1;
  const t = clamp((value - edge0) / (edge1 - edge0), 0, 1);
  return t * t * (3 - 2 * t);
}

export function loudnessColor(lu, transition = 1.5) {
  const width = Math.max(0.05, Number(transition) || 1.5);
  let color = [0.18, 0.48, 0.32];
  const bands = [
    [-42, [0.18, 0.72, 0.33]],
    [-36, [0.47, 0.82, 0.24]],
    [-30, [0.92, 0.86, 0.18]],
    [-24, [1.00, 0.62, 0.10]],
    [-18, [0.96, 0.24, 0.10]],
    [-12, [0.88, 0.14, 0.34]],
    [-6, [0.34, 0.42, 1.00]],
  ];
  for (const [threshold, next] of bands) {
    const mix = smoothstep(threshold - width, threshold + width, lu);
    color = color.map((value, index) => value + (next[index] - value) * mix);
  }
  return color;
}

export function rgbCss(rgb, alpha = 1) {
  const values = rgb.map(value => Math.round(clamp(value, 0, 1) * 255));
  return `rgba(${values[0]},${values[1]},${values[2]},${clamp(alpha, 0, 1)})`;
}

export function unpackSpectrogram12(bytes, record, channel, bin, channels) {
  const boundedBin = clamp(Math.floor(bin), 0, 127);
  const pair = boundedBin >> 1;
  const base = (record * channels + channel) * 192 + pair * 3;
  const b0 = bytes[base];
  const b1 = bytes[base + 1];
  const b2 = bytes[base + 2];
  return (boundedBin & 1) === 0
    ? ((b0 << 4) | (b1 >> 4))
    : ((b2 << 4) | (b1 & 15));
}

export function spectrogramCodeToDb(code, gainDb = 0) {
  return (Number(code) - 4095.5) * (10 / (88.92179516969081 * Math.log(10))) + Number(gainDb || 0);
}

export function dbToGain(db) {
  return 10 ** (Number(db || 0) / 20);
}

export function energyToLufs(energy) {
  return -0.691 + 10 * Math.log10(Math.max(Number(energy) || 0, 1e-20));
}
