import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizedWheelSteps,
  verticalScaleFromWheel,
  zoomWindow,
} from './interaction_math.mjs';

test('conventional and precision wheel deltas remain directional and continuous', () => {
  assert.equal(normalizedWheelSteps(-120), 1);
  assert.equal(normalizedWheelSteps(120), -1);
  assert.equal(normalizedWheelSteps(-12), 0.1);
  assert.equal(normalizedWheelSteps(-3, 1), 1);
  assert.equal(normalizedWheelSteps(-1, 2, 600), 4);
  assert.equal(normalizedWheelSteps(1, 2, 600), -4);
});

test('horizontal zoom preserves the frame under the pointer', () => {
  const start = 100;
  const end = 1100;
  for (const anchor of [0, 0.13, 0.25, 0.5, 0.91, 1]) {
    const target = start + (end - start) * anchor;
    for (const factor of [0.2, 0.72, 1, 1 / 0.72, 3]) {
      const [nextStart, nextEnd] = zoomWindow(start, end, factor, anchor);
      const nextTarget = nextStart + (nextEnd - nextStart) * anchor;
      assert.ok(Math.abs(nextTarget - target) < 1e-9);
    }
  }
});

test('ctrl-wheel uses full-scale semantics and clamps extremes', () => {
  const taller = verticalScaleFromWheel(1, 1);
  const shorter = verticalScaleFromWheel(1, -1);
  assert.ok(taller < 1, 'wheel up should make waveform taller');
  assert.ok(shorter > 1, 'wheel down should make waveform shorter');
  assert.equal(verticalScaleFromWheel(0.1, 100), 0.1);
  assert.equal(verticalScaleFromWheel(32, -100), 32);
});
