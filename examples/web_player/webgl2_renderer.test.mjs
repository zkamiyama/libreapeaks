import assert from 'node:assert/strict';
import test from 'node:test';

import {
  FRAGMENT_SHADER,
  WebGL2PackedRenderer,
  nearestLevel,
  nextPowerOfTwo,
  pageRecordsForLayer,
  pageWindow,
  planPcmDraw,
  planPcmLod,
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

test('record pages are byte-budgeted for wide-channel packed spectrograms', () => {
  const wave = {bytes_per_channel_record: 4};
  const g = {bytes_per_channel_record: 192};
  assert.equal(pageRecordsForLayer(wave, 8), 512);
  assert.equal(pageRecordsForLayer(g, 2), 512);
  assert.equal(pageRecordsForLayer(g, 8), 128);
});

test('pageWindow adds guard records with one-page minimum prefetch', () => {
  assert.deepEqual(pageWindow(1600, 3200, 160, 10000, 512), {first: 0, count: 512});
  const far = pageWindow(160 * 1400, 160 * 1500, 160, 10000, 512);
  assert.equal(far.first, 1024);
  assert.equal(far.count, 512);
});

test('texture shapes preserve exact reapeaks payload packing', () => {
  assert.deepEqual(textureShape('waveform', 512, 2, 4), {width: 2, height: 512, components: 4, type: 'u8'});
  assert.deepEqual(textureShape('spectral', 512, 6, 4), {width: 6, height: 512, components: 4, type: 'u8'});
  assert.deepEqual(textureShape('spectrogram', 512, 2, 192), {width: 384, height: 512, components: 1, type: 'u8'});
  assert.deepEqual(textureShape('spectrogram', 512, 8, 192), {width: 1536, height: 512, components: 1, type: 'u8'});
  assert.deepEqual(textureShape('loudness', 512, 8, 8), {width: 8, height: 512, components: 2, type: 'f32'});
});

test('fragment shader performs packed 12-bit g unpacking on GPU', () => {
  assert.match(FRAGMENT_SHADER, /uint unpackG/);
  assert.match(FRAGMENT_SHADER, /channel \* 192/);
  assert.match(FRAGMENT_SHADER, /b0 << 4u/);
  assert.match(FRAGMENT_SHADER, /b2 << 4u/);
  assert.match(FRAGMENT_SHADER, /u_specGain/);
  assert.match(FRAGMENT_SHADER, /u_tileDebug/);
});

test('PCM LOD enters with hysteresis then reaches exact sample mode', () => {
  assert.equal(nextPowerOfTwo(100), 128);
  const entered = planPcmLod({
    viewStart: 0,
    viewEnd: 100_000,
    width: 1000,
    totalFrames: 2_000_000,
    channels: 2,
    fineDivision: 160,
    targetPageBytes: 2 * 1024 * 1024,
    maxWindowBytes: 8 * 1024 * 1024,
  });
  assert.equal(entered.active, true);
  assert.equal(entered.mode, 'envelope');
  assert.equal(entered.division, 128);

  const cold = planPcmLod({
    viewStart: 0, viewEnd: 160_000, width: 1200, totalFrames: 2_000_000,
    channels: 2, fineDivision: 160, sourceActive: false,
  });
  const warm = planPcmLod({
    viewStart: 0, viewEnd: 160_000, width: 1200, totalFrames: 2_000_000,
    channels: 2, fineDivision: 160, sourceActive: true,
  });
  assert.equal(cold.active, false);
  assert.equal(warm.active, true);
  assert.ok(warm.division <= 160);

  const samples = planPcmLod({
    viewStart: 1000, viewEnd: 2000, width: 2000, totalFrames: 2_000_000,
    channels: 2, fineDivision: 160, sourceActive: true,
  });
  assert.equal(samples.mode, 'samples');
  assert.equal(samples.division, 1);
  assert.ok(samples.frameCount <= 4096);
});

test('PCM LOD refuses a source window beyond its byte budget', () => {
  const plan = planPcmLod({
    viewStart: 0,
    viewEnd: 100_000,
    width: 1000,
    totalFrames: 2_000_000,
    channels: 8,
    fineDivision: 160,
    maxWindowBytes: 1024,
  });
  assert.equal(plan.active, false);
  assert.equal(plan.reason, 'source byte budget');
});

test('PCM page slides to cover a wide multichannel viewport at a page edge', () => {
  const plan = planPcmLod({
    viewStart: 256_000,
    viewEnd: 665_600,
    width: 4096,
    totalFrames: 2_000_000,
    channels: 8,
    fineDivision: 160,
    maxWindowBytes: 16 * 1024 * 1024,
    targetPageBytes: 4 * 1024 * 1024,
    maxTextureRecords: 4096,
  });
  assert.equal(plan.active, true);
  assert.ok(plan.firstFrame <= 256_000);
  assert.ok(plan.firstFrame + plan.frameCount >= 665_600);
  assert.ok(plan.frameCount * 8 * 4 <= 16 * 1024 * 1024);
});

test('fragment shader has source envelope and exact sample dot paths', () => {
  assert.match(FRAGMENT_SHADER, /uniform int u_pcmMode/);
  assert.match(FRAGMENT_SHADER, /texelFetch\(u_pcm/);
  assert.match(FRAGMENT_SHADER, /u_pcmDrawPoints != 0/);
  assert.match(FRAGMENT_SHADER, /nearestPosition/);
  assert.match(FRAGMENT_SHADER, /u_pcmResident/);
  assert.match(FRAGMENT_SHADER, /float finitePcm/);
  assert.match(FRAGMENT_SHADER, /value == value/);
});

test('PCM draw plan maps visible samples to line and point geometry', () => {
  const draw = planPcmDraw({
    window: {
      firstFrame: 50, frameCount: 7, division: 1, records: 7,
      channels: 2, components: 1, mode: 'samples',
    },
    viewStart: 52,
    viewEnd: 55,
    width: 12,
  });
  assert.equal(draw.firstVisibleRecord, 1);
  assert.equal(draw.visibleRecordCount, 6);
  assert.equal(draw.xOriginPx, -4);
  assert.equal(draw.xStepPx, 4);
  assert.equal(draw.drawLines, true);
  assert.equal(draw.drawPoints, true);
  assert.equal(draw.xForLocalRecord(2), 4);
  assert.equal(draw.sampleOffset(0, 1), 3);
  assert.equal(planPcmDraw({
    window: {
      firstFrame: 50, frameCount: 7, division: 1, records: 7,
      channels: 2, components: 1, mode: 'samples',
    },
    viewStart: 52,
    viewEnd: 55,
    width: 6,
  }).drawPoints, false);
});

test('PCM LOD rejects nonfinite or impossible public configuration', () => {
  const valid = {
    viewStart: 0, viewEnd: 100, width: 100, totalFrames: 1000,
    channels: 2, fineDivision: 160,
  };
  const invalid = [
    {totalFrames: 0},
    {width: 0},
    {channels: 0},
    {channels: 256},
    {fineDivision: 0},
    {maxWindowBytes: 0},
    {targetPageBytes: 0},
    {maxTextureRecords: 0},
    {enterPixelsPerPeak: Number.NaN},
    {exitPixelsPerPeak: Number.POSITIVE_INFINITY},
    {enterPixelsPerPeak: 1, exitPixelsPerPeak: 1.1},
    {totalFrames: Number.MAX_SAFE_INTEGER + 1},
  ];
  for (const override of invalid) {
    assert.throws(() => planPcmLod({...valid, ...override}), /PCM LOD/);
  }
});

test('randomized PCM LOD plans stay within byte, texture, and coverage bounds', () => {
  let state = 0x10d5afe;
  const next = () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state;
  };
  const integer = (minimum, maximum) => minimum + (next() % (maximum - minimum + 1));
  const fineChoices = [1, 2, 3, 16, 64, 160, 256, 1024];
  for (let iteration = 0; iteration < 5000; iteration++) {
    const totalFrames = integer(1, 20_000_000);
    const viewStart = integer(-totalFrames, totalFrames * 2);
    const viewEnd = viewStart + integer(-1000, Math.max(1, Math.floor(totalFrames / 2)));
    const width = integer(1, 8192);
    const channels = integer(1, 32);
    const fineDivision = fineChoices[next() % fineChoices.length];
    const maxWindowBytes = integer(1, 32) * 1024 * 1024;
    const targetPageBytes = integer(1, 48) * 1024 * 1024;
    const maxTextureRecords = integer(1, 8192);
    const plan = planPcmLod({
      viewStart, viewEnd, width, totalFrames, channels, fineDivision,
      sourceActive: Boolean(next() & 1), maxWindowBytes, targetPageBytes,
      maxTextureRecords,
    });
    assert.ok(Number.isFinite(plan.framesPerPixel));
    assert.ok(Number.isFinite(plan.pixelsPerFinePeak));
    if (!plan.active) {
      assert.equal(plan.key, null);
      continue;
    }
    const start = Math.min(Math.max(0, viewStart), totalFrames - 1);
    const end = Math.min(Math.max(start + 1, viewEnd), totalFrames);
    assert.ok(plan.frameCount > 0);
    assert.ok(plan.division > 0 && plan.division <= fineDivision);
    assert.equal(plan.firstFrame % plan.division, 0);
    assert.ok(plan.firstFrame <= start);
    assert.ok(plan.firstFrame + plan.frameCount >= end);
    assert.ok(plan.frameCount * channels * 4 <= maxWindowBytes);
    assert.ok(Math.ceil(plan.frameCount / plan.division) <= maxTextureRecords);
    assert.equal(plan.mode, plan.division === 1 ? 'samples' : 'envelope');
  }
});

test('PCM draw plan rejects nonfinite drawing parameters and bad geometry', () => {
  const valid = {
    window: {
      firstFrame: 0, frameCount: 4, division: 1, records: 4,
      channels: 1, components: 1, mode: 'samples',
    },
    viewStart: 0,
    viewEnd: 4,
    width: 100,
  };
  assert.throws(() => planPcmDraw({...valid, pointMinPixelsPerFrame: Number.NaN}));
  assert.throws(() => planPcmDraw({...valid, pointRadiusPx: Number.POSITIVE_INFINITY}));
  assert.throws(() => planPcmDraw({...valid, lineWidthPx: 0}));
  assert.throws(() => planPcmDraw({
    ...valid,
    window: {...valid.window, mode: 'envelope', components: 1},
  }));
});

function pcmResponse(overrides = {}, payloadBytes = 16) {
  const headers = {
    'X-Pcm-First-Frame': '10',
    'X-Pcm-Frame-Count': '4',
    'X-Pcm-Division': '1',
    'X-Pcm-Record-Count': '4',
    'X-Pcm-Channels': '1',
    'X-Pcm-Components': '1',
    'X-Pcm-Mode': 'samples',
    'X-Pcm-Backend': 'test-reader',
    'X-Pcm-Raw-Cache-Hit': '0',
    'X-Pcm-Cache-Disposition': 'decoded',
    'X-Pcm-Range-Reader-Ran': '1',
    'X-Pcm-Range-Decode-Ran': '1',
    'X-Pcm-Range-Reader-Ms': '0.25',
    'X-Pcm-Raw-First-Frame': '0',
    'X-Pcm-Raw-Frame-Count': '32',
    'X-Pcm-Range-Event-Id': '1',
    'X-Pcm-Range-Event-Unix-Ms': '1700000000000',
    'X-Pcm-Cache-Bytes': '128',
    'X-Pcm-Payload-Bytes': String(payloadBytes),
    ...overrides,
  };
  for (const [key, value] of Object.entries(headers)) {
    if (value === null) delete headers[key];
  }
  const lower = new Map(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value]));
  const payload = new Uint8Array(payloadBytes);
  return {
    ok: true,
    status: 200,
    headers: {get: name => lower.get(name.toLowerCase()) ?? null},
    arrayBuffer: async () => payload.buffer.slice(0),
  };
}

