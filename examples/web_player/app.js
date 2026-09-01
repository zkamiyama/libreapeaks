const $ = (id) => document.getElementById(id);

const colors = {
  bg: '#11141a', grid: '#363e4b', wave: '#6edaa4', spectral: '#5faeff',
  playhead: '#ffc440', tileEdge: '#e678ff', text: '#d2dae6',
};

const state = {
  meta: null,
  viewStart: 0,
  viewEnd: 1,
  playhead: 0,
  plan: null,
  showTiles: true,
  follow: true,
  verticalScale: 1,
  renderSerial: 0,
  renderRaf: 0,
  overviewRaf: 0,
  planAbortController: null,
};

class Lru {
  constructor(capacity = 96) {
    this.capacity = capacity;
    this.map = new Map();
    this.hits = 0;
    this.misses = 0;
  }
  get(key, loader) {
    if (this.map.has(key)) {
      const value = this.map.get(key);
      this.map.delete(key);
      this.map.set(key, value);
      this.hits++;
      return value;
    }
    this.misses++;
    const value = Promise.resolve().then(loader).catch((error) => {
      this.map.delete(key);
      throw error;
    });
    this.map.set(key, value);
    while (this.map.size > this.capacity) this.map.delete(this.map.keys().next().value);
    return value;
  }
}
const tileCache = new Lru(96);

function signedI16(lo, hi) {
  const value = lo | (hi << 8);
  return value & 0x8000 ? value - 0x10000 : value;
}
function decodeAmplitude(code) {
  if (state.meta.wave_encoding === 'RPKN') return code / (code < 0 ? 32768 : 32767);
  const neg = code < 0;
  const mag = Math.abs(code);
  const amp = mag <= 24576 ? mag / 24576 : 2 ** ((mag - 24576) / 1024);
  return neg ? -amp : amp;
}
function u32le(bytes, o) {
  return (bytes[o] | (bytes[o+1] << 8) | (bytes[o+2] << 16) | (bytes[o+3] << 24)) >>> 0;
}
function formatTime(seconds) {
  seconds = Math.max(0, seconds || 0);
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${s.toFixed(2).padStart(5,'0')}`
           : `${String(m).padStart(2,'0')}:${s.toFixed(2).padStart(5,'0')}`;
}
function clamp(value, lo, hi) {
  return Math.max(lo, Math.min(hi, value));
}
function frameToX(frame, width) {
  return (frame - state.viewStart) / Math.max(1, state.viewEnd - state.viewStart) * width;
}
function resizeCanvas(canvas) {
  const w = Math.max(1, Math.floor(canvas.clientWidth));
  const h = Math.max(1, Math.floor(canvas.clientHeight));
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  return [w, h];
}
function clearAndGrid(ctx, w, h, channels) {
  ctx.fillStyle = colors.bg;
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = colors.grid;
  ctx.lineWidth = 1;
  for (let c = 0; c <= channels; c++) {
    const y = h * c / channels;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  for (let c = 0; c < channels; c++) {
    const y = h * (c + 0.5) / channels;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
}
function drawTileBand(ctx, x0, x1, h, label, odd) {
  if (!state.showTiles) return;
  ctx.fillStyle = odd ? 'rgba(80,160,255,.07)' : 'rgba(255,255,255,.035)';
  ctx.fillRect(x0, 0, Math.max(1, x1 - x0), h);
  ctx.strokeStyle = colors.tileEdge;
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(x0, 0); ctx.lineTo(x0, h); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = colors.text;
  ctx.font = '11px ui-monospace, monospace';
  ctx.fillText(label, x0 + 4, 14);
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${response.status}`);
  return body;
}
async function fetchTexture(key, url) {
  return tileCache.get(key, async () => {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`${key}: HTTP ${response.status}`);
    const raw = new Uint8Array(await response.arrayBuffer());
    return {
      raw,
      first: Number(response.headers.get('X-First-Peak')),
      width: Number(response.headers.get('X-Texture-Width')),
      height: Number(response.headers.get('X-Texture-Height')),
      division: Number(response.headers.get('X-Division')),
    };
  });
}

