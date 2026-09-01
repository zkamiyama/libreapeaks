const PAGE_RECORDS = 512;
const TARGET_PAGE_BYTES = 256 * 1024;
const PCM_ENTER_PIXELS_PER_PEAK = 1.5;
const PCM_EXIT_PIXELS_PER_PEAK = 1.1;
const PCM_MAX_TEXTURE_RECORDS = 4096;

export function nextPowerOfTwo(value) {
  const integer = Math.max(1, Math.ceil(Number(value) || 1));
  return 2 ** Math.ceil(Math.log2(integer));
}

export function planPcmLod({
  viewStart,
  viewEnd,
  width,
  totalFrames,
  channels,
  fineDivision,
  sourceActive = false,
  enterPixelsPerPeak = PCM_ENTER_PIXELS_PER_PEAK,
  exitPixelsPerPeak = PCM_EXIT_PIXELS_PER_PEAK,
  maxWindowBytes = 16 * 1024 * 1024,
  targetPageBytes = 1 * 1024 * 1024,
  maxTextureRecords = PCM_MAX_TEXTURE_RECORDS,
} = {}) {
  const integer = (value, label) => {
    const numeric = Number(value);
    if (!Number.isSafeInteger(numeric)) throw new Error(`PCM LOD ${label} must be a safe integer`);
    return numeric;
  };
  const total = integer(totalFrames, 'total frame count');
  const startInput = integer(viewStart, 'view start');
  const endInput = integer(viewEnd, 'view end');
  const widthPx = integer(width, 'viewport width');
  const channelCount = integer(channels, 'channel count');
  const fine = integer(fineDivision, 'fine division');
  const maxWindow = integer(maxWindowBytes, 'maximum window bytes');
  let targetPage = integer(targetPageBytes, 'target page bytes');
  const maxRecords = integer(maxTextureRecords, 'texture record limit');
  const enterThreshold = Number(enterPixelsPerPeak);
  const exitThreshold = Number(exitPixelsPerPeak);
  if (!(total > 0) || !(widthPx > 0) || !(channelCount > 0 && channelCount <= 255) || !(fine > 0)) {
    throw new Error('PCM LOD audio/view geometry must be positive and supported');
  }
  if (!(maxWindow > 0) || !(targetPage > 0) || !(maxRecords > 0)) {
    throw new Error('PCM LOD byte/texture limits are invalid');
  }
  targetPage = Math.min(targetPage, maxWindow);
  if (!Number.isFinite(enterThreshold) || !Number.isFinite(exitThreshold)
      || !(exitThreshold > 0) || exitThreshold > enterThreshold) {
    throw new Error('PCM LOD thresholds must satisfy 0 < exit <= enter');
  }
  const start = Math.min(Math.max(0, startInput), total - 1);
  const end = Math.min(Math.max(start + 1, endInput), total);
  const span = Math.max(1, end - start);
  const framesPerPixel = span / widthPx;
  const pixelsPerFinePeak = fine / framesPerPixel;
  const inactive = reason => ({
    active: false,
    mode: null,
    division: 0,
    firstFrame: 0,
    frameCount: 0,
    framesPerPixel,
    pixelsPerFinePeak,
    reason,
    key: null,
  });
  const threshold = sourceActive ? exitThreshold : enterThreshold;
  if (pixelsPerFinePeak < threshold) return inactive('peak-cache density');

  const division = framesPerPixel <= 1
    ? 1
    : Math.min(fine, nextPowerOfTwo(framesPerPixel));
  const mode = division === 1 ? 'samples' : 'envelope';
  const frameBytes = channelCount * 4;
  const guardFrames = Math.max(2, division * 2);
  const neededFirst = Math.floor(Math.max(0, start - guardFrames) / division) * division;
  const neededLast = Math.min(
    total,
    Math.ceil(Math.min(total, end + guardFrames) / division) * division,
  );
  const neededRecords = Math.ceil((neededLast - neededFirst) / division);
  const byteLimitedRecords = Math.floor(maxWindow / Math.max(1, frameBytes * division));
  const capacityRecords = Math.min(maxRecords, byteLimitedRecords);
  if (neededRecords > capacityRecords) {
    return inactive(byteLimitedRecords < neededRecords ? 'source byte budget' : 'texture record limit');
  }
  const targetRecords = Math.max(
    neededRecords,
    Math.min(
      capacityRecords,
      Math.max(
        1,
        Math.floor(targetPage / Math.max(1, frameBytes * division)),
      ),
    ),
  );
  const pageFrames = targetRecords * division;
  const strideFrames = Math.max(division, Math.floor(targetRecords / 2) * division);
  const gridFirst = Math.floor(neededFirst / strideFrames) * strideFrames;
  const minimumFirst = Math.max(0, Math.ceil((neededLast - pageFrames) / division) * division);
  const maximumFirst = neededFirst;
  const firstFrame = Math.min(Math.max(gridFirst, minimumFirst), maximumFirst);
  const lastFrame = Math.min(total, firstFrame + pageFrames);
  const frameCount = Math.max(0, lastFrame - firstFrame);
  const records = Math.ceil(frameCount / division);
  if (!frameCount) return inactive('empty source window');
  if (frameCount * frameBytes > maxWindow) return inactive('source byte budget');
  if (records > maxRecords) return inactive('texture record limit');
  return {
    active: true,
    mode,
    division,
    firstFrame,
    frameCount,
    framesPerPixel,
    pixelsPerFinePeak,
    reason: 'source PCM',
    key: `${firstFrame}:${frameCount}:${division}`,
  };
}

