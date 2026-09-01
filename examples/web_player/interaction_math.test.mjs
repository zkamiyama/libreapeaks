import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('./app.js', import.meta.url), 'utf8');
const sandbox = {
  WheelEvent: {
    DOM_DELTA_PIXEL: 0,
    DOM_DELTA_LINE: 1,
    DOM_DELTA_PAGE: 2,
  },
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox, {filename: 'examples/web_player/app.js'});
const {
  wheelSteps,
  verticalScaleFromWheel,
  zoomWindow,
} = sandbox.__libreapeaksInteractionMath;

test('conventional and precision wheel deltas remain directional and continuous', () => {
  const element = {clientHeight: 600};
  assert.equal(wheelSteps({deltaY: -120, deltaMode: 0}, element), 1);
  assert.equal(wheelSteps({deltaY: 120, deltaMode: 0}, element), -1);
  assert.equal(wheelSteps({deltaY: -12, deltaMode: 0}, element), 0.1);
  assert.equal(wheelSteps({deltaY: -3, deltaMode: 1}, element), 1);
  assert.equal(wheelSteps({deltaY: -1, deltaMode: 2}, element), 4);
  assert.equal(wheelSteps({deltaY: 1, deltaMode: 2}, element), -4);
});

test('horizontal zoom preserves the exact frame under the pointer', () => {
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

test('ctrl-wheel follows full-scale semantics and clamps hostile deltas', () => {
  const taller = verticalScaleFromWheel(1, 1);
  const shorter = verticalScaleFromWheel(1, -1);
  assert.ok(taller < 1, 'wheel up should make waveform taller');
  assert.ok(shorter > 1, 'wheel down should make waveform shorter');
  assert.equal(verticalScaleFromWheel(0.1, 100), 0.1);
  assert.equal(verticalScaleFromWheel(32, -100), 32);
});