async function renderOverview() {
  const canvas = $('overviewBase');
  const [w, h] = resizeCanvas(canvas);
  resizeCanvas($('overviewOverlay'));
  const response = await fetch(`/api/overview?width=${w}&height=${h}&start=0&end=${state.meta.total_frames}`);
  const bytes = new Uint8ClampedArray(await response.arrayBuffer());
  canvas.getContext('2d').putImageData(new ImageData(bytes, w, h), 0, 0);
  drawOverlays();
}

function scheduleOverview() {
  if (!state.meta || state.overviewRaf) return;
  state.overviewRaf = requestAnimationFrame(() => {
    state.overviewRaf = 0;
    renderOverview().catch((error) => {
      $('tileInfo').textContent = `overview: ${String(error)}`;
    });
  });
}

async function renderWave(serial, signal) {
  const canvas = $('waveBase');
  const [w, h] = resizeCanvas(canvas);
  resizeCanvas($('waveOverlay'));
  const ctx = canvas.getContext('2d');
  clearAndGrid(ctx, w, h, state.meta.channels);
  const plan = await fetchJson(
    `/api/plan?start=${Math.floor(state.viewStart)}&end=${Math.ceil(state.viewEnd)}&width=${w}`,
    {signal},
  );
  if (serial !== state.renderSerial) return;
  state.plan = plan;
  const textures = await Promise.all(plan.tiles.map(({level_index, tile_index}) =>
    fetchTexture(`W:${level_index}:${tile_index}`, `/api/wave-tile?level=${level_index}&tile=${tile_index}`)
      .then(texture => ({texture, level_index, tile_index}))
  ));
  if (serial !== state.renderSerial) return;

  for (const {texture, level_index, tile_index} of textures) {
    const x0 = frameToX(texture.first * texture.division, w);
    const x1 = frameToX((texture.first + texture.width) * texture.division, w);
    drawTileBand(ctx, x0, x1, h, `L${level_index} T${tile_index}`, tile_index & 1);
  }

  const bandH = h / state.meta.channels;
  ctx.strokeStyle = colors.wave;
  ctx.lineWidth = 1;
  for (const {texture} of textures) {
    for (let c = 0; c < Math.min(state.meta.channels, texture.height); c++) {
      const center = bandH * (c + 0.5);
      const scale = bandH * 0.45 / state.verticalScale;
      const row = c * texture.width * 4;
      ctx.beginPath();
      for (let i = 0; i < texture.width; i++) {
        const frame = (texture.first + i) * texture.division;
        if (frame < state.viewStart || frame > state.viewEnd) continue;
        const o = row + i * 4;
        const max = decodeAmplitude(signedI16(texture.raw[o], texture.raw[o+1]));
        const min = decodeAmplitude(signedI16(texture.raw[o+2], texture.raw[o+3]));
        const x = frameToX(frame, w);
        ctx.moveTo(x, center - max * scale);
        ctx.lineTo(x, center - min * scale);
      }
      ctx.stroke();
    }
  }
}