export function planPcmDraw({
  window,
  viewStart,
  viewEnd,
  width,
  pointMinPixelsPerFrame = 3,
  pointRadiusPx = 2.7,
  lineWidthPx = 1.35,
} = {}) {
  const start = Number(viewStart);
  const end = Number(viewEnd);
  const widthPx = Number(width);
  const division = Math.floor(Number(window?.division));
  const records = Math.floor(Number(window?.records));
  const frameCount = Math.floor(Number(window?.frameCount));
  const channels = Math.floor(Number(window?.channels));
  const components = Math.floor(Number(window?.components));
  const firstFrame = Number(window?.firstFrame);
  const mode = window?.mode;
  const pointThreshold = Number(pointMinPixelsPerFrame);
  const pointRadius = Number(pointRadiusPx);
  const lineWidth = Number(lineWidthPx);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    throw new Error('PCM draw view must be finite and non-empty');
  }
  if (!Number.isSafeInteger(widthPx) || widthPx <= 0
      || !Number.isSafeInteger(division) || !(division > 0)
      || !Number.isSafeInteger(records) || !(records > 0)
      || !Number.isSafeInteger(frameCount) || !(frameCount > 0)
      || !Number.isSafeInteger(channels) || !(channels > 0)
      || !Number.isSafeInteger(components)
      || !Number.isSafeInteger(firstFrame) || firstFrame < 0
      || records !== Math.ceil(frameCount / division)) {
    throw new Error('PCM draw window has invalid geometry');
  }
  if ((mode === 'samples' ? components !== 1 : mode === 'envelope' ? components !== 2 : true)) {
    throw new Error('PCM draw mode/component geometry is inconsistent');
  }
  if (mode === 'samples' && division !== 1) {
    throw new Error('exact sample draw mode requires division=1');
  }
  if (!Number.isFinite(pointThreshold) || !(pointThreshold >= 0)
      || !Number.isFinite(pointRadius) || !(pointRadius >= 0)
      || !Number.isFinite(lineWidth) || !(lineWidth > 0)) {
    throw new Error('PCM point/line drawing parameters are invalid');
  }

  const span = end - start;
  const record0 = (start - firstFrame) / division;
  const recordsAcross = span / division;
  const endRecord = record0 + recordsAcross;
  let firstVisible;
  let lastVisible;
  if (mode === 'samples') {
    firstVisible = Math.ceil(record0) - 1;
    lastVisible = Math.floor(endRecord) + 2;
  } else {
    firstVisible = Math.floor(record0);
    lastVisible = Math.floor(endRecord) + 1;
  }
  firstVisible = Math.max(0, Math.min(records, firstVisible));
  lastVisible = Math.max(firstVisible, Math.min(records, lastVisible));
  const pixelsPerFrame = widthPx / span;
  const xStepPx = pixelsPerFrame * division;
  const xOriginPx = (firstFrame + firstVisible * division - start) * pixelsPerFrame;
  const visibleRecordCount = lastVisible - firstVisible;
  return {
    mode,
    record0,
    recordsAcross,
    pixelsPerFrame,
    pixelsPerRecord: xStepPx,
    firstVisibleRecord: firstVisible,
    visibleRecordCount,
    xOriginPx,
    xStepPx,
    drawLines: mode === 'samples' && visibleRecordCount >= 2,
    drawPoints: mode === 'samples' && visibleRecordCount > 0 && pixelsPerFrame >= pointThreshold,
    pointRadiusPx: pointRadius,
    lineWidthPx: lineWidth,
    channels,
    components,
    xForLocalRecord(localRecord) {
      const local = Math.floor(Number(localRecord));
      if (local < 0 || local >= visibleRecordCount) throw new RangeError('PCM local record is outside the draw plan');
      return xOriginPx + local * xStepPx;
    },
    valueOffset(localRecord, channel, component = 0) {
      const local = Math.floor(Number(localRecord));
      const lane = Math.floor(Number(channel));
      const valueComponent = Math.floor(Number(component));
      if (local < 0 || local >= visibleRecordCount) throw new RangeError('PCM local record is outside the draw plan');
      if (lane < 0 || lane >= channels) throw new RangeError('PCM channel is outside the draw plan');
      if (valueComponent < 0 || valueComponent >= components) throw new RangeError('PCM component is outside the draw plan');
      return ((firstVisible + local) * channels + lane) * components + valueComponent;
    },
    sampleOffset(localRecord, channel) {
      if (mode !== 'samples') throw new Error('sampleOffset is only valid for exact samples');
      return this.valueOffset(localRecord, channel, 0);
    },
  };
}

export function nearestLevel(levels, desiredDivision) {
  if (!Array.isArray(levels) || levels.length === 0) return null;
  const desired = Math.max(1, Number(desiredDivision) || 1);
  let best = null;
  let bestDistance = Infinity;
  for (let index = 0; index < levels.length; index++) {
    const level = levels[index];
    const division = Math.max(1, Number(level.division) || 1);
    const distance = Math.abs(Math.log(division / desired));
    if (distance < bestDistance) {
      bestDistance = distance;
      best = {...level, layer_index: Number(level.layer_index ?? index)};
    }
  }
  return best;
}

export function pageRecordsForLayer(
  layer,
  channels,
  maximum = PAGE_RECORDS,
  targetBytes = TARGET_PAGE_BYTES,
) {
  const maxPage = Math.max(64, Number(maximum) | 0);
  const bytesPerChannel = Math.max(1, Number(layer?.bytes_per_channel_record) | 0);
  const bytesPerRecord = Math.max(1, Number(channels) | 0) * bytesPerChannel;
  const budgetRecords = Math.max(1, Math.floor(Math.max(1, Number(targetBytes)) / bytesPerRecord));
  let page = 64;
  while (page * 2 <= maxPage && page * 2 <= budgetRecords) page *= 2;
  return Math.min(maxPage, page);
}

export function pageWindow(viewStart, viewEnd, division, totalRecords, pageRecords = PAGE_RECORDS) {
  const div = Math.max(1, Math.floor(Number(division) || 1));
  const total = Math.max(0, Math.floor(Number(totalRecords) || 0));
  const page = Math.max(1, Math.floor(Number(pageRecords) || PAGE_RECORDS));
  if (total === 0) return {first: 0, count: 0};
  const firstNeeded = Math.max(0, Math.floor(Number(viewStart) / div) - 2);
  const lastNeeded = Math.min(total, Math.ceil(Number(viewEnd) / div) + 3);
  const first = Math.floor(firstNeeded / page) * page;
  const minimumEnd = Math.min(total, first + page);
  const last = Math.min(total, Math.max(minimumEnd, Math.ceil(lastNeeded / page) * page));
  return {first, count: Math.max(1, last - first)};
}

export function textureShape(kind, records, channels, bytesPerChannel) {
  records = Math.max(0, Number(records) | 0);
  channels = Math.max(0, Number(channels) | 0);
  bytesPerChannel = Math.max(0, Number(bytesPerChannel) | 0);
  if (kind === 'waveform' || kind === 'spectral') {
    if (bytesPerChannel !== 4) throw new Error(`unexpected ${kind} record size ${bytesPerChannel}`);
    return {width: channels, height: records, components: 4, type: 'u8'};
  }
  if (kind === 'spectrogram') {
    if (bytesPerChannel !== 192) throw new Error(`unexpected spectrogram record size ${bytesPerChannel}`);
    // `.reapeaks` is time-major, then channel-inner. One texture row is one
    // time record, so this upload is byte-for-byte contiguous and avoids a
    // records*channels texture height that can exceed MAX_TEXTURE_SIZE on 8ch.
    return {width: 192 * channels, height: records, components: 1, type: 'u8'};
  }
  if (kind === 'loudness') {
    if (bytesPerChannel !== 8) throw new Error(`unexpected loudness record size ${bytesPerChannel}`);
    return {width: channels, height: records, components: 2, type: 'f32'};
  }
  throw new Error(`unknown GPU layer kind ${kind}`);
}