function pcmRendererStub() {
  const renderer = Object.create(WebGL2PackedRenderer.prototype);
  Object.assign(renderer, {
    meta: {channels: 1},
    pcmFetchDebounceMs: 0,
    pcmUpload: null,
    pcmCacheHit: false,
    pcmCacheDisposition: 'none',
    pcmServerCacheBytes: 0,
    lastFetchMs: 0,
    lastUploadMs: 0,
    networkBytes: 0,
    uploadBytes: 0,
    uploadCount: 0,
    event: null,
    gl: {deleteTexture() {}},
    notifyPcmRangeEvent(detail) { this.event = detail; },
    uploadPcmTexture(records, channels, components, raw) {
      assert.equal(raw.byteLength, records * channels * components * 4);
      return {records};
    },
  });
  return renderer;
}

test('PCM HTTP metadata is validated before upload and debug notification', async () => {
  const originalFetch = globalThis.fetch;
  const plan = {
    active: true, key: '10:4:1', firstFrame: 10, frameCount: 4, division: 1,
  };
  try {
    const valid = pcmRendererStub();
    globalThis.fetch = async () => pcmResponse();
    const upload = await valid.fetchPcmWindow(plan);
    assert.equal(upload.frameCount, 4);
    assert.equal(valid.event.readerRan, true);
    assert.equal(valid.pcmServerCacheBytes, 128);

    const malformed = [
      {'X-Pcm-Payload-Bytes': null},
      {'X-Pcm-Payload-Bytes': '15'},
      {'X-Pcm-Range-Reader-Ran': '0'},
      {'X-Pcm-Range-Decode-Ran': '0'},
      {'X-Pcm-Raw-Frame-Count': '2'},
      {'X-Pcm-Raw-Cache-Hit': '1'},
      {'X-Pcm-Backend': 'bad\nlabel'},
      {'X-Pcm-Range-Reader-Ms': '-1'},
      {'X-Pcm-Record-Count': '5'},
    ];
    for (const overrides of malformed) {
      const renderer = pcmRendererStub();
      globalThis.fetch = async () => pcmResponse(overrides);
      await assert.rejects(renderer.fetchPcmWindow(plan));
      assert.equal(renderer.event, null);
      assert.equal(renderer.uploadCount, 0);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});
