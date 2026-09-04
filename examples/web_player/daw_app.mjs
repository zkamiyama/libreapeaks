import {
  clamp,
  chooseLevel,
  dbToGain,
  decodeWave,
  energyToLufs,
  loudnessColor,
  rgbCss,
  signedI16,
  spectralCodeColor,
  spectrogramCodeToDb,
  u32le,
  unpackSpectrogram12,
} from '/daw_render_math.mjs';

const $ = id => document.getElementById(id);

const state = {
  meta: null,
  viewStart: 0,
  viewEnd: 1,
  playhead: 0,
  renderSerial: 0,
  raf: 0,
  abortController: null,
  sourceActive: false,
};

function formatTime(seconds) {
  seconds = Math.max(0, Number(seconds) || 0);
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h
    ? `${h}:${String(m).padStart(2, '0')}:${s.toFixed(2).padStart(5, '0')}`
    : `${String(m).padStart(2, '0')}:${s.toFixed(2).padStart(5, '0')}`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  let body;
  try { body = await response.json(); } catch (_error) { body = {}; }
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

function cssCanvasSize(canvas) {
  const width = Math.max(1, Math.floor(canvas.clientWidth));
  const height = Math.max(1, Math.floor(canvas.clientHeight));
  const dpr = Math.min(2, Math.max(1, Number(globalThis.devicePixelRatio) || 1));
  const pixelWidth = Math.max(1, Math.round(width * dpr));
  const pixelHeight = Math.max(1, Math.round(height * dpr));
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {ctx, width, height, dpr};
}

function rgbaHeat(t, heatmap = true) {
  t = clamp(Number(t) || 0, 0, 1);
  if (!heatmap) {
    const v = Math.round(t * 255);
    return [v, v, v, 255];
  }
  const stops = [
    [0.00, [3, 3, 8]],
    [0.25, [13, 46, 158]],
    [0.50, [10, 217, 235]],
    [0.75, [255, 219, 31]],
    [1.00, [235, 20, 5]],
  ];
  for (let index = 1; index < stops.length; index++) {
    if (t <= stops[index][0]) {
      const [x0, c0] = stops[index - 1];
      const [x1, c1] = stops[index];
      const mix = (t - x0) / Math.max(1e-9, x1 - x0);
      return [
        Math.round(c0[0] + (c1[0] - c0[0]) * mix),
        Math.round(c0[1] + (c1[1] - c0[1]) * mix),
        Math.round(c0[2] + (c1[2] - c0[2]) * mix),
        255,
      ];
    }
  }
  return [235, 20, 5, 255];
}

function mode() {
  return $('displayMode')?.value || 'waveform';
}

function verticalScale() {
  return clamp(Number($('verticalScale')?.value || 1), 0.1, 32);
}

function frameToX(frame, width) {
  return (frame - state.viewStart) / Math.max(1, state.viewEnd - state.viewStart) * width;
}

function amplitudeToY(value, channel, height, gain = 1) {
  const channels = Math.max(1, Number(state.meta.channels));
  const laneHeight = height / channels;
  const center = laneHeight * (channel + 0.5);
  return center - (Number(value) * gain / verticalScale()) * laneHeight * 0.45;
}

function clear(ctx, width, height) {
  ctx.fillStyle = '#10151d';
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = '#303947';
  ctx.lineWidth = 1;
  const channels = Math.max(1, Number(state.meta.channels));
  for (let c = 1; c < channels; c++) {
    const y = height * c / channels;
    ctx.beginPath();
    ctx.moveTo(0, y + 0.5);
    ctx.lineTo(width, y + 0.5);
    ctx.stroke();
  }
}

function drawPlayhead(ctx, width, height) {
  const x = frameToX(state.playhead, width);
  if (x < 0 || x > width) return;
  ctx.strokeStyle = '#ffc440';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(x, 0);
  ctx.lineTo(x, height);
  ctx.stroke();
}

function drawDebug(ctx, width, height, labels) {
  if (!$('showGpuTiles')?.checked) return;
  const text = labels.filter(Boolean).join(' | ');
  if (!text) return;
  ctx.fillStyle = 'rgba(7,10,14,.82)';
  ctx.fillRect(0, Math.max(0, height - 22), width, 22);
  ctx.fillStyle = '#dce6f3';
  ctx.font = '11px ui-monospace, monospace';
  ctx.fillText(text, 8, height - 7);
}

function updateDawInfo(text) {
  const element = $('dawInfo');
  if (element) element.textContent = text;
}

function layerWindow(choice) {
  if (!choice) return null;
  const division = Math.max(1, Number(choice.division));
  const totalRecords = Math.max(0, Number(choice.record_count));
  const first = Math.max(0, Math.floor(state.viewStart / division) - 3);
  const last = Math.min(totalRecords, Math.ceil(state.viewEnd / division) + 4);
  return {first, count: Math.max(0, last - first), division};
}

async function fetchLayer(kind, choice, signal) {
  if (!choice) return null;
  const window = layerWindow(choice);
  if (!window?.count) return null;
  const query = new URLSearchParams({
    kind,
    layer: String(choice.layer_index),
    first: String(window.first),
    count: String(window.count),
  });
  const response = await fetch(`/api/gpu-records?${query}`, {signal});
  if (!response.ok) throw new Error(`${kind}: HTTP ${response.status}`);
  const raw = new Uint8Array(await response.arrayBuffer());
  return {
    kind,
    layer: Number(choice.layer_index),
    first: Number(response.headers.get('X-First-Record')),
    count: Number(response.headers.get('X-Record-Count')),
    channels: Number(response.headers.get('X-Channels')),
    bytesPerChannel: Number(response.headers.get('X-Bytes-Per-Channel-Record')),
    division: Number(response.headers.get('X-Division')),
    raw,
  };
}

function waveValues(upload, record, channel) {
  const offset = (record * upload.channels + channel) * 4;
  const maximum = signedI16(upload.raw[offset], upload.raw[offset + 1]);
  const minimum = signedI16(upload.raw[offset + 2], upload.raw[offset + 3]);
  return [
    decodeWave(maximum, state.meta.wave_encoding),
    decodeWave(minimum, state.meta.wave_encoding),
  ];
}

function drawWaveUpload(ctx, width, height, upload, {
  color = '#6edaa4',
  fillAlpha = 0.45,
  gain = 1,
} = {}) {
  if (!upload) return;
  const channels = Math.min(Number(state.meta.channels), upload.channels);
  for (let channel = 0; channel < channels; channel++) {
    const maxima = [];
    const minima = [];
    for (let record = 0; record < upload.count; record++) {
      const frame = (upload.first + record + 0.5) * upload.division;
      if (frame < state.viewStart - upload.division || frame > state.viewEnd + upload.division) continue;
      const x = frameToX(frame, width);
      const [maximum, minimum] = waveValues(upload, record, channel);
      maxima.push([x, amplitudeToY(maximum, channel, height, gain)]);
      minima.push([x, amplitudeToY(minimum, channel, height, gain)]);
    }
    if (!maxima.length) continue;
    ctx.beginPath();
    ctx.moveTo(maxima[0][0], maxima[0][1]);
    for (let i = 1; i < maxima.length; i++) ctx.lineTo(maxima[i][0], maxima[i][1]);
    for (let i = minima.length - 1; i >= 0; i--) ctx.lineTo(minima[i][0], minima[i][1]);
    ctx.closePath();
    ctx.globalAlpha = fillAlpha;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(maxima[0][0], maxima[0][1]);
    for (let i = 1; i < maxima.length; i++) ctx.lineTo(maxima[i][0], maxima[i][1]);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(minima[0][0], minima[0][1]);
    for (let i = 1; i < minima.length; i++) ctx.lineTo(minima[i][0], minima[i][1]);
    ctx.stroke();
  }
}

function nextPowerOfTwo(value) {
  return 2 ** Math.ceil(Math.log2(Math.max(1, Math.ceil(value))));
}

function sourcePlan(width) {
  if (state.meta.source_pcm?.available !== true) return null;
  const span = Math.max(1, state.viewEnd - state.viewStart);
  const fineCandidates = [
    ...(state.meta.gpu_layers?.waveform || []).map(layer => Number(layer.division)),
    Number(state.meta.levels?.[0]?.division || 1),
  ].filter(value => Number.isFinite(value) && value > 0);
  const fineDivision = Math.max(1, Math.min(...fineCandidates));
  const framesPerPixel = span / Math.max(1, width);
  const pixelsPerFinePeak = fineDivision / framesPerPixel;
  const threshold = state.sourceActive ? 1.1 : 1.5;
  if (pixelsPerFinePeak < threshold) return null;
  const division = framesPerPixel <= 1 ? 1 : Math.min(fineDivision, nextPowerOfTwo(framesPerPixel));
  const guard = Math.max(2, division * 2);
  const first = Math.floor(Math.max(0, state.viewStart - guard) / division) * division;
  const last = Math.min(
    state.meta.total_frames,
    Math.ceil(Math.min(state.meta.total_frames, state.viewEnd + guard) / division) * division,
  );
  const count = Math.max(0, last - first);
  const maxBytes = Number(state.meta.source_pcm.max_window_bytes || 0);
  if (!count || count * Number(state.meta.channels) * 4 > maxBytes) return null;
  return {first, count, division};
}

async function fetchSourcePcm(plan, signal) {
  if (!plan) return null;
  const query = new URLSearchParams({
    first: String(plan.first),
    count: String(plan.count),
    division: String(plan.division),
  });
  const response = await fetch(`/api/pcm-window?${query}`, {signal});
  if (!response.ok) return null;
  const raw = new Uint8Array(await response.arrayBuffer());
  const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
  const values = new Float32Array(raw.byteLength / 4);
  for (let i = 0; i < values.length; i++) values[i] = view.getFloat32(i * 4, true);
  return {
    first: Number(response.headers.get('X-Pcm-First-Frame')),
    frameCount: Number(response.headers.get('X-Pcm-Frame-Count')),
    division: Number(response.headers.get('X-Pcm-Division')),
    records: Number(response.headers.get('X-Pcm-Record-Count')),
    channels: Number(response.headers.get('X-Pcm-Channels')),
    components: Number(response.headers.get('X-Pcm-Components')),
    mode: response.headers.get('X-Pcm-Mode'),
    backend: response.headers.get('X-Pcm-Backend'),
    cacheDisposition: response.headers.get('X-Pcm-Cache-Disposition'),
    values,
  };
}

function drawSourcePcm(ctx, width, height, upload) {
  if (!upload) return;
  const channels = Math.min(Number(state.meta.channels), upload.channels);
  const color = '#f8e246';
  for (let channel = 0; channel < channels; channel++) {
    if (upload.mode === 'samples') {
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      let started = false;
      const pixelsPerFrame = width / Math.max(1, state.viewEnd - state.viewStart);
      for (let record = 0; record < upload.records; record++) {
        const frame = upload.first + record;
        if (frame < state.viewStart - 1 || frame > state.viewEnd + 1) continue;
        const x = frameToX(frame, width);
        const y = amplitudeToY(upload.values[record * channels + channel], channel, height);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        if (pixelsPerFrame >= 8) {
          ctx.moveTo(x + 2, y);
          ctx.arc(x, y, 2, 0, Math.PI * 2);
        }
      }
      ctx.stroke();
    } else {
      const maxima = [];
      const minima = [];
      for (let record = 0; record < upload.records; record++) {
        const frame = upload.first + (record + 0.5) * upload.division;
        if (frame < state.viewStart - upload.division || frame > state.viewEnd + upload.division) continue;
        const base = (record * channels + channel) * 2;
        maxima.push([frameToX(frame, width), amplitudeToY(upload.values[base], channel, height)]);
        minima.push([frameToX(frame, width), amplitudeToY(upload.values[base + 1], channel, height)]);
      }
      if (!maxima.length) continue;
      ctx.globalAlpha = 0.52;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(maxima[0][0], maxima[0][1]);
      for (const point of maxima.slice(1)) ctx.lineTo(point[0], point[1]);
      for (const point of [...minima].reverse()) ctx.lineTo(point[0], point[1]);
      ctx.closePath();
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = color;
      ctx.beginPath();
      ctx.moveTo(maxima[0][0], maxima[0][1]);
      for (const point of maxima.slice(1)) ctx.lineTo(point[0], point[1]);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(minima[0][0], minima[0][1]);
      for (const point of minima.slice(1)) ctx.lineTo(point[0], point[1]);
      ctx.stroke();
    }
  }
}

async function renderWaveform(ctx, width, height, signal) {
  const plan = sourcePlan(width);
  if (plan) {
    const pcm = await fetchSourcePcm(plan, signal);
    if (pcm) {
      state.sourceActive = true;
      drawSourcePcm(ctx, width, height, pcm);
      return [`PCM ${pcm.mode} ${pcm.backend} ${pcm.cacheDisposition}`];
    }
  }
  state.sourceActive = false;
  const desired = (state.viewEnd - state.viewStart) / Math.max(1, width);
  const waveChoice = chooseLevel(state.meta.gpu_layers?.waveform || [], desired);
  const wave = await fetchLayer('waveform', waveChoice, signal);
  drawWaveUpload(ctx, width, height, wave);
  return [wave ? `wave L${wave.layer} div=${wave.division}` : 'wave unavailable'];
}

async function renderSpectral(ctx, width, height, signal) {
  const desired = (state.viewEnd - state.viewStart) / Math.max(1, width);
  const waveChoice = chooseLevel(state.meta.gpu_layers?.waveform || [], desired);
  const targetDivision = waveChoice?.division || desired;
  const spectralChoice = chooseLevel(state.meta.gpu_layers?.spectral || [], targetDivision);
  const [wave, spectral] = await Promise.all([
    fetchLayer('waveform', waveChoice, signal),
    fetchLayer('spectral', spectralChoice, signal),
  ]);
  if (!wave || !spectral) return ['spectral layers unavailable'];
  drawWaveUpload(ctx, width, height, wave, {color: '#6edaa4', fillAlpha: 0.12});
  const lowHz = Number($('spectralLowHz').value);
  const highHz = Number($('spectralHighHz').value);
  const rangeMode = Number($('spectralRange').value);
  const reverse = $('spectralReverse').checked;
  const fadeNoise = $('spectralFadeNoise').checked;
  const opacity = Number($('analysisOpacity').value) / 100;
  const peakGain = dbToGain(Number($('analysisZoomDb').value));
  const nyquist = Number(state.meta.sample_rate) * 0.5;
  const channels = Math.min(wave.channels, spectral.channels);
  const xWidth = Math.max(1, width * wave.division / Math.max(1, state.viewEnd - state.viewStart) + 0.7);
  ctx.lineWidth = xWidth;
  for (let record = 0; record < wave.count; record++) {
    const frame = (wave.first + record + 0.5) * wave.division;
    if (frame < state.viewStart || frame > state.viewEnd) continue;
    const spectralRecord = Math.floor(frame / spectral.division) - spectral.first;
    if (spectralRecord < 0 || spectralRecord >= spectral.count) continue;
    const x = frameToX(frame, width);
    for (let channel = 0; channel < channels; channel++) {
      const code = u32le(spectral.raw, (spectralRecord * channels + channel) * 4);
      const color = spectralCodeColor(code, {
        lowHz, highHz, rangeMode, reverse, fadeNoise, nyquist,
      });
      const [maximum, minimum] = waveValues(wave, record, channel);
      ctx.strokeStyle = rgbCss(color, opacity);
      ctx.beginPath();
      ctx.moveTo(x, amplitudeToY(maximum, channel, height, peakGain));
      ctx.lineTo(x, amplitudeToY(minimum, channel, height, peakGain));
      ctx.stroke();
    }
  }
  return [`wave L${wave.layer} div=${wave.division}`, `spectral L${spectral.layer} div=${spectral.division}`];
}

async function renderSpectrogram(ctx, width, height, signal) {
  const desired = (state.viewEnd - state.viewStart) / Math.max(1, width);
  const choice = chooseLevel(state.meta.gpu_layers?.spectrogram || [], desired);
  const upload = await fetchLayer('spectrogram', choice, signal);
  if (!upload) return ['spectrogram unavailable'];
  const floorDb = Number($('specFloorDb').value);
  const ceilingDb = Number($('specCeilingDb').value);
  const lo = Math.min(floorDb, ceilingDb - 0.001);
  const hi = Math.max(ceilingDb, lo + 0.001);
  const gainDb = Number($('specGainDb').value);
  const contrast = clamp(Number($('specContrast').value), 0.05, 8);
  const heatmap = $('specHeatmap').checked;
  const frequencyLog = $('specFrequency').value === 'log';
  const channels = Number(state.meta.channels);
  const nyquist = Number(state.meta.sample_rate) * 0.5;
  const image = ctx.createImageData(width, height);
  const pixels = image.data;
  for (let y = 0; y < height; y++) {
    const lane = y / height * channels;
    const channel = clamp(Math.floor(lane), 0, channels - 1);
    const localY = lane - channel;
    let bin;
    if (frequencyLog) {
      const minFreq = Math.max(20, nyquist / 128);
      const frequency = Math.exp(
        Math.log(minFreq) + (Math.log(Math.max(minFreq + 1, nyquist)) - Math.log(minFreq)) * (1 - localY),
      );
      bin = clamp(frequency * 128 / Math.max(1, nyquist) - 0.5, 0, 127);
    } else {
      bin = clamp((1 - localY) * 128 - 0.5, 0, 127);
    }
    for (let x = 0; x < width; x++) {
      const frame = state.viewStart + (x + 0.5) / width * (state.viewEnd - state.viewStart);
      const record = clamp(Math.floor(frame / upload.division) - upload.first, 0, upload.count - 1);
      const code = unpackSpectrogram12(upload.raw, record, channel, bin, channels);
      const db = spectrogramCodeToDb(code, gainDb);
      const normalized = clamp((db - lo) / (hi - lo), 0, 1);
      const intensity = clamp(normalized ** contrast, 0, 1);
      const [r, g, b, a] = rgbaHeat(intensity, heatmap);
      const offset = (y * width + x) * 4;
      pixels[offset] = r; pixels[offset + 1] = g; pixels[offset + 2] = b; pixels[offset + 3] = a;
    }
  }
  ctx.putImageData(image, 0, 0);
  ctx.strokeStyle = 'rgba(255,255,255,.18)';
  for (let channel = 1; channel < channels; channel++) {
    const y = height * channel / channels;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }
  return [`spectrogram L${upload.layer} div=${upload.division} ${frequencyLog ? 'log' : 'linear'} Hz`];
}

function readF32LE(bytes, offset) {
  const view = new DataView(bytes.buffer, bytes.byteOffset + offset, 4);
  return view.getFloat32(0, true);
}

async function renderLoudness(ctx, width, height, signal) {
  const desired = (state.viewEnd - state.viewStart) / Math.max(1, width);
  const waveChoice = chooseLevel(state.meta.gpu_layers?.waveform || [], desired);
  const targetDivision = waveChoice?.division || desired;
  const loudChoice = chooseLevel(state.meta.gpu_layers?.loudness || [], targetDivision);
  const [wave, loud] = await Promise.all([
    fetchLayer('waveform', waveChoice, signal),
    fetchLayer('loudness', loudChoice, signal),
  ]);
  if (!wave || !loud) return ['loudness layers unavailable'];
  const style = Number($('loudnessStyle').value);
  const metric = Number($('loudnessMetric').value);
  const floorLu = Number($('loudnessFloorLu').value);
  const ceilingLu = Number($('loudnessCeilingLu').value);
  const offsetLu = Number($('loudnessOffsetLu').value);
  const transition = Number($('loudnessTransitionLu').value);
  const opacity = Number($('analysisOpacity').value) / 100;
  const peakGain = dbToGain(Number($('analysisZoomDb').value));
  const channels = Math.min(wave.channels, loud.channels);
  drawWaveUpload(ctx, width, height, wave, {
    color: '#6edaa4', fillAlpha: style === 1 ? 0.34 : 0.10, gain: peakGain,
  });
  const loudValue = (record, channel) => {
    const offset = (record * channels + channel) * 8 + (metric ? 4 : 0);
    return energyToLufs(readF32LE(loud.raw, offset)) + offsetLu;
  };
  if (style === 0) {
    ctx.lineWidth = Math.max(1, width * wave.division / Math.max(1, state.viewEnd - state.viewStart) + 0.7);
    for (let record = 0; record < wave.count; record++) {
      const frame = (wave.first + record + 0.5) * wave.division;
      if (frame < state.viewStart || frame > state.viewEnd) continue;
      const loudRecord = Math.floor(frame / loud.division) - loud.first;
      if (loudRecord < 0 || loudRecord >= loud.count) continue;
      const x = frameToX(frame, width);
      for (let channel = 0; channel < channels; channel++) {
        const lu = loudValue(loudRecord, channel);
        const color = loudnessColor(lu, transition);
        const [maximum, minimum] = waveValues(wave, record, channel);
        ctx.strokeStyle = rgbCss(color, opacity);
        ctx.beginPath();
        ctx.moveTo(x, amplitudeToY(maximum, channel, height, peakGain));
        ctx.lineTo(x, amplitudeToY(minimum, channel, height, peakGain));
        ctx.stroke();
      }
    }
  } else {
    const lo = Math.min(floorLu, ceilingLu - 0.001);
    const hi = Math.max(ceilingLu, lo + 0.001);
    const laneHeight = height / channels;
    for (let channel = 0; channel < channels; channel++) {
      let previous = null;
      for (let record = 0; record < loud.count; record++) {
        const frame = (loud.first + record + 0.5) * loud.division;
        if (frame < state.viewStart - loud.division || frame > state.viewEnd + loud.division) continue;
        const lu = loudValue(record, channel);
        const frac = clamp((lu - lo) / (hi - lo), 0, 1);
        const point = {
          x: frameToX(frame, width),
          y: channel * laneHeight + (1 - frac) * laneHeight,
          color: loudnessColor(lu, transition),
        };
        if (previous) {
          ctx.strokeStyle = rgbCss(point.color, opacity);
          ctx.lineWidth = 2;
          ctx.beginPath(); ctx.moveTo(previous.x, previous.y); ctx.lineTo(point.x, point.y); ctx.stroke();
        }
        previous = point;
      }
    }
  }
  return [`wave L${wave.layer} div=${wave.division}`, `loudness L${loud.layer} div=${loud.division}`];
}

async function render(serial) {
  if (!state.meta || serial !== state.renderSerial) return;
  state.abortController?.abort();
  const controller = new AbortController();
  state.abortController = controller;
  const canvas = $('dawCanvas');
  const {ctx, width, height} = cssCanvasSize(canvas);
  clear(ctx, width, height);
  const started = performance.now();
  let labels = [];
  try {
    if (mode() === 'spectral') labels = await renderSpectral(ctx, width, height, controller.signal);
    else if (mode() === 'spectrogram') labels = await renderSpectrogram(ctx, width, height, controller.signal);
    else if (mode() === 'loudness') labels = await renderLoudness(ctx, width, height, controller.signal);
    else labels = await renderWaveform(ctx, width, height, controller.signal);
    if (serial !== state.renderSerial || controller.signal.aborted) return;
    drawPlayhead(ctx, width, height);
    drawDebug(ctx, width, height, labels);
    const elapsed = performance.now() - started;
    updateDawInfo(
      `${mode()} | ${formatTime(state.viewStart / state.meta.sample_rate)}…${formatTime(state.viewEnd / state.meta.sample_rate)} `
      + `| V-FS=${verticalScale().toFixed(2)}× | render=${elapsed.toFixed(1)}ms | ${labels.join(' | ')}`,
    );
  } catch (error) {
    if (error?.name !== 'AbortError') updateDawInfo(`DAW renderer error: ${String(error)}`);
  } finally {
    if (state.abortController === controller) state.abortController = null;
  }
}

function scheduleRender() {
  state.renderSerial++;
  state.abortController?.abort();
  if (state.raf) return;
  state.raf = requestAnimationFrame(() => {
    state.raf = 0;
    render(state.renderSerial);
  });
}

function minSpan() {
  if (state.meta.source_pcm?.available === true) {
    return Math.max(4, Number(state.meta.source_lod?.min_view_frames) || 4);
  }
  return Math.max(64, Number(state.meta.sample_rate) / 50);
}

function setView(start, end) {
  let span = Math.max(minSpan(), Number(end) - Number(start));
  span = Math.min(span, state.meta.total_frames);
  start = clamp(Number(start), 0, Math.max(0, state.meta.total_frames - span));
  const nextEnd = start + span;
  if (Math.abs(start - state.viewStart) < 0.01 && Math.abs(nextEnd - state.viewEnd) < 0.01) return;
  state.viewStart = start;
  state.viewEnd = nextEnd;
  scheduleRender();
}

function zoom(factor, anchor = 0.5) {
  const span = state.viewEnd - state.viewStart;
  const target = state.viewStart + span * clamp(anchor, 0, 1);
  const nextSpan = span * factor;
  setView(target - nextSpan * anchor, target + nextSpan * (1 - anchor));
}

function wheelSteps(event, element) {
  let delta = event.deltaY;
  if (event.deltaMode === WheelEvent.DOM_DELTA_LINE) delta *= 40;
  else if (event.deltaMode === WheelEvent.DOM_DELTA_PAGE) delta *= Math.max(1, element.clientHeight);
  return clamp(-delta / 120, -4, 4);
}

function installMirrorInteraction(element) {
  let drag = null;
  element.addEventListener('wheel', event => {
    const steps = wheelSteps(event, element);
    if (!steps || event.ctrlKey) return;
    const rect = element.getBoundingClientRect();
    const anchor = clamp((event.clientX - rect.left) / Math.max(1, rect.width), 0, 1);
    zoom(0.72 ** steps, anchor);
  }, {capture: true, passive: false});
  element.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    drag = {id: event.pointerId, x: event.clientX, start: state.viewStart, end: state.viewEnd};
  }, true);
  element.addEventListener('pointermove', event => {
    if (!drag || drag.id !== event.pointerId) return;
    const dx = event.clientX - drag.x;
    const framesPerPx = (drag.end - drag.start) / Math.max(1, element.clientWidth);
    const shift = -dx * framesPerPx;
    setView(drag.start + shift, drag.end + shift);
  }, true);
  const finish = event => {
    if (drag?.id === event.pointerId) drag = null;
  };
  element.addEventListener('pointerup', finish, true);
  element.addEventListener('pointercancel', finish, true);
}