async function renderSpectrum(serial) {
  const canvas = $('spectrumBase');
  const [w, h] = resizeCanvas(canvas);
  resizeCanvas($('spectrumOverlay'));
  const ctx = canvas.getContext('2d');
  clearAndGrid(ctx, w, h, state.meta.channels);
  if (!state.plan?.spectral) return;
  const spectral = state.plan.spectral;
  const division = spectral.division;
  const tilePeaks = state.meta.tile_peaks;
  const firstTile = Math.floor(Math.floor(state.viewStart / division) / tilePeaks);
  const lastTile = Math.floor(Math.ceil(state.viewEnd / division) / tilePeaks);
  const requests = [];
  for (let tile = firstTile; tile <= lastTile; tile++) {
    requests.push(
      fetchTexture(`S:${spectral.layer_index}:${tile}`, `/api/spectral-tile?layer=${spectral.layer_index}&tile=${tile}`)
        .then(texture => ({texture, tile})).catch(() => null)
    );
  }
  const textures = (await Promise.all(requests)).filter(Boolean);
  if (serial !== state.renderSerial) return;
  for (const {texture, tile} of textures) {
    const x0 = frameToX(texture.first * division, w);
    const x1 = frameToX((texture.first + texture.width) * division, w);
    drawTileBand(ctx, x0, x1, h, `S${spectral.layer_index} T${tile}`, tile & 1);
  }

  const bandH = h / state.meta.channels;
  const logLo = Math.log(20);
  const logHi = Math.log(Math.max(21, state.meta.sample_rate / 2));
  for (const {texture} of textures) {
    for (let c = 0; c < Math.min(state.meta.channels, texture.height); c++) {
      const row = c * texture.width * 4;
      const laneTop = c * bandH;
      for (let i = 0; i < texture.width; i++) {
        const frame = (texture.first + i) * division;
        if (frame < state.viewStart || frame > state.viewEnd) continue;
        const code = u32le(texture.raw, row + i * 4);
        const frequency = code & 0x7fff;
        const density = (code >>> 15) & 0x3fff;
        if (!frequency) continue;
        let frac = (Math.log(Math.max(20, frequency)) - logLo) / Math.max(1e-9, logHi - logLo);
        frac = Math.max(0, Math.min(1, frac));
        const x = frameToX(frame, w);
        const y = laneTop + bandH * (1 - frac);
        const alpha = 0.18 + 0.82 * density / 16383;
        ctx.fillStyle = `rgba(95,174,255,${alpha})`;
        ctx.fillRect(Math.round(x), Math.round(y), 1, 2);
      }
    }
  }
}

async function renderView(serial = state.renderSerial) {
  if (!state.meta || serial !== state.renderSerial) return;
  const controller = new AbortController();
  state.planAbortController = controller;
  try {
    await renderWave(serial, controller.signal);
    if (serial !== state.renderSerial) return;
    await renderSpectrum(serial);
    if (serial === state.renderSerial) updateDiagnostics();
  } catch (error) {
    if (error?.name !== 'AbortError' && serial === state.renderSerial) {
      $('tileInfo').textContent = String(error);
    }
  } finally {
    if (state.planAbortController === controller) state.planAbortController = null;
    drawOverlays();
  }
}

function scheduleRender() {
  if (!state.meta) return;
  state.renderSerial++;
  state.planAbortController?.abort();
  if (state.renderRaf) return;
  state.renderRaf = requestAnimationFrame(() => {
    state.renderRaf = 0;
    renderView(state.renderSerial);
  });
}

function drawOverlays() {
  if (!state.meta) return;
  for (const id of ['waveOverlay', 'spectrumOverlay']) {
    const canvas = $(id); const [w, h] = resizeCanvas(canvas); const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, w, h);
    const x = frameToX(state.playhead, w);
    if (x >= 0 && x <= w) {
      ctx.strokeStyle = colors.playhead; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
    }
  }
  const canvas = $('overviewOverlay'); const [w, h] = resizeCanvas(canvas); const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, w, h);
  const x0 = state.viewStart / state.meta.total_frames * w;
  const x1 = state.viewEnd / state.meta.total_frames * w;
  ctx.fillStyle = 'rgba(255,255,255,.08)'; ctx.fillRect(x0, 0, Math.max(1, x1-x0), h);
  ctx.strokeStyle = colors.tileEdge; ctx.strokeRect(x0, .5, Math.max(1, x1-x0), h-1);
  const xp = state.playhead / state.meta.total_frames * w;
  ctx.strokeStyle = colors.playhead; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(xp,0); ctx.lineTo(xp,h); ctx.stroke();
}

