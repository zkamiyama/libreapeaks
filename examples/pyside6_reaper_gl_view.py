"""REAPER-style interaction wrapper for the direct GLSL analysis canvas.

The wrapper keeps the packed `.reapeaks` renderer byte-preserving while adding
REAPER-like interaction, explicit analysis display modes, calibrated
spectrogram controls, stable waveform LOD, waveform-colored spectral peaks,
and loudness peak/graph views.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal

import pyside6_gl_view as _gl


def _replace_required(source: str, old: str, new: str) -> str:
    if old not in source:
        raise RuntimeError("pyside6_gl_view shader layout changed")
    return source.replace(old, new, 1)


_shader = _gl.FRAGMENT_SHADER
_shader = _replace_required(
    _shader,
    "vec3 active =",
    "vec3 loadedColor =",
)
_shader = _replace_required(
    _shader,
    "? active :",
    "? loadedColor :",
)
_shader = _replace_required(
    _shader,
    "uniform float u_specGain;\nuniform int u_heatmap;",
    "uniform float u_specGain;\n"
    "uniform float u_specGainDb;\n"
    "uniform float u_specFloorDb;\n"
    "uniform float u_specCeilingDb;\n"
    "uniform float u_specContrast;\n"
    "uniform int u_specFreqLog;\n"
    "uniform int u_displayMode;\n"
    "uniform float u_peakDisplayGain;\n"
    "uniform float u_analysisOpacity;\n"
    "uniform float u_spectralLowHz;\n"
    "uniform float u_spectralHighHz;\n"
    "uniform int u_spectralRangeMode;\n"
    "uniform int u_spectralReverse;\n"
    "uniform int u_spectralFadeNoise;\n"
    "uniform int u_loudnessMetric;\n"
    "uniform int u_loudnessView;\n"
    "uniform float u_loudnessFloorLu;\n"
    "uniform float u_loudnessCeilingLu;\n"
    "uniform float u_loudnessOffsetLu;\n"
    "uniform float u_loudnessTransitionLu;\n"
    "uniform int u_heatmap;",
)
_shader = _replace_required(
    _shader,
    "bool resident(vec2 range, float x) {",
    """float sampleG(float recordPosition, int channel, float binPosition) {
    float record = clamp(recordPosition, 0.0, float(max(0, u_gCount - 1)));
    float bin = clamp(binPosition, 0.0, 127.0);
    int r0 = int(floor(record));
    int r1 = min(r0 + 1, max(0, u_gCount - 1));
    int b0 = int(floor(bin));
    int b1 = min(b0 + 1, 127);
    float rt = fract(record);
    float bt = fract(bin);
    float lo = mix(float(unpackG(r0, channel, b0)), float(unpackG(r1, channel, b0)), rt);
    float hi = mix(float(unpackG(r0, channel, b1)), float(unpackG(r1, channel, b1)), rt);
    return mix(lo, hi, bt);
}

void wavePixelExtrema(
    float centerRecord,
    float recordsPerPixel,
    int channel,
    out float mx,
    out float mn
) {
    int maxRecord = max(0, u_waveCount - 1);
    float halfFootprint = max(recordsPerPixel * 0.5, 0.00001);
    float leftEdge = centerRecord - halfFootprint;
    float rightEdge = centerRecord + halfFootprint;
    int firstRecord = clamp(int(floor(leftEdge)), 0, maxRecord);
    int lastRecord = clamp(int(floor(rightEdge - 0.00001)), 0, maxRecord);
    lastRecord = max(firstRecord, lastRecord);

    uvec4 firstBytes = texelFetch(u_wave, ivec2(channel, firstRecord), 0);
    mx = decodeWave(s16(firstBytes.r, firstBytes.g));
    mn = decodeWave(s16(firstBytes.b, firstBytes.a));

    // REAPER's normal three-level cache ratios stay well below this bound.
    // The fixed cap keeps the GLSL loop predictable even for custom divisions.
    for (int offset = 1; offset < 64; ++offset) {
        int record = firstRecord + offset;
        if (record > lastRecord) break;
        uvec4 bytes = texelFetch(u_wave, ivec2(channel, record), 0);
        mx = max(mx, decodeWave(s16(bytes.r, bytes.g)));
        mn = min(mn, decodeWave(s16(bytes.b, bytes.a)));
    }
}

