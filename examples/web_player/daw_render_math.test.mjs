import assert from 'node:assert/strict';
import {
  chooseLevel,
  decodeWave,
  energyToLufs,
  signedI16,
  spectralCodeColor,
  spectrogramCodeToDb,
  unpackSpectrogram12,
} from './daw_render_math.mjs';

assert.equal(signedI16(0xff, 0x7f), 32767);
assert.equal(signedI16(0x00, 0x80), -32768);
assert.ok(Math.abs(decodeWave(32767, 'RPKN') - 1) < 1e-9);
assert.equal(chooseLevel([{division: 4}, {division: 16}], 12).division, 4);
assert.equal(chooseLevel([{division: 4}, {division: 16}], 20).division, 16);
const packed = new Uint8Array([0x12, 0x34, 0x56]);
assert.equal(unpackSpectrogram12(packed, 0, 0, 0, 1), 0x123);
assert.equal(unpackSpectrogram12(packed, 0, 0, 1, 1), 0x564);
assert.ok(Number.isFinite(spectrogramCodeToDb(2048, 0)));
assert.ok(Number.isFinite(energyToLufs(1)));
const color = spectralCodeColor((1200 | (12000 << 15)) >>> 0);
assert.equal(color.length, 3);
console.log('daw_render_math tests passed');