export const VERTEX_SHADER = `#version 300 es
precision highp float;
out vec2 v_uv;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    v_uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

export const FRAGMENT_SHADER = `#version 300 es
precision highp float;
precision highp int;
precision highp usampler2D;
in vec2 v_uv;
out vec4 fragColor;

uniform int u_channels;
uniform int u_waveEncoding;
uniform float u_verticalFs;
uniform float u_specGain;
uniform int u_heatmap;
uniform float u_playhead;
uniform float u_nyquist;

uniform int u_hasWave;
uniform usampler2D u_wave;
uniform float u_waveRecord0;
uniform float u_waveRecordsAcross;
uniform int u_waveCount;

uniform int u_hasSpectral;
uniform usampler2D u_spectral;
uniform float u_sRecord0;
uniform float u_sRecordsAcross;
uniform int u_sCount;

uniform int u_hasG;
uniform usampler2D u_g;
uniform float u_gRecord0;
uniform float u_gRecordsAcross;
uniform int u_gCount;

uniform int u_hasLoudness;
uniform sampler2D u_loudness;
uniform float u_rRecord0;
uniform float u_rRecordsAcross;
uniform int u_rCount;

// Dynamic source PCM is independent from the persistent peak pyramid. Mode 1
// is exact source-derived max/min; mode 2 is one texture record per sample.
uniform int u_pcmMode;
uniform sampler2D u_pcm;
uniform float u_pcmRecord0;
uniform float u_pcmRecordsAcross;
uniform int u_pcmCount;
uniform float u_pcmPixelsPerFrame;
uniform int u_pcmDrawPoints;

uniform int u_tileDebug;
uniform vec2 u_viewGlobal;
uniform vec2 u_waveResident;
uniform vec2 u_sResident;
uniform vec2 u_gResident;
uniform vec2 u_rResident;
uniform vec2 u_pcmResident;

int s16(uint lo, uint hi) {
    uint value = lo | (hi << 8u);
    return (value & 0x8000u) != 0u ? int(value) - 65536 : int(value);
}

float decodeWave(int code) {
    if (u_waveEncoding == 0) {
        return float(code) / (code < 0 ? 32768.0 : 32767.0);
    }
    bool neg = code < 0;
    float mag = float(abs(code));
    float amp = mag <= 24576.0
        ? mag / 24576.0
        : exp2((mag - 24576.0) / 1024.0);
    return neg ? -amp : amp;
}

float finitePcm(float value) {
    return value == value && abs(value) < 3.402823e38 ? value : 0.0;
}

int recordAt(float record0, float across, int count) {
    return clamp(int(floor(record0 + v_uv.x * across)), 0, max(0, count - 1));
}

vec3 heat(float t) {
    t = clamp(t, 0.0, 1.0);
    if (u_heatmap == 0) return vec3(t);
    vec3 a = vec3(0.01, 0.01, 0.03);
    vec3 b = vec3(0.05, 0.18, 0.62);
    vec3 c = vec3(0.04, 0.85, 0.92);
    vec3 d = vec3(1.00, 0.86, 0.12);
    vec3 e = vec3(0.92, 0.08, 0.02);
    if (t < 0.25) return mix(a, b, t * 4.0);
    if (t < 0.50) return mix(b, c, (t - 0.25) * 4.0);
    if (t < 0.75) return mix(c, d, (t - 0.50) * 4.0);
    return mix(d, e, (t - 0.75) * 4.0);
}

uint unpackG(int record, int channel, int bin) {
    int pair = bin >> 1;
    int x = channel * 192 + pair * 3;
    uint b0 = texelFetch(u_g, ivec2(x, record), 0).r;
    uint b1 = texelFetch(u_g, ivec2(x + 1, record), 0).r;
    uint b2 = texelFetch(u_g, ivec2(x + 2, record), 0).r;
    return (bin & 1) == 0
        ? ((b0 << 4u) | (b1 >> 4u))
        : ((b2 << 4u) | (b1 & 15u));
}

bool resident(vec2 range, float x) {
    return range.y > range.x && x >= range.x && x <= range.y;
}

vec3 debugRow(int row, float x) {
    vec2 r = row == 0 ? u_waveResident
           : row == 1 ? u_sResident
           : row == 2 ? u_gResident
           : row == 3 ? u_rResident
                      : u_pcmResident;
    vec3 loadedColor = row == 0 ? vec3(0.30, 0.95, 0.56)
                     : row == 1 ? vec3(0.28, 0.62, 1.00)
                     : row == 2 ? vec3(0.95, 0.34, 0.90)
                     : row == 3 ? vec3(1.00, 0.58, 0.18)
                                : vec3(0.98, 0.90, 0.28);
    vec3 base = resident(r, x) ? loadedColor : vec3(0.10, 0.11, 0.14);
    if (x >= u_viewGlobal.x && x <= u_viewGlobal.y) base = mix(base, vec3(1.0), 0.20);
    return base;
}