float segmentDistancePx(vec2 pointPx, vec2 aPx, vec2 bPx) {
    vec2 ab = bPx - aPx;
    float denom = dot(ab, ab);
    if (denom <= 1e-12) {
        return length(pointPx - aPx);
    }
    float t = clamp(dot(pointPx - aPx, ab) / denom, 0.0, 1.0);
    return length(pointPx - (aPx + t * ab));
}

float prefilteredCoverage(float distancePx, float halfWidthPx) {
    const float filterRadiusPx = 0.5;
    return clamp(
        (halfWidthPx + filterRadiusPx - distancePx) / (2.0 * filterRadiusPx),
        0.0,
        1.0
    );
}

float pcmNeighborSegmentDistancePx(
    float position,
    float amplitude,
    float samplesPerPixel,
    float amplitudePerPixel,
    int channel
) {
    int maxRecord = max(0, u_pcmCount - 1);
    int base = int(floor(position));
    float best = 1e20;
    for (int offset = -1; offset <= 1; ++offset) {
        int ia = clamp(base + offset, 0, maxRecord);
        int ib = clamp(base + offset + 1, 0, maxRecord);
        float sa = finitePcm(texelFetch(u_pcm, ivec2(channel, ia), 0).r);
        float sb = finitePcm(texelFetch(u_pcm, ivec2(channel, ib), 0).r);
        vec2 aPx = vec2(
            (float(ia) - position) / samplesPerPixel,
            (sa - amplitude) / amplitudePerPixel
        );
        vec2 bPx = vec2(
            (float(ib) - position) / samplesPerPixel,
            (sb - amplitude) / amplitudePerPixel
        );
        best = min(best, segmentDistancePx(vec2(0.0), aPx, bPx));
    }
    return best;
}

vec3 hsv2rgb(vec3 c) {
    vec3 p = abs(fract(c.xxx + vec3(0.0, 2.0 / 3.0, 1.0 / 3.0)) * 6.0 - 3.0);
    return c.z * mix(vec3(1.0), clamp(p - 1.0, 0.0, 1.0), c.y);
}

uint spectralCode(int record, int channel) {
    uvec4 bytes = texelFetch(u_spectral, ivec2(channel, record), 0);
    return bytes.r | (bytes.g << 8u) | (bytes.b << 16u) | (bytes.a << 24u);
}

vec3 spectralCodeColor(uint code) {
    const vec3 normalPeak = vec3(0.43, 0.92, 0.67);
    float frequency = float(code & 0x7fffu);
    float tonality = float((code >> 15u) & 0x3fffu) / 16383.0;
    if (frequency <= 0.0) return normalPeak;

    float low = max(10.0, u_spectralLowHz);
    float high = min(max(low + 1.0, u_spectralHighHz), max(low + 1.0, u_nyquist));
    float f = clamp(frequency, low, high);
    float phase;
    if (u_spectralRangeMode != 0) {
        phase = fract(log(f / low) / log(2.0));
    } else {
        phase = clamp(log(f / low) / max(1e-6, log(high / low)), 0.0, 1.0);
    }
    if (u_spectralReverse != 0) phase = 1.0 - phase;

    // Red -> yellow -> green -> cyan -> blue -> violet, close to REAPER's
    // frequency-colored waveform concept rather than a frequency-vs-Y trace.
    vec3 hueColor = hsv2rgb(vec3(phase * 0.78, 0.92, 1.0));
    float tonalMix = pow(clamp(tonality, 0.0, 1.0), 0.65);
    vec3 neutral = u_spectralFadeNoise != 0 ? normalPeak : vec3(0.58);
    return mix(neutral, hueColor, tonalMix);
}