function refreshControlVisibility() {
  const current = mode();
  $('analysisCommonGroup').hidden = !['spectral', 'loudness'].includes(current);
  $('spectralGroup').hidden = current !== 'spectral';
  $('loudnessGroup').hidden = current !== 'loudness';
  $('spectrogramGroup').hidden = current !== 'spectrogram';
}

function wireControls() {
  const rerenderIds = [
    'displayMode', 'verticalScale', 'showGpuTiles',
    'analysisZoomDb', 'analysisOpacity',
    'spectralRange', 'spectralLowHz', 'spectralHighHz', 'spectralReverse', 'spectralFadeNoise',
    'loudnessMetric', 'loudnessStyle', 'loudnessFloorLu', 'loudnessCeilingLu', 'loudnessOffsetLu', 'loudnessTransitionLu',
    'specHeatmap', 'specGainDb', 'specFloorDb', 'specCeilingDb', 'specContrast', 'specFrequency',
  ];
  for (const id of rerenderIds) {
    const element = $(id);
    if (!element) continue;
    const eventName = element.matches('input[type="range"], input[type="number"]') ? 'input' : 'change';
    element.addEventListener(eventName, () => {
      if (id === 'displayMode') refreshControlVisibility();
      scheduleRender();
    });
  }
  $('zoomInButton')?.addEventListener('click', () => zoom(0.5));
  $('zoomOutButton')?.addEventListener('click', () => zoom(2));
  $('fullButton')?.addEventListener('click', () => setView(0, state.meta.total_frames));
  $('overviewBase')?.parentElement?.addEventListener('pointerdown', event => {
    const rect = event.currentTarget.getBoundingClientRect();
    state.playhead = clamp((event.clientX - rect.left) / Math.max(1, rect.width), 0, 1) * state.meta.total_frames;
    scheduleRender();
  }, true);
  const stack = $('webglStack');
  if (stack) installMirrorInteraction(stack);
  const audio = $('audio');
  audio?.addEventListener('timeupdate', () => {
    state.playhead = audio.currentTime * state.meta.sample_rate;
    if ($('followPlayhead')?.checked && (state.playhead < state.viewStart || state.playhead > state.viewEnd)) {
      const span = state.viewEnd - state.viewStart;
      setView(state.playhead - span * 0.25, state.playhead + span * 0.75);
    } else {
      scheduleRender();
    }
  });
  refreshControlVisibility();
}