function setView(start, end, render = true) {
  const minSpan = Math.max(64, state.meta.sample_rate / 50);
  let span = Math.max(minSpan, end - start);
  span = Math.min(span, state.meta.total_frames);
  start = Math.max(0, Math.min(start, state.meta.total_frames - span));
  const nextEnd = start + span;
  if (Math.abs(start - state.viewStart) < 0.01 && Math.abs(nextEnd - state.viewEnd) < 0.01) return;
  state.viewStart = start;
  state.viewEnd = nextEnd;
  drawOverlays();
  if (render) scheduleRender();
}
function zoomWindow(start, end, factor, anchor = 0.5) {
  anchor = clamp(anchor, 0, 1);
  const span = end - start;
  const target = start + span * anchor;
  const newSpan = span * factor;
  return [target - newSpan * anchor, target + newSpan * (1 - anchor)];
}
function zoom(factor, anchor = 0.5) {
  const [start, end] = zoomWindow(state.viewStart, state.viewEnd, factor, anchor);
  setView(start, end);
}
function verticalScaleFromWheel(current, steps) {
  return clamp(current * (1.15 ** (-steps)), 0.1, 32);
}
function setVerticalScale(value, render = true) {
  const next = clamp(Number(value), 0.1, 32);
  if (!Number.isFinite(next)) return;
  if (Math.abs(next - state.verticalScale) < 1e-6) return;
  state.verticalScale = next;
  const slider = $('verticalScale');
  if (slider) slider.value = String(next);
  const label = $('verticalValue');
  if (label) label.textContent = next.toFixed(2);
  if (render) scheduleRender();
  else updateDiagnostics();
}
function seekFrame(frame) {
  frame = Math.max(0, Math.min(frame, state.meta.total_frames));
  $('audio').currentTime = frame / state.meta.sample_rate;
  state.playhead = frame;
  drawOverlays();
}

function wheelSteps(event, element) {
  let delta = event.deltaY;
  if (event.deltaMode === WheelEvent.DOM_DELTA_LINE) delta *= 40;
  else if (event.deltaMode === WheelEvent.DOM_DELTA_PAGE) delta *= Math.max(1, element.clientHeight);
  return clamp(-delta / 120, -4, 4);
}

function installCanvasInteraction(element) {
  let drag = null;
  element.addEventListener('wheel', (event) => {
    event.preventDefault();
    const steps = wheelSteps(event, element);
    if (!steps) return;
    if (event.ctrlKey) {
      setVerticalScale(verticalScaleFromWheel(state.verticalScale, steps));
      return;
    }
    const rect = element.getBoundingClientRect();
    const anchor = clamp((event.clientX - rect.left) / Math.max(1, rect.width), 0, 1);
    zoom(0.72 ** steps, anchor);
  }, {passive: false});
  element.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    element.setPointerCapture(event.pointerId);
    element.classList.add('isDragging');
    drag = {pointerId: event.pointerId, x: event.clientX, start: state.viewStart, end: state.viewEnd, moved: 0};
  });
  element.addEventListener('pointermove', (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.x; drag.moved = Math.max(drag.moved, Math.abs(dx));
    const framesPerPx = (drag.end - drag.start) / Math.max(1, element.clientWidth);
    const shift = -dx * framesPerPx;
    setView(drag.start + shift, drag.end + shift);
  });
  const finishPointer = (event, seek) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (seek && drag.moved < 4) {
      const rect = element.getBoundingClientRect();
      const ratio = clamp((event.clientX - rect.left) / Math.max(1, rect.width), 0, 1);
      seekFrame(state.viewStart + ratio * (state.viewEnd - state.viewStart));
    }
    if (element.hasPointerCapture?.(event.pointerId)) element.releasePointerCapture(event.pointerId);
    drag = null;
    element.classList.remove('isDragging');
  };
  element.addEventListener('pointerup', (event) => finishPointer(event, true));
  element.addEventListener('pointercancel', (event) => finishPointer(event, false));
  element.addEventListener('lostpointercapture', () => {
    drag = null;
    element.classList.remove('isDragging');
  });
}