void main() {
    if (u_tileDebug != 0 && v_uv.y < 0.100) {
        int row = clamp(int(floor(v_uv.y / 0.020)), 0, 4);
        fragColor = vec4(debugRow(row, v_uv.x), 1.0);
        return;
    }

    float lane = (1.0 - clamp(v_uv.y, 0.0, 0.999999)) * float(max(1, u_channels));
    int channel = clamp(int(floor(lane)), 0, max(0, u_channels - 1));
    float localY = fract(lane);
    vec3 color = vec3(0.025, 0.032, 0.045);

    if (u_hasG != 0 && u_gCount > 0) {
        int record = recordAt(u_gRecord0, u_gRecordsAcross, u_gCount);
        int bin = clamp(int(floor((1.0 - localY) * 128.0)), 0, 127);
        float intensity = clamp(float(unpackG(record, channel, bin)) / 4095.0 * u_specGain, 0.0, 1.0);
        color = mix(color, heat(intensity), 0.92);
    }

    if (u_hasSpectral != 0 && u_sCount > 0) {
        int record = recordAt(u_sRecord0, u_sRecordsAcross, u_sCount);
        uvec4 bytes = texelFetch(u_spectral, ivec2(channel, record), 0);
        uint code = bytes.r | (bytes.g << 8u) | (bytes.b << 16u) | (bytes.a << 24u);
        float frequency = float(code & 0x7fffu);
        float density = float((code >> 15u) & 0x3fffu) / 16383.0;
        if (frequency > 0.0) {
            float logLo = log(20.0);
            float logHi = log(max(21.0, u_nyquist));
            float target = 1.0 - clamp((log(max(20.0, frequency)) - logLo) / max(1e-6, logHi - logLo), 0.0, 1.0);
            float alpha = (1.0 - smoothstep(0.002, 0.012, abs(localY - target))) * (0.25 + 0.75 * density);
            color = mix(color, vec3(0.35, 0.72, 1.0), alpha);
        }
    }

    if (u_hasWave != 0 && u_waveCount > 0) {
        int record = recordAt(u_waveRecord0, u_waveRecordsAcross, u_waveCount);
        uvec4 bytes = texelFetch(u_wave, ivec2(channel, record), 0);
        float mx = decodeWave(s16(bytes.r, bytes.g));
        float mn = decodeWave(s16(bytes.b, bytes.a));
        float amplitude = (0.5 - localY) * 2.0 * u_verticalFs;
        float aa = max(fwidth(amplitude) * 1.5, 0.001);
        float inside = smoothstep(mn - aa, mn + aa, amplitude) * (1.0 - smoothstep(mx - aa, mx + aa, amplitude));
        color = mix(color, vec3(0.43, 0.92, 0.67), inside);
    }

    if (u_pcmMode == 1 && u_pcmCount > 0) {
        int record = recordAt(u_pcmRecord0, u_pcmRecordsAcross, u_pcmCount);
        vec2 rawExtrema = texelFetch(u_pcm, ivec2(channel, record), 0).rg;
        vec2 extrema = vec2(finitePcm(rawExtrema.r), finitePcm(rawExtrema.g));
        float amplitude = (0.5 - localY) * 2.0 * u_verticalFs;
        float aa = max(fwidth(amplitude) * 1.5, 0.001);
        float inside = smoothstep(extrema.g - aa, extrema.g + aa, amplitude)
                     * (1.0 - smoothstep(extrema.r - aa, extrema.r + aa, amplitude));
        color = mix(color, vec3(0.98, 0.90, 0.28), inside);
    } else if (u_pcmMode == 2 && u_pcmCount > 0) {
        float position = clamp(
            u_pcmRecord0 + v_uv.x * u_pcmRecordsAcross,
            0.0,
            float(max(0, u_pcmCount - 1))
        );
        int left = clamp(int(floor(position)), 0, max(0, u_pcmCount - 1));
        int right = min(left + 1, u_pcmCount - 1);
        float leftSample = finitePcm(texelFetch(u_pcm, ivec2(channel, left), 0).r);
        float rightSample = finitePcm(texelFetch(u_pcm, ivec2(channel, right), 0).r);
        float lineSample = mix(leftSample, rightSample, fract(position));
        float amplitude = (0.5 - localY) * 2.0 * u_verticalFs;
        float amplitudePerPixel = max(fwidth(amplitude), 1e-6);
        float line = 1.0 - smoothstep(
            amplitudePerPixel * 0.75,
            amplitudePerPixel * 1.75,
            abs(amplitude - lineSample)
        );
        float alpha = line * 0.95;
        if (u_pcmDrawPoints != 0) {
            float nearestPosition = floor(position + 0.5);
            int nearest = clamp(int(nearestPosition), 0, u_pcmCount - 1);
            float pointSample = finitePcm(texelFetch(u_pcm, ivec2(channel, nearest), 0).r);
            float dxPixels = abs(position - nearestPosition) * u_pcmPixelsPerFrame;
            float dyPixels = abs(amplitude - pointSample) / amplitudePerPixel;
            float dot = 1.0 - smoothstep(2.0, 3.0, length(vec2(dxPixels, dyPixels)));
            alpha = max(alpha, dot);
        }
        color = mix(color, vec3(0.98, 0.90, 0.28), alpha);
    }

    if (u_hasLoudness != 0 && u_rCount > 0) {
        int record = recordAt(u_rRecord0, u_rRecordsAcross, u_rCount);
        vec2 energy = texelFetch(u_loudness, ivec2(channel, record), 0).rg;
        float m = -0.691 + 10.0 * log(max(energy.r, 1e-20)) / log(10.0);
        float s = -0.691 + 10.0 * log(max(energy.g, 1e-20)) / log(10.0);
        float my = 1.0 - clamp((m + 70.0) / 70.0, 0.0, 1.0);
        float sy = 1.0 - clamp((s + 70.0) / 70.0, 0.0, 1.0);
        float ma = 1.0 - smoothstep(0.002, 0.010, abs(localY - my));
        float sa = 1.0 - smoothstep(0.002, 0.010, abs(localY - sy));
        color = mix(color, vec3(1.0, 0.62, 0.12), ma * 0.85);
        color = mix(color, vec3(1.0, 0.24, 0.10), sa * 0.85);
    }

    if (u_playhead >= 0.0 && u_playhead <= 1.0) {
        float line = 1.0 - smoothstep(0.0005, 0.0020, abs(v_uv.x - u_playhead));
        color = mix(color, vec3(1.0, 0.78, 0.20), line);
    }
    fragColor = vec4(color, 1.0);
}`;

function compileShader(gl, type, source) {
  const shader = gl.createShader(type);
  if (!shader) throw new Error('WebGL2 shader allocation failed');
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader) || 'unknown shader error';
    gl.deleteShader(shader);
    throw new Error(log);
  }
  return shader;
}

function linkProgram(gl) {
  const vertex = compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
  const fragment = compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
  const program = gl.createProgram();
  if (!program) throw new Error('WebGL2 program allocation failed');
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  gl.deleteShader(vertex);
  gl.deleteShader(fragment);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const log = gl.getProgramInfoLog(program) || 'unknown link error';
    gl.deleteProgram(program);
    throw new Error(log);
  }
  return program;
}

function isLittleEndian() {
  const word = new Uint16Array([1]);
  return new Uint8Array(word.buffer)[0] === 1;
}

function abortableDelay(milliseconds, signal) {
  if (!(milliseconds > 0)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', aborted);
      resolve();
    }, milliseconds);
    const aborted = () => {
      clearTimeout(timer);
      const error = new Error('source PCM request aborted');
      error.name = 'AbortError';
      reject(error);
    };
    if (signal?.aborted) aborted();
    else signal?.addEventListener('abort', aborted, {once: true});
  });
}

export class WebGL2PackedRenderer {
  constructor(
    canvas,
    meta,
    {pageRecords = PAGE_RECORDS, pcmFetchDebounceMs = 45} = {},
  ) {
    this.canvas = canvas;
    this.meta = meta;
    this.pageRecords = Math.max(64, Number(pageRecords) | 0);
    this.pcmFetchDebounceMs = Math.max(0, Number(pcmFetchDebounceMs) || 0);
    this.gl = canvas.getContext('webgl2', {
      alpha: false,
      antialias: false,
      depth: false,
      stencil: false,
      preserveDrawingBuffer: false,
      powerPreference: 'high-performance',
    });
    if (!this.gl) throw new Error('WebGL2 is not available in this browser');
    this.maxTextureSize = Number(this.gl.getParameter(this.gl.MAX_TEXTURE_SIZE));
    this.program = linkProgram(this.gl);
    this.vao = this.gl.createVertexArray();
    if (!this.vao) throw new Error('WebGL2 VAO allocation failed');
    this.gl.bindVertexArray(this.vao);
    this.gl.useProgram(this.program);
    this.gl.pixelStorei(this.gl.UNPACK_ALIGNMENT, 1);
    this.uniforms = new Map();
    this.uploads = new Map();
    this.abortController = null;
    this.timerExt = this.gl.getExtension('EXT_disjoint_timer_query_webgl2');
    this.pendingQuery = null;
    this.gpuMs = 0;
    this.cpuSubmitMs = 0;
    this.lastFetchMs = 0;
    this.lastUploadMs = 0;
    this.networkBytes = 0;
    this.uploadBytes = 0;
    this.uploadCount = 0;
    this.frameCount = 0;
    this.lastError = '';
    this.lastPcmError = '';
    this.sourceActive = false;
    this.currentPcmPlan = null;
    this.pcmUpload = null;
    this.pcmCacheHit = false;
    this.pcmCacheDisposition = 'none';
    this.pcmServerCacheBytes = 0;
    this.pcmRangeAccessCount = 0;
    this.pcmRangeDecodeCount = 0;
    this.lastPcmRangeEvent = null;
    this.littleEndian = isLittleEndian();
    this.lastParams = null;
  }

  uniform(name) {
    if (!this.uniforms.has(name)) this.uniforms.set(name, this.gl.getUniformLocation(this.program, name));
    return this.uniforms.get(name);
  }

  resize() {
    const dpr = Math.min(2, Math.max(1, Number(globalThis.devicePixelRatio) || 1));
    const width = Math.max(1, Math.round(this.canvas.clientWidth * dpr));
    const height = Math.max(1, Math.round(this.canvas.clientHeight * dpr));
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    this.gl.viewport(0, 0, width, height);
    return {width, height};
  }

  layerChoice(kind, desiredDivision) {
    return nearestLevel(this.meta.gpu_layers?.[kind] || [], desiredDivision);
  }

  async fetchWindow(kind, choice, viewStart, viewEnd, signal) {
    if (!choice) return null;
    const pageRecords = pageRecordsForLayer(choice, this.meta.channels, this.pageRecords);
    const window = pageWindow(viewStart, viewEnd, choice.division, choice.record_count, pageRecords);
    if (!window.count) return null;
    const key = `${choice.layer_index}:${window.first}:${window.count}`;
    const current = this.uploads.get(kind);
    if (current?.key === key) return current;
    const query = new URLSearchParams({
      kind,
      layer: String(choice.layer_index),
      first: String(window.first),
      count: String(window.count),
    });
    const fetchStart = performance.now();
    const response = await fetch(`/api/gpu-records?${query}`, {signal});
    if (!response.ok) throw new Error(`${kind} GPU records: HTTP ${response.status}`);
    const raw = new Uint8Array(await response.arrayBuffer());
    const fetchEnd = performance.now();
    const first = Number(response.headers.get('X-First-Record'));
    const records = Number(response.headers.get('X-Record-Count'));
    const channels = Number(response.headers.get('X-Channels'));
    const bytesPerChannel = Number(response.headers.get('X-Bytes-Per-Channel-Record'));
    const division = Number(response.headers.get('X-Division'));
    const uploadStart = performance.now();
    const texture = this.uploadTexture(kind, records, channels, bytesPerChannel, raw);
    const uploadEnd = performance.now();
    if (current?.texture) this.gl.deleteTexture(current.texture);
    const uploaded = {key, kind, layerIndex: choice.layer_index, division, first, records, channels, bytesPerChannel, texture, byteCount: raw.byteLength};
    this.uploads.set(kind, uploaded);
    this.lastFetchMs = fetchEnd - fetchStart;
    this.lastUploadMs = uploadEnd - uploadStart;
    this.networkBytes += raw.byteLength;
    this.uploadBytes += raw.byteLength;
    this.uploadCount++;
    return uploaded;
  }

  uploadTexture(kind, records, channels, bytesPerChannel, raw) {
    const gl = this.gl;
    const shape = textureShape(kind, records, channels, bytesPerChannel);
    if (shape.width > this.maxTextureSize || shape.height > this.maxTextureSize) {
      throw new Error(
        `${kind} texture ${shape.width}x${shape.height} exceeds WebGL2 MAX_TEXTURE_SIZE=${this.maxTextureSize}`,
      );
    }
    const texture = gl.createTexture();
    if (!texture) throw new Error(`cannot create ${kind} WebGL2 texture`);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    if (kind === 'waveform' || kind === 'spectral') {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8UI, shape.width, shape.height, 0, gl.RGBA_INTEGER, gl.UNSIGNED_BYTE, raw);
    } else if (kind === 'spectrogram') {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8UI, shape.width, shape.height, 0, gl.RED_INTEGER, gl.UNSIGNED_BYTE, raw);
    } else if (kind === 'loudness') {
      let floats;
      if (this.littleEndian && raw.byteOffset % 4 === 0) {
        floats = new Float32Array(raw.buffer, raw.byteOffset, raw.byteLength / 4);
      } else {
        floats = new Float32Array(raw.byteLength / 4);
        const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
        for (let i = 0; i < floats.length; i++) floats[i] = view.getFloat32(i * 4, true);
      }
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG32F, shape.width, shape.height, 0, gl.RG, gl.FLOAT, floats);
    }
    gl.bindTexture(gl.TEXTURE_2D, null);
    return texture;
  }

  f32View(raw) {
    if (raw.byteLength % 4) throw new Error('PCM float payload is not 32-bit aligned');
    if (this.littleEndian && raw.byteOffset % 4 === 0) {
      return new Float32Array(raw.buffer, raw.byteOffset, raw.byteLength / 4);
    }
    const floats = new Float32Array(raw.byteLength / 4);
    const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
    for (let index = 0; index < floats.length; index++) {
      floats[index] = view.getFloat32(index * 4, true);
    }
    return floats;
  }

  uploadPcmTexture(records, channels, components, raw) {
    const gl = this.gl;
    if (records > this.maxTextureSize || channels > this.maxTextureSize) {
      throw new Error(
        `source PCM texture ${channels}x${records} exceeds WebGL2 MAX_TEXTURE_SIZE=${this.maxTextureSize}`,
      );
    }
    if (components !== 1 && components !== 2) {
      throw new Error(`unexpected source PCM component count ${components}`);
    }
    const expected = records * channels * components * 4;
    if (raw.byteLength !== expected) {
      throw new Error(`source PCM payload ${raw.byteLength} bytes != expected ${expected}`);
    }
    const texture = gl.createTexture();
    if (!texture) throw new Error('cannot create source PCM WebGL2 texture');
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const floats = this.f32View(raw);
    if (components === 1) {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, channels, records, 0, gl.RED, gl.FLOAT, floats);
    } else {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG32F, channels, records, 0, gl.RG, gl.FLOAT, floats);
    }
    gl.bindTexture(gl.TEXTURE_2D, null);
    return texture;
  }

  notifyPcmRangeEvent(detail) {
    this.lastPcmRangeEvent = Object.freeze({...detail});
    this.pcmRangeAccessCount++;
    if (detail.readerRan) this.pcmRangeDecodeCount++;
    this.canvas.dispatchEvent(new CustomEvent('libreapeaks:pcm-range', {
      detail: this.lastPcmRangeEvent,
    }));
  }

  async fetchPcmWindow(plan, signal) {
    if (!plan?.active) return null;
    if (this.pcmUpload?.key === plan.key) return this.pcmUpload;
    // Wheel/pan bursts should not turn client-side cancellation into a burst of
    // already-running FFmpeg processes on the local server.
    await abortableDelay(this.pcmFetchDebounceMs, signal);
    const query = new URLSearchParams({
      first: String(plan.firstFrame),
      count: String(plan.frameCount),
      division: String(plan.division),
    });
    const fetchStart = performance.now();
    const response = await fetch(`/api/pcm-window?${query}`, {signal});
    if (!response.ok) {
      let detail = '';
      try { detail = (await response.json()).error || ''; } catch (_error) { /* binary endpoint */ }
      throw new Error(`source PCM: HTTP ${response.status}${detail ? ` (${detail})` : ''}`);
    }
    const raw = new Uint8Array(await response.arrayBuffer());
    const fetchEnd = performance.now();
    const numberHeader = name => {
      const value = response.headers.get(name);
      if (value === null || value.trim() === '') {
        throw new Error(`source PCM response is missing ${name}`);
      }
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) {
        throw new Error(`invalid source PCM ${name}=${value}`);
      }
      return parsed;
    };
    const integerHeader = name => {
      const value = numberHeader(name);
      if (!Number.isSafeInteger(value) || value < 0) {
        throw new Error(`invalid non-negative integer PCM ${name}=${value}`);
      }
      return value;
    };
    const firstFrame = integerHeader('X-Pcm-First-Frame');
    const frameCount = integerHeader('X-Pcm-Frame-Count');
    const division = integerHeader('X-Pcm-Division');
    const records = integerHeader('X-Pcm-Record-Count');
    const channels = integerHeader('X-Pcm-Channels');
    const components = integerHeader('X-Pcm-Components');
    const mode = response.headers.get('X-Pcm-Mode') || (components === 1 ? 'samples' : 'envelope');
    if (
      firstFrame !== plan.firstFrame
      || frameCount <= 0
      || frameCount > plan.frameCount
      || division !== plan.division
      || records <= 0
      || channels <= 0
      || channels !== Number(this.meta.channels)
      || records !== Math.ceil(frameCount / Math.max(1, division))
      || (mode !== 'samples' && mode !== 'envelope')
      || (mode === 'samples' ? components !== 1 || division !== 1 : components !== 2)
    ) {
      throw new Error('source PCM response geometry does not match its request');
    }
    const rawFirstFrame = integerHeader('X-Pcm-Raw-First-Frame');
    const rawFrameCount = integerHeader('X-Pcm-Raw-Frame-Count');
    const rangeEventId = integerHeader('X-Pcm-Range-Event-Id');
    const occurredUnixMs = integerHeader('X-Pcm-Range-Event-Unix-Ms');
    const readerMs = numberHeader('X-Pcm-Range-Reader-Ms');
    const payloadBytes = integerHeader('X-Pcm-Payload-Bytes');
    const serverCacheBytes = integerHeader('X-Pcm-Cache-Bytes');
    const cacheDisposition = response.headers.get('X-Pcm-Cache-Disposition') || 'unknown';
    if (!['decoded', 'cache-hit', 'coalesced'].includes(cacheDisposition)) {
      throw new Error(`invalid PCM cache disposition ${cacheDisposition}`);
    }
    const semanticReaderRan = response.headers.get('X-Pcm-Range-Reader-Ran');
    const aliasReaderRan = response.headers.get('X-Pcm-Range-Decode-Ran');
    const readerRanHeader = semanticReaderRan ?? aliasReaderRan;
    if (!['0', '1'].includes(readerRanHeader)
        || (semanticReaderRan !== null && aliasReaderRan !== null && semanticReaderRan !== aliasReaderRan)) {
      throw new Error('invalid or conflicting PCM range-reader flag');
    }
    const readerRan = readerRanHeader === '1';
    const rawCacheHitHeader = response.headers.get('X-Pcm-Raw-Cache-Hit');
    if (!['0', '1'].includes(rawCacheHitHeader)) {
      throw new Error('invalid PCM raw-cache-hit flag');
    }
    const rawCacheHit = rawCacheHitHeader === '1';
    const expectedBytes = records * channels * components * 4;
    if (
      rangeEventId <= 0
      || occurredUnixMs <= 0
      || readerMs < 0
      || rawFrameCount <= 0
      || rawFirstFrame > firstFrame
      || rawFirstFrame + rawFrameCount < firstFrame + frameCount
      || payloadBytes !== raw.byteLength
      || payloadBytes !== expectedBytes
      || rawCacheHit !== (cacheDisposition === 'cache-hit')
      || readerRan !== (cacheDisposition === 'decoded')
    ) {
      throw new Error('source PCM range diagnostics or payload are inconsistent');
    }
    const backend = response.headers.get('X-Pcm-Backend') || 'unknown';
    if (backend.length > 256 || !/^[\x20-\x7e]+$/.test(backend)) {
      throw new Error('invalid source PCM backend label');
    }
    this.notifyPcmRangeEvent({
      eventId: rangeEventId,
      occurredUnixMs,
      requestFirstFrame: plan.firstFrame,
      requestFrameCount: plan.frameCount,
      rawFirstFrame,
      rawFrameCount,
      displayFirstFrame: firstFrame,
      displayFrameCount: frameCount,
      displayRecordCount: records,
      division,
      mode,
      backend,
      cacheDisposition,
      readerRan,
      readerMs,
    });
    const uploadStart = performance.now();
    const texture = this.uploadPcmTexture(records, channels, components, raw);
    const uploadEnd = performance.now();
    if (this.pcmUpload?.texture) this.gl.deleteTexture(this.pcmUpload.texture);
    this.pcmUpload = {
      key: plan.key,
      firstFrame,
      frameCount,
      division,
      records,
      channels,
      components,
      mode,
      backend,
      texture,
      byteCount: raw.byteLength,
    };
    this.pcmCacheHit = rawCacheHit;
    this.pcmCacheDisposition = cacheDisposition;
    this.pcmServerCacheBytes = serverCacheBytes;
    this.lastFetchMs = fetchEnd - fetchStart;
    this.lastUploadMs = uploadEnd - uploadStart;
    this.networkBytes += raw.byteLength;
    this.uploadBytes += raw.byteLength;
    this.uploadCount++;
    return this.pcmUpload;
  }

  setWindowUniforms(hasName, shortName, upload, viewStart, viewEnd) {
    const gl = this.gl;
    if (!upload) {
      gl.uniform1i(this.uniform(`u_has${hasName}`), 0);
      gl.uniform1i(this.uniform(`u_${shortName}Count`), 0);
      gl.uniform1f(this.uniform(`u_${shortName}Record0`), 0);
      gl.uniform1f(this.uniform(`u_${shortName}RecordsAcross`), 0);
      return;
    }
    gl.uniform1i(this.uniform(`u_has${hasName}`), 1);
    gl.uniform1f(this.uniform(`u_${shortName}Record0`), viewStart / Math.max(1, upload.division) - upload.first);
    gl.uniform1f(this.uniform(`u_${shortName}RecordsAcross`), (viewEnd - viewStart) / Math.max(1, upload.division));
    gl.uniform1i(this.uniform(`u_${shortName}Count`), upload.records);
  }

  residentRange(kind, totalFrames) {
    const upload = this.uploads.get(kind);
    if (!upload) return [0, 0];
    const total = Math.max(1, totalFrames);
    return [
      Math.max(0, upload.first * upload.division / total),
      Math.min(1, (upload.first + upload.records) * upload.division / total),
    ];
  }

  pcmResidentRange(totalFrames) {
    const upload = this.pcmUpload;
    if (!upload) return [0, 0];
    const total = Math.max(1, totalFrames);
    return [
      Math.max(0, upload.firstFrame / total),
      Math.min(1, (upload.firstFrame + upload.frameCount) / total),
    ];
  }

  pollGpuTimer() {
    const gl = this.gl;
    if (!this.pendingQuery || !this.timerExt) return;
    const available = gl.getQueryParameter(this.pendingQuery, gl.QUERY_RESULT_AVAILABLE);
    const disjoint = gl.getParameter(this.timerExt.GPU_DISJOINT_EXT);
    if (!available) return;
    if (!disjoint) this.gpuMs = Number(gl.getQueryParameter(this.pendingQuery, gl.QUERY_RESULT)) / 1e6;
    gl.deleteQuery(this.pendingQuery);
    this.pendingQuery = null;
  }

  paint(params) {
    this.lastParams = {...params};
    const gl = this.gl;
    this.resize();
    this.pollGpuTimer();
    gl.useProgram(this.program);
    gl.bindVertexArray(this.vao);
    gl.clearColor(0.02, 0.025, 0.035, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);

    const span = Math.max(1, params.viewEnd - params.viewStart);
    const total = Math.max(1, params.totalFrames);
    gl.uniform1i(this.uniform('u_channels'), Number(this.meta.channels));
    gl.uniform1i(this.uniform('u_waveEncoding'), this.meta.wave_encoding === 'RPKN' ? 0 : 1);
    gl.uniform1f(this.uniform('u_verticalFs'), Number(params.verticalScale));
    gl.uniform1f(this.uniform('u_specGain'), Number(params.spectrogramGain));
    gl.uniform1i(this.uniform('u_heatmap'), params.heatmap ? 1 : 0);
    gl.uniform1f(this.uniform('u_nyquist'), Number(this.meta.sample_rate) * 0.5);
    gl.uniform1f(this.uniform('u_playhead'), (params.playhead - params.viewStart) / span);
    gl.uniform1i(this.uniform('u_tileDebug'), params.tileDebug ? 1 : 0);
    gl.uniform2f(this.uniform('u_viewGlobal'), params.viewStart / total, params.viewEnd / total);

    const residentUniforms = [
      ['waveform', 'u_waveResident'], ['spectral', 'u_sResident'],
      ['spectrogram', 'u_gResident'], ['loudness', 'u_rResident'],
    ];
    for (const [kind, name] of residentUniforms) {
      const [lo, hi] = this.residentRange(kind, total);
      gl.uniform2f(this.uniform(name), lo, hi);
    }
    const [pcmLo, pcmHi] = this.pcmResidentRange(total);
    gl.uniform2f(this.uniform('u_pcmResident'), pcmLo, pcmHi);

    const bindings = [
      ['waveform', 'u_wave', 0], ['spectral', 'u_spectral', 1],
      ['spectrogram', 'u_g', 2], ['loudness', 'u_loudness', 3],
    ];
    for (const [kind, name, unit] of bindings) {
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, this.uploads.get(kind)?.texture || null);
      gl.uniform1i(this.uniform(name), unit);
    }

    gl.activeTexture(gl.TEXTURE4);
    gl.bindTexture(gl.TEXTURE_2D, this.pcmUpload?.texture || null);
    gl.uniform1i(this.uniform('u_pcm'), 4);

    const usePcm = Boolean(
      this.sourceActive
      && this.currentPcmPlan?.active
      && this.pcmUpload?.key === this.currentPcmPlan.key,
    );
    const pcmDraw = usePcm ? planPcmDraw({
      window: this.pcmUpload,
      viewStart: params.viewStart,
      viewEnd: params.viewEnd,
      width: this.canvas.width,
    }) : null;

    this.setWindowUniforms('Wave', 'wave', usePcm ? null : this.uploads.get('waveform'), params.viewStart, params.viewEnd);
    this.setWindowUniforms('Spectral', 's', usePcm ? null : (params.showSpectral ? this.uploads.get('spectral') : null), params.viewStart, params.viewEnd);
    this.setWindowUniforms('G', 'g', usePcm ? null : this.uploads.get('spectrogram'), params.viewStart, params.viewEnd);
    this.setWindowUniforms('Loudness', 'r', usePcm ? null : (params.showLoudness ? this.uploads.get('loudness') : null), params.viewStart, params.viewEnd);
    if (usePcm && pcmDraw) {
      const upload = this.pcmUpload;
      gl.uniform1i(this.uniform('u_pcmMode'), upload.mode === 'samples' ? 2 : 1);
      gl.uniform1f(this.uniform('u_pcmRecord0'), pcmDraw.record0);
      gl.uniform1f(this.uniform('u_pcmRecordsAcross'), pcmDraw.recordsAcross);
      gl.uniform1i(this.uniform('u_pcmCount'), upload.records);
      gl.uniform1f(this.uniform('u_pcmPixelsPerFrame'), pcmDraw.pixelsPerFrame);
      gl.uniform1i(this.uniform('u_pcmDrawPoints'), pcmDraw.drawPoints ? 1 : 0);
    } else {
      gl.uniform1i(this.uniform('u_pcmMode'), 0);
      gl.uniform1f(this.uniform('u_pcmRecord0'), 0);
      gl.uniform1f(this.uniform('u_pcmRecordsAcross'), 0);
      gl.uniform1i(this.uniform('u_pcmCount'), 0);
      gl.uniform1f(this.uniform('u_pcmPixelsPerFrame'), 0);
      gl.uniform1i(this.uniform('u_pcmDrawPoints'), 0);
    }

    const cpuStart = performance.now();
    let query = null;
    if (this.timerExt && !this.pendingQuery) {
      query = gl.createQuery();
      if (query) gl.beginQuery(this.timerExt.TIME_ELAPSED_EXT, query);
    }
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    if (query) {
      gl.endQuery(this.timerExt.TIME_ELAPSED_EXT);
      this.pendingQuery = query;
    }
    this.cpuSubmitMs = performance.now() - cpuStart;
    this.frameCount++;
    gl.bindVertexArray(null);
  }

  async render(params) {
    this.abortController?.abort();
    const controller = new AbortController();
    this.abortController = controller;
    this.resize();
    const desired = Math.max(1e-9, (params.viewEnd - params.viewStart) / Math.max(1, this.canvas.clientWidth));
    const divisions = (this.meta.gpu_layers?.waveform || [])
      .map(layer => Math.max(1, Number(layer.division) || 1));
    divisions.push(Math.max(1, Number(this.meta.levels?.[0]?.division) || 1));
    const fineDivision = Math.min(...divisions);
    const sourceAvailable = this.meta.source_pcm?.available === true;
    const lod = this.meta.source_lod || {};
    const pcm = this.meta.source_pcm || {};
    const sourcePlan = sourceAvailable ? planPcmLod({
      viewStart: params.viewStart,
      viewEnd: params.viewEnd,
      width: this.canvas.width,
      totalFrames: params.totalFrames,
      channels: this.meta.channels,
      fineDivision,
      sourceActive: this.sourceActive,
      enterPixelsPerPeak: lod.enter_pixels_per_fine_peak ?? PCM_ENTER_PIXELS_PER_PEAK,
      exitPixelsPerPeak: lod.exit_pixels_per_fine_peak ?? PCM_EXIT_PIXELS_PER_PEAK,
      maxWindowBytes: pcm.max_window_bytes,
      targetPageBytes: pcm.target_page_bytes,
      maxTextureRecords: Math.min(PCM_MAX_TEXTURE_RECORDS, this.maxTextureSize),
    }) : {active: false, reason: pcm.error || 'source PCM unavailable', key: null};
    this.currentPcmPlan = sourcePlan;
    this.sourceActive = sourcePlan.active;

    if (sourcePlan.active) {
      try {
        await this.fetchPcmWindow(sourcePlan, controller.signal);
        if (controller.signal.aborted) return false;
        this.paint(params);
        this.lastPcmError = '';
        this.lastError = '';
        if (this.abortController === controller) this.abortController = null;
        return true;
      } catch (error) {
        if (error?.name === 'AbortError') return false;
        // Source PCM is an enhancement. Preserve an interactive waveform when
        // a codec seek fails and expose the failure in diagnostics.
        this.lastPcmError = String(error);
        this.sourceActive = false;
      }
    }
    const waveChoice = this.layerChoice('waveform', desired);
    const targetDivision = waveChoice?.division || desired;
    const spectralChoice = params.showSpectral ? this.layerChoice('spectral', targetDivision) : null;
    const gChoice = this.layerChoice('spectrogram', targetDivision);
    const rChoice = params.showLoudness ? this.layerChoice('loudness', targetDivision) : null;
    try {
      await Promise.all([
        this.fetchWindow('waveform', waveChoice, params.viewStart, params.viewEnd, controller.signal),
        this.fetchWindow('spectral', spectralChoice, params.viewStart, params.viewEnd, controller.signal),
        this.fetchWindow('spectrogram', gChoice, params.viewStart, params.viewEnd, controller.signal),
        this.fetchWindow('loudness', rChoice, params.viewStart, params.viewEnd, controller.signal),
      ]);
      if (controller.signal.aborted) return false;
      this.paint(params);
      this.lastError = '';
      return true;
    } catch (error) {
      if (error?.name === 'AbortError') return false;
      this.lastError = String(error);
      throw error;
    } finally {
      if (this.abortController === controller) this.abortController = null;
    }
  }

  diagnostics() {
    const resident = [...this.uploads.entries()].map(([kind, u]) => `${kind}:${u.layerIndex}@${u.first}+${u.records}`).join(', ');
    const mib = this.uploadBytes / (1024 * 1024);
    const timer = this.timerExt ? `${this.gpuMs.toFixed(3)}ms` : 'n/a';
    const pcm = this.sourceActive && this.pcmUpload
      ? `PCM ${this.pcmUpload.mode} ${this.pcmUpload.backend}@${this.pcmUpload.firstFrame}+${this.pcmUpload.frameCount} div=${this.pcmUpload.division} cache=${this.pcmCacheDisposition} ranges=${this.pcmRangeAccessCount}/decoded=${this.pcmRangeDecodeCount} server=${(this.pcmServerCacheBytes / 1048576).toFixed(2)}MiB`
      : `reapeaks${this.lastPcmError ? ` (PCM fallback: ${this.lastPcmError})` : ''}`;
    return `WebGL2 packed ${pcm} | cpu=${this.cpuSubmitMs.toFixed(3)}ms gpu=${timer} | last fetch=${this.lastFetchMs.toFixed(3)}ms upload=${this.lastUploadMs.toFixed(3)}ms | uploads=${this.uploadCount} ${mib.toFixed(2)}MiB | maxTex=${this.maxTextureSize} | resident [${resident || 'none'}]`;
  }

  dispose() {
    this.abortController?.abort();
    const gl = this.gl;
    for (const upload of this.uploads.values()) if (upload.texture) gl.deleteTexture(upload.texture);
    this.uploads.clear();
    if (this.pcmUpload?.texture) gl.deleteTexture(this.pcmUpload.texture);
    this.pcmUpload = null;
    if (this.pendingQuery) gl.deleteQuery(this.pendingQuery);
    gl.deleteVertexArray(this.vao);
    gl.deleteProgram(this.program);
  }
}