vec3 spectralColorAt(float recordPosition, int channel) {
    float position = clamp(recordPosition, 0.0, float(max(0, u_sCount - 1)));
    int r0 = int(floor(position));
    int r1 = min(r0 + 1, max(0, u_sCount - 1));
    float t = fract(position);
    return mix(
        spectralCodeColor(spectralCode(r0, channel)),
        spectralCodeColor(spectralCode(r1, channel)),
        t
    );
}

vec3 loudnessColor(float lu) {
    float transition = max(0.05, u_loudnessTransitionLu);
    vec3 c = vec3(0.18, 0.48, 0.32);
    c = mix(c, vec3(0.18, 0.72, 0.33), smoothstep(-42.0 - transition, -42.0 + transition, lu));
    c = mix(c, vec3(0.47, 0.82, 0.24), smoothstep(-36.0 - transition, -36.0 + transition, lu));
    c = mix(c, vec3(0.92, 0.86, 0.18), smoothstep(-30.0 - transition, -30.0 + transition, lu));
    c = mix(c, vec3(1.00, 0.62, 0.10), smoothstep(-24.0 - transition, -24.0 + transition, lu));
    c = mix(c, vec3(0.96, 0.24, 0.10), smoothstep(-18.0 - transition, -18.0 + transition, lu));
    c = mix(c, vec3(0.88, 0.14, 0.34), smoothstep(-12.0 - transition, -12.0 + transition, lu));
    c = mix(c, vec3(0.34, 0.42, 1.00), smoothstep(-6.0 - transition, -6.0 + transition, lu));
    return c;
}

