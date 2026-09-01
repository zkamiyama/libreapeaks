import assert from 'node:assert/strict';
import test from 'node:test';

import {
  FRAGMENT_SHADER,
  nearestLevel,
  pageWindow,
  textureShape,
} from './webgl2_renderer.mjs';

test('nearestLevel follows logarithmic division distance', () => {
  const levels = [
    {layer_index: 0, division: 160, record_count: 100, bytes_per_channel_record: 4},
    {layer_index: 1, division: 1600, record_count: 10, bytes_per_channel_record: 4},
    {layer_index: 2, division: 16000, record_count: 1, bytes_per_channel_record: 4},
  ];
  assert.equal(nearestLevel(levels, 200).layer_index, 0);
  assert.equal(nearestLevel(levels, 1000).layer_index, 1);
  assert.equal(nearestLevel(levels, 9000).layer_index, 2);
});

test('pageWindow adds guard records and page-aligns raw requests', () => {
  assert.deepEqual(pageWindow(1600, 3200, 160, 10000, 512), {first: 0, count: 1024});
  const far = pageWindow(160 * 1400, 160 * 1500, 160, 10000, 512);
  assert.equal(far.first, 1024);
  assert.equal(far.count, 1024);
});

test('texture shapes preserve exact reapeaks payload packing', () => {
  assert.deepEqual(textureShape('waveform', 512, 2, 4), {width: 2, height: 512, components: 4, type: 'u8'});
  assert.deepEqual(textureShape('spectral', 512, 6, 4), {width: 6, height: 512, components: 4, type: 'u8'});
  assert.deepEqual(textureShape('spectrogram', 512, 2, 192), {width: 192, height: 1024, components: 1, type: 'u8'});
  assert.deepEqual(textureShape('loudness', 512, 8, 8), {width: 8, height: 512, components: 2, type: 'f32'});
});

test('fragment shader performs packed 12-bit g unpacking on GPU', () => {
  assert.match(FRAGMENT_SHADER, /uint unpackG/);
  assert.match(FRAGMENT_SHADER, /b0 << 4u/);
  assert.match(FRAGMENT_SHADER, /b2 << 4u/);
  assert.match(FRAGMENT_SHADER, /u_specGain/);
  assert.match(FRAGMENT_SHADER, /u_tileDebug/);
});