function updateDiagnostics() {
  const p = state.plan;
  if (!p || !state.meta) return;
  const waveTiles = p.tiles.map(t => `L${t.level_index}/T${t.tile_index}`).join(', ');
  const s = p.spectral;
  const div = s ? `S${s.layer_index} div=${s.division}` : 'none';
  const span = Math.max(1, state.viewEnd - state.viewStart);
  const horizontalZoom = state.meta.total_frames / span;
  $('viewportInfo').textContent = `${formatTime(state.viewStart/state.meta.sample_rate)}…${formatTime(state.viewEnd/state.meta.sample_rate)} | H=${horizontalZoom.toFixed(2)}× V-FS=${state.verticalScale.toFixed(2)} | wave L${p.level_index} div=${p.division}, ${p.peaks_per_pixel.toFixed(2)} peaks/px | spectral ${div}`;
  $('tileInfo').textContent = waveTiles || '(no wave tiles)';
  $('cacheInfo').textContent = `${tileCache.map.size}/${tileCache.capacity} resident/pending · hit=${tileCache.hits} miss=${tileCache.misses}`;
}

function updateTransport() {
  const audio = $('audio');
  state.playhead = audio.currentTime * state.meta.sample_rate;
  $('positionSlider').value = Math.round(state.playhead / state.meta.total_frames * 100000);
  $('timeLabel').textContent = `${formatTime(audio.currentTime)} / ${formatTime(state.meta.duration_seconds)}`;
  if (state.follow && (state.playhead < state.viewStart || state.playhead > state.viewEnd)) {
    const span = state.viewEnd - state.viewStart;
    setView(state.playhead - span * .25, state.playhead + span * .75);
  } else {
    drawOverlays();
  }
}

async function init() {
  state.meta = await fetchJson('/api/meta');
  state.viewStart = 0; state.viewEnd = state.meta.total_frames;
  $('mediaInfo').textContent = `${state.meta.audio_name} · ${state.meta.sample_rate} Hz · ${state.meta.channels} ch · ${state.meta.wave_encoding}`;
  $('apiInfo').textContent = `tile_peaks=${state.meta.tile_peaks}; levels=${state.meta.levels.length}; default_divisions=[${state.meta.default_divisions.join(', ')}]; coarsest envelope_texture=${state.meta.coarsest_envelope_texture.width}×${state.meta.coarsest_envelope_texture.height}/${state.meta.coarsest_envelope_texture.bytes} bytes; cache=${state.meta.generated_cache ? 'generated by libreapeaks' : state.meta.peaks_name}`;

  installCanvasInteraction($('waveStack'));
  installCanvasInteraction($('spectrumStack'));
  $('overviewBase').parentElement.addEventListener('pointerdown', event => {
    const rect = event.currentTarget.getBoundingClientRect();
    seekFrame((event.clientX - rect.left) / rect.width * state.meta.total_frames);
  });

  $('playButton').onclick = async () => {
    const audio = $('audio');
    if (audio.paused) await audio.play(); else audio.pause();
  };
  $('stopButton').onclick = () => { $('audio').pause(); seekFrame(0); };
  $('zoomInButton').onclick = () => zoom(.5);
  $('zoomOutButton').onclick = () => zoom(2);
  $('fullButton').onclick = () => setView(0, state.meta.total_frames);
  $('showTiles').onchange = event => { state.showTiles = event.target.checked; scheduleRender(); };
  $('followPlayhead').onchange = event => { state.follow = event.target.checked; };
  $('verticalScale').oninput = event => setVerticalScale(event.target.value);
  $('positionSlider').oninput = event => seekFrame(Number(event.target.value)/100000 * state.meta.total_frames);
  $('audio').addEventListener('play', () => $('playButton').textContent = 'Pause');
  $('audio').addEventListener('pause', () => $('playButton').textContent = 'Play');
  $('audio').addEventListener('timeupdate', updateTransport);

  const ro = new ResizeObserver(() => {
    scheduleOverview();
    scheduleRender();
  });
  ro.observe(document.querySelector('main'));
  await renderOverview();
  state.renderSerial++;
  await renderView(state.renderSerial);
  updateTransport();

  const tick = () => { if (!$('audio').paused) updateTransport(); requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}

if (typeof globalThis !== 'undefined') {
  globalThis.__libreapeaksInteractionMath = {
    clamp,
    wheelSteps,
    zoomWindow,
    verticalScaleFromWheel,
  };
}

if (typeof document !== 'undefined') {
  init().catch(error => { document.body.innerHTML += `<pre>${String(error)}</pre>`; });
}