bool resident(vec2 range, float x) {""",
)
_shader = _replace_required(
    _shader,
    """    if (u_hasG != 0 && u_gCount > 0) {
        int record = recordAt(u_gRecord0, u_gRecordsAcross, u_gCount);
        int bin = clamp(int(floor((1.0 - localY) * 128.0)), 0, 127);
        float intensity = clamp(
            float(unpackG(record, channel, bin)) / 4095.0 * u_specGain,
            0.0,
            1.0
        );""",
    """    if (u_displayMode == 2 && u_hasG != 0 && u_gCount > 0) {
        float record = clamp(
            u_gRecord0 + v_uv.x * u_gRecordsAcross,
            0.0,
            float(max(0, u_gCount - 1))
        );
        float bin;
        if (u_specFreqLog != 0) {
            float minFreq = max(20.0, u_nyquist / 128.0);
            float frequency = exp(mix(log(minFreq), log(max(minFreq + 1.0, u_nyquist)), 1.0 - localY));
            bin = clamp(frequency * 128.0 / max(1.0, u_nyquist) - 0.5, 0.0, 127.0);
        } else {
            bin = clamp((1.0 - localY) * 128.0 - 0.5, 0.0, 127.0);
        }
        float code = sampleG(record, channel, bin);
        float db = (code - 4095.5) * (10.0 / (88.92179516969081 * log(10.0))) + u_specGainDb;
        float lo = min(u_specFloorDb, u_specCeilingDb - 0.001);
        float hi = max(u_specCeilingDb, lo + 0.001);
        float normalized = clamp((db - lo) / (hi - lo), 0.0, 1.0);
        float intensity = clamp(pow(normalized, max(0.05, u_specContrast)) * u_specGain, 0.0, 1.0);""",
)
_shader = _replace_required(
    _shader,
    """    if (u_hasSpectral != 0 && u_sCount > 0) {
        int record = recordAt(u_sRecord0, u_sRecordsAcross, u_sCount);
        uvec4 bytes = texelFetch(u_spectral, ivec2(channel, record), 0);
        uint code = bytes.r | (bytes.g << 8u) | (bytes.b << 16u) | (bytes.a << 24u);
        float frequency = float(code & 0x7fffu);
        float density = float((code >> 15u) & 0x3fffu) / 16383.0;
        if (frequency > 0.0) {
            float logLo = log(20.0);
            float logHi = log(max(21.0, u_nyquist));
            float target = 1.0 - clamp(
                (log(max(20.0, frequency)) - logLo) / max(1e-6, logHi - logLo),
                0.0,
                1.0
            );
            float alpha = (1.0 - smoothstep(0.002, 0.012, abs(localY - target)))
                        * (0.25 + 0.75 * density);
            color = mix(color, vec3(0.35, 0.72, 1.0), alpha);
        }
    }""",
    """    if (
        u_displayMode == 1
        && u_hasSpectral != 0
        && u_sCount > 0
        && u_hasWave != 0
        && u_waveCount > 0
    ) {
        float wavePosition = u_waveRecord0 + v_uv.x * u_waveRecordsAcross;
        float recordsPerPixel = max(abs(dFdx(wavePosition)), 0.00001);
        float mx;
        float mn;
        wavePixelExtrema(wavePosition, recordsPerPixel, channel, mx, mn);
        mx *= u_peakDisplayGain;
        mn *= u_peakDisplayGain;

        float amplitude = (0.5 - localY) * 2.0 * u_verticalFs;
        float lower = amplitude - mn;
        float upper = mx - amplitude;
        float lowerAa = max(fwidth(lower) * 0.85, 0.001);
        float upperAa = max(fwidth(upper) * 0.85, 0.001);
        float inside = smoothstep(-lowerAa, lowerAa, lower)
                     * smoothstep(-upperAa, upperAa, upper);

        float spectralPosition = u_sRecord0 + v_uv.x * u_sRecordsAcross;
        vec3 peakColor = spectralColorAt(spectralPosition, channel);
        color = mix(color, peakColor, inside * u_analysisOpacity);
    }""",
)
_shader = _replace_required(
    _shader,
    """    if (u_hasWave != 0 && u_waveCount > 0) {
        int record = recordAt(u_waveRecord0, u_waveRecordsAcross, u_waveCount);
        uvec4 bytes = texelFetch(u_wave, ivec2(channel, record), 0);
        float mx = decodeWave(s16(bytes.r, bytes.g));
        float mn = decodeWave(s16(bytes.b, bytes.a));
        float amplitude = (0.5 - localY) * 2.0 * u_verticalFs;
        float aa = max(fwidth(amplitude) * 1.5, 0.001);
        float inside = smoothstep(mn - aa, mn + aa, amplitude)
                     * (1.0 - smoothstep(mx - aa, mx + aa, amplitude));
        color = mix(color, vec3(0.43, 0.92, 0.67), inside);
    }""",
    """    if (u_displayMode == 0 && u_hasWave != 0 && u_waveCount > 0) {
        float recordPosition = u_waveRecord0 + v_uv.x * u_waveRecordsAcross;
        float recordsPerPixel = max(abs(dFdx(recordPosition)), 0.00001);
        float mx;
        float mn;
        wavePixelExtrema(recordPosition, recordsPerPixel, channel, mx, mn);

        float amplitude = (0.5 - localY) * 2.0 * u_verticalFs;
        float lower = amplitude - mn;
        float upper = mx - amplitude;
        float lowerAa = max(fwidth(lower) * 0.85, 0.001);
        float upperAa = max(fwidth(upper) * 0.85, 0.001);
        float inside = smoothstep(-lowerAa, lowerAa, lower)
                     * smoothstep(-upperAa, upperAa, upper);
        color = mix(color, vec3(0.43, 0.92, 0.67), inside);
    }""",
)
_shader = _replace_required(
    _shader,
    "    if (u_pcmMode == 1 && u_pcmCount > 0) {",
    "    if (u_displayMode == 0 && u_pcmMode == 1 && u_pcmCount > 0) {",
)
_shader = _replace_required(
    _shader,
    """        float aa = max(fwidth(amplitude) * 1.5, 0.001);
        float inside = smoothstep(extrema.g - aa, extrema.g + aa, amplitude)
                     * (1.0 - smoothstep(extrema.r - aa, extrema.r + aa, amplitude));""",
    """        float lower = amplitude - extrema.g;
        float upper = extrema.r - amplitude;
        float lowerAa = max(fwidth(lower) * 0.85, 0.001);
        float upperAa = max(fwidth(upper) * 0.85, 0.001);
        float inside = smoothstep(-lowerAa, lowerAa, lower)
                     * smoothstep(-upperAa, upperAa, upper);""",
)
_shader = _replace_required(
    _shader,
    "    } else if (u_pcmMode == 2 && u_pcmCount > 0) {",
    "    } else if (u_displayMode == 0 && u_pcmMode == 2 && u_pcmCount > 0) {",
)
_shader = _replace_required(
    _shader,
    """        float amplitudePerPixel = max(fwidth(amplitude), 1e-6);
        float line = 1.0 - smoothstep(
            amplitudePerPixel * 0.75,
            amplitudePerPixel * 1.75,
            abs(amplitude - lineSample)
        );""",
    """        float amplitudePerPixel = max(abs(dFdy(amplitude)), 1e-6);
        float samplesPerPixel = max(abs(dFdx(position)), 1e-6);
        float pixelsPerFrame = 1.0 / samplesPerPixel;
        float distancePx = pcmNeighborSegmentDistancePx(
            position,
            amplitude,
            samplesPerPixel,
            amplitudePerPixel,
            channel
        );
        float line = prefilteredCoverage(distancePx, 0.55);""",
)
_shader = _replace_required(
    _shader,
    """        if (u_pcmDrawPoints != 0) {
            float nearestPosition = floor(position + 0.5);
            int nearest = clamp(int(nearestPosition), 0, u_pcmCount - 1);
            float pointSample = finitePcm(texelFetch(u_pcm, ivec2(channel, nearest), 0).r);
            float dxPixels = abs(position - nearestPosition) * u_pcmPixelsPerFrame;
            float dyPixels = abs(amplitude - pointSample) / amplitudePerPixel;
            float dot = 1.0 - smoothstep(2.0, 3.0, length(vec2(dxPixels, dyPixels)));
            alpha = max(alpha, dot);
        }""",
    """        if (u_pcmDrawPoints != 0) {
            float nearestPosition = floor(position + 0.5);
            int nearest = clamp(int(nearestPosition), 0, u_pcmCount - 1);
            float pointSample = finitePcm(texelFetch(u_pcm, ivec2(channel, nearest), 0).r);
            vec2 pointDeltaPx = vec2(
                (nearestPosition - position) / samplesPerPixel,
                (pointSample - amplitude) / amplitudePerPixel
            );
            float pointFade = smoothstep(8.0, 11.0, pixelsPerFrame);
            float dot = prefilteredCoverage(length(pointDeltaPx), 1.65) * pointFade;
            alpha = max(alpha, dot);
        }""",
)
_shader = _replace_required(
    _shader,
    """    if (u_hasLoudness != 0 && u_rCount > 0) {
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
    }""",
    """    if (
        u_displayMode == 3
        && u_hasLoudness != 0
        && u_rCount > 0
        && u_hasWave != 0
        && u_waveCount > 0
    ) {
        float wavePosition = u_waveRecord0 + v_uv.x * u_waveRecordsAcross;
        float recordsPerPixel = max(abs(dFdx(wavePosition)), 0.00001);
        float mx;
        float mn;
        wavePixelExtrema(wavePosition, recordsPerPixel, channel, mx, mn);
        mx *= u_peakDisplayGain;
        mn *= u_peakDisplayGain;
        float amplitude = (0.5 - localY) * 2.0 * u_verticalFs;
        float lower = amplitude - mn;
        float upper = mx - amplitude;
        float lowerAa = max(fwidth(lower) * 0.85, 0.001);
        float upperAa = max(fwidth(upper) * 0.85, 0.001);
        float inside = smoothstep(-lowerAa, lowerAa, lower)
                     * smoothstep(-upperAa, upperAa, upper);

        int record = recordAt(u_rRecord0, u_rRecordsAcross, u_rCount);
        vec2 energy = texelFetch(u_loudness, ivec2(channel, record), 0).rg;
        float selectedEnergy = u_loudnessMetric == 0 ? energy.r : energy.g;
        float lu = -0.691 + 10.0 * log(max(selectedEnergy, 1e-20)) / log(10.0);
        lu += u_loudnessOffsetLu;
        vec3 luColor = loudnessColor(lu);

        if (u_loudnessView == 0) {
            color = mix(color, luColor, inside * u_analysisOpacity);
        } else {
            // REAPER's "normal peaks + LUFS graph" is much easier to read than
            // two unrelated M/S traces: retain the waveform and overlay one
            // selected, band-colored loudness graph.
            color = mix(color, vec3(0.43, 0.92, 0.67), inside * 0.72);
            float lo = min(u_loudnessFloorLu, u_loudnessCeilingLu - 0.001);
            float hi = max(u_loudnessCeilingLu, lo + 0.001);
            float target = 1.0 - clamp((lu - lo) / (hi - lo), 0.0, 1.0);
            float aa = max(fwidth(localY), 0.0005);
            float graph = 1.0 - smoothstep(aa * 0.7, aa * 2.2, abs(localY - target));
            color = mix(color, luColor, graph * u_analysisOpacity);
        }
    }""",
)
_gl.FRAGMENT_SHADER = _shader
GpuAnalysisCanvas = _gl.GpuAnalysisCanvas


class _UniformNameProxy:
    """Delegate QOpenGLShaderProgram with stable scalar uniform dispatch."""

    def __init__(self, program, owner):
        self._program = program
        self._owner = owner
        self._locations = {}

    def _location(self, name) -> int:
        if isinstance(name, str):
            key = name.encode("ascii")
        elif isinstance(name, (bytes, bytearray, memoryview)):
            key = bytes(name)
        else:
            return int(name)
        location = self._locations.get(key)
        if location is None:
            location = int(self._program.uniformLocation(key))
            self._locations[key] = location
        return location

    def _set_scalar(self, location: int, value):
        if isinstance(value, bool):
            return self._program.setUniformValue1i(location, int(value))
        if isinstance(value, int):
            return self._program.setUniformValue1i(location, value)
        if isinstance(value, float):
            return self._program.setUniformValue1f(location, value)
        return self._program.setUniformValue(location, value)

    def _set_optional_float(self, name: str, value: float) -> None:
        location = self._location(name)
        if location >= 0:
            self._program.setUniformValue1f(location, float(value))

    def _set_optional_int(self, name: str, value: int) -> None:
        location = self._location(name)
        if location >= 0:
            self._program.setUniformValue1i(location, int(value))

    def setUniformValue(self, name, *values):  # noqa: N802 - Qt API
        location = self._location(name)
        if len(values) == 1:
            result = self._set_scalar(location, values[0])
        else:
            result = self._program.setUniformValue(location, *values)
        key = name.decode("ascii") if isinstance(name, bytes) else name
        if key == "u_specGain":
            owner = self._owner
            self._set_optional_float("u_specGainDb", owner.spectrogram_gain_db)
            self._set_optional_float("u_specFloorDb", owner.spectrogram_floor_db)
            self._set_optional_float("u_specCeilingDb", owner.spectrogram_ceiling_db)
            self._set_optional_float("u_specContrast", owner.spectrogram_contrast)
            self._set_optional_int("u_specFreqLog", owner.spectrogram_frequency_log)
            self._set_optional_int("u_displayMode", owner.display_mode_index)
            self._set_optional_float("u_peakDisplayGain", owner.peak_display_gain)
            self._set_optional_float("u_analysisOpacity", owner.analysis_opacity)
            self._set_optional_float("u_spectralLowHz", owner.spectral_low_hz)
            self._set_optional_float("u_spectralHighHz", owner.spectral_high_hz)
            self._set_optional_int("u_spectralRangeMode", owner.spectral_range_mode)
            self._set_optional_int("u_spectralReverse", owner.spectral_reverse)
            self._set_optional_int("u_spectralFadeNoise", owner.spectral_fade_noise)
            self._set_optional_int("u_loudnessMetric", owner.loudness_metric)
            self._set_optional_int("u_loudnessView", owner.loudness_view)
            self._set_optional_float("u_loudnessFloorLu", owner.loudness_floor_lu)
            self._set_optional_float("u_loudnessCeilingLu", owner.loudness_ceiling_lu)
            self._set_optional_float("u_loudnessOffsetLu", owner.loudness_offset_lu)
            self._set_optional_float(
                "u_loudnessTransitionLu", owner.loudness_transition_lu
            )
        return result

    def __getattr__(self, name):
        return getattr(self._program, name)


def wheel_steps(event) -> float:
    delta = event.angleDelta().y()
    if delta:
        return delta / 120.0
    pixel_delta = event.pixelDelta().y()
    if pixel_delta:
        return pixel_delta / 120.0
    return 0.0


class ReaperGpuAnalysisCanvas(GpuAnalysisCanvas):
    """Packed GLSL canvas with REAPER-like interaction and DAW display controls."""

    verticalScaleChanged = Signal(float)
    displayModeChanged = Signal(str)
    DISPLAY_MODES = {
        "waveform": 0,
        "spectral": 1,
        "spectrogram": 2,
        "loudness": 3,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spectrogram_gain_db = 0.0
        self.spectrogram_floor_db = -100.0
        self.spectrogram_ceiling_db = 0.0
        self.spectrogram_contrast = 1.0
        self.spectrogram_frequency_log = True

        self.peak_display_zoom_db = 0.0
        self.peak_display_gain = 1.0
        self.analysis_opacity = 0.92

        self.spectral_low_hz = 20.0
        self.spectral_high_hz = 10000.0
        self.spectral_range_mode = 0
        self.spectral_reverse = 0
        self.spectral_fade_noise = 1

        self.loudness_metric = 0
        self.loudness_view = 1
        self.loudness_floor_lu = -48.0
        self.loudness_ceiling_lu = 0.0
        self.loudness_offset_lu = 0.0
        self.loudness_transition_lu = 1.5

        self.display_mode = "waveform"
        self.display_mode_index = self.DISPLAY_MODES[self.display_mode]
        self.show_spectral = False
        self.show_loudness = False

    @staticmethod
    def _nearest_level(levels, desired_division: float):
        """Choose the coarsest cache level that is no coarser than one pixel.

        A nearest-in-log-space choice switches to the next coarse mip too early:
        one peak bucket can then cover several screen pixels and appears as a
        rectangular block. Staying on the finest level needed for the current
        pixel footprint lets the shader union every max/min bucket touched by
        that pixel, preserving transients while zooming out.
        """
        if not levels:
            return None
        desired = max(1.0, float(desired_division))
        indexed = list(enumerate(levels))
        eligible = [
            item for item in indexed if max(1.0, float(item[1][0])) <= desired
        ]
        if eligible:
            return max(eligible, key=lambda item: float(item[1][0]))
        return min(indexed, key=lambda item: float(item[1][0]))

    def paintGL(self):  # noqa: N802 - Qt API
        program = self._program
        if program is None:
            return super().paintGL()
        self._program = _UniformNameProxy(program, self)
        try:
            return super().paintGL()
        finally:
            self._program = program

    def _source_upload_for_view(self):
        if self.display_mode != "waveform":
            return None
        return super()._source_upload_for_view()

    def set_display_mode(self, mode: str):
        normalized = str(mode).strip().lower()
        if normalized not in self.DISPLAY_MODES:
            raise ValueError(f"unknown display mode: {mode}")
        changed = normalized != self.display_mode
        self.display_mode = normalized
        self.display_mode_index = self.DISPLAY_MODES[normalized]
        self.show_spectral = normalized == "spectral"
        self.show_loudness = normalized == "loudness"
        if changed:
            self.displayModeChanged.emit(normalized)
        self.update()

    def set_spectral_overlay(self, enabled: bool):
        if enabled:
            self.set_display_mode("spectral")
        elif self.display_mode == "spectral":
            self.set_display_mode("waveform")

    def set_loudness_overlay(self, enabled: bool):
        if enabled:
            self.set_display_mode("loudness")
        elif self.display_mode == "loudness":
            self.set_display_mode("waveform")

    def set_vertical_full_scale(self, value: float):
        self.vertical_full_scale = max(0.1, min(32.0, float(value)))
        self.update()

    def set_peak_display_zoom_db(self, value: float):
        self.peak_display_zoom_db = max(-24.0, min(24.0, float(value)))
        self.peak_display_gain = 10.0 ** (self.peak_display_zoom_db / 20.0)
        self.update()

    def set_analysis_opacity(self, value: float):
        self.analysis_opacity = max(0.0, min(1.0, float(value)))
        self.update()

    def set_spectral_low_hz(self, value: float):
        self.spectral_low_hz = max(10.0, min(20000.0, float(value)))
        if self.spectral_high_hz <= self.spectral_low_hz:
            self.spectral_high_hz = self.spectral_low_hz + 1.0
        self.update()

    def set_spectral_high_hz(self, value: float):
        self.spectral_high_hz = max(20.0, min(30000.0, float(value)))
        if self.spectral_high_hz <= self.spectral_low_hz:
            self.spectral_low_hz = max(10.0, self.spectral_high_hz - 1.0)
        self.update()

    def set_spectral_range_mode(self, mode: int):
        self.spectral_range_mode = 1 if int(mode) else 0
        self.update()

    def set_spectral_reverse(self, enabled: bool):
        self.spectral_reverse = 1 if enabled else 0
        self.update()

    def set_spectral_fade_noise(self, enabled: bool):
        self.spectral_fade_noise = 1 if enabled else 0
        self.update()

    def set_loudness_metric(self, metric: int):
        self.loudness_metric = 1 if int(metric) else 0
        self.update()

    def set_loudness_view(self, view: int):
        self.loudness_view = 1 if int(view) else 0
        self.update()

    def set_loudness_floor_lu(self, value: float):
        self.loudness_floor_lu = max(-70.0, min(-0.1, float(value)))
        if self.loudness_ceiling_lu <= self.loudness_floor_lu:
            self.loudness_ceiling_lu = min(6.0, self.loudness_floor_lu + 1.0)
        self.update()

    def set_loudness_ceiling_lu(self, value: float):
        self.loudness_ceiling_lu = max(-69.0, min(6.0, float(value)))
        if self.loudness_ceiling_lu <= self.loudness_floor_lu:
            self.loudness_floor_lu = max(-70.0, self.loudness_ceiling_lu - 1.0)
        self.update()

    def set_loudness_offset_lu(self, value: float):
        self.loudness_offset_lu = max(-24.0, min(24.0, float(value)))
        self.update()

    def set_loudness_transition_lu(self, value: float):
        self.loudness_transition_lu = max(0.05, min(12.0, float(value)))
        self.update()

    def set_spectrogram_gain_db(self, value: float):
        self.spectrogram_gain_db = max(-60.0, min(60.0, float(value)))
        self.update()

    def set_spectrogram_floor_db(self, value: float):
        self.spectrogram_floor_db = max(-200.0, min(-0.001, float(value)))
        if self.spectrogram_ceiling_db <= self.spectrogram_floor_db:
            self.spectrogram_ceiling_db = min(
                24.0, self.spectrogram_floor_db + 1.0
            )
        self.update()

    def set_spectrogram_ceiling_db(self, value: float):
        self.spectrogram_ceiling_db = max(-199.0, min(24.0, float(value)))
        if self.spectrogram_ceiling_db <= self.spectrogram_floor_db:
            self.spectrogram_floor_db = max(
                -200.0, self.spectrogram_ceiling_db - 1.0
            )
        self.update()

    def set_spectrogram_contrast(self, value: float):
        self.spectrogram_contrast = max(0.05, min(8.0, float(value)))
        self.update()

    def set_spectrogram_frequency_log(self, enabled: bool):
        self.spectrogram_frequency_log = bool(enabled)
        self.update()

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        steps = wheel_steps(event)
        if steps == 0.0:
            event.ignore()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            value = self.vertical_full_scale * (1.15 ** (-steps))
            self.set_vertical_full_scale(value)
            self.verticalScaleChanged.emit(self.vertical_full_scale)
        else:
            anchor = min(
                1.0,
                max(0.0, event.position().x() / max(1, self.width())),
            )
            self.zoom(0.72**steps, anchor)
        event.accept()