function configureAvailableModes() {
  const select = $('displayMode');
  const availability = {
    waveform: true,
    spectral: Boolean(state.meta.gpu_layers?.spectral?.length),
    spectrogram: Boolean(state.meta.gpu_layers?.spectrogram?.length),
    loudness: Boolean(state.meta.gpu_layers?.loudness?.length),
  };
  for (const option of select.options) option.disabled = !availability[option.value];
  if (!availability[select.value]) select.value = 'waveform';
}

async function loadRpkx() {
  const summary = $('rpkxSummary');
  const body = $('rpkxBody');
  const detail = $('rpkxDetail');
  try {
    const inventory = await fetchJson('/api/rpkx');
    body.textContent = '';
    if (!inventory.present) {
      summary.textContent = `${inventory.peaks_name || state.meta.peaks_name}: no RPKX container attached`;
      detail.textContent = 'No application-extension chunks are present.';
      return;
    }
    summary.textContent = `chunks=${inventory.chunk_count} flags=0x${Number(inventory.container_flags).toString(16).padStart(8, '0')} `
      + `source(mtime=0x${Number(inventory.source_mtime_low32).toString(16).padStart(8, '0')}, `
      + `size=0x${Number(inventory.source_size_low32).toString(16).padStart(8, '0')})`;
    for (const chunk of inventory.chunks) {
      const tr = document.createElement('tr');
      const values = [
        chunk.index, chunk.namespace, chunk.kind, chunk.version,
        `0x${Number(chunk.flags).toString(16).padStart(8, '0')}`,
        Number(chunk.payload_bytes).toLocaleString(), chunk.preview.ascii,
      ];
      for (const value of values) {
        const td = document.createElement('td');
        td.textContent = String(value);
        tr.appendChild(td);
      }
      tr.tabIndex = 0;
      const show = () => {
        detail.textContent = [
          `namespace: ${chunk.namespace}`,
          `namespace bytes: ${chunk.namespace_hex}`,
          `kind: ${chunk.kind} (${chunk.kind_hex})`,
          `version: ${chunk.version}`,
          `flags: 0x${Number(chunk.flags).toString(16).padStart(8, '0')}`,
          `payload: ${Number(chunk.payload_bytes).toLocaleString()} bytes`,
          `preview hex: ${chunk.preview.hex || '(empty)'}`,
          `preview ascii: ${chunk.preview.ascii || '(empty)'}`,
        ].join('\n');
      };
      tr.addEventListener('click', show);
      tr.addEventListener('focus', show);
      body.appendChild(tr);
    }
    if (inventory.chunks.length) body.firstElementChild?.click();
  } catch (error) {
    summary.textContent = `RPKX inventory unavailable: ${String(error)}`;
  }
}

async function waitForBaseApp() {
  for (let count = 0; count < 120; count++) {
    if ($('audio') && $('overviewBase') && document.documentElement.dataset.renderer) return;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
}

async function init() {
  state.meta = await fetchJson('/api/meta');
  state.viewStart = 0;
  state.viewEnd = state.meta.total_frames;
  state.playhead = 0;
  document.body.classList.add('dawMode');
  await waitForBaseApp();
  configureAvailableModes();
  wireControls();
  await loadRpkx();
  const observer = new ResizeObserver(() => scheduleRender());
  observer.observe($('webglStack'));
  scheduleRender();
}

init().catch(error => updateDawInfo(String(error)));
