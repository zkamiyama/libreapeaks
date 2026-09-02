"""REAPER-style interaction wrapper for the direct GLSL analysis canvas.

The wrapper is the public demo surface. It normalizes two binding/runtime
quirks before the renderer is used: one Mesa-reserved GLSL identifier and
PySide6 6.11's scalar-uniform overload resolution by resolving names to integer
uniform locations and dispatching scalar values through the explicit
``setUniformValue1f``/``setUniformValue1i`` APIs.

It also layers DAW-style display controls on top of the exact packed cache
bytes: an exclusive analysis view mode plus spectrogram gain in dB,
floor/ceiling range, contrast, and a linear/log frequency-axis switch. These
are shader-only display transforms and never rewrite the cache.
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
    "uniform int u_heatmap;",
)
_shader = _replace_required(
    _shader,
    "bool resident(vec2 range, float x) {",
    "float sampleG(float recordPosition, int channel, float binPosition) {\n"
    "    float record = clamp(recordPosition, 0.0, float(max(0, u_gCount - 1)));\n"
    "    float bin = clamp(binPosition, 0.0, 127.0);\n"
    "    int r0 = int(floor(record));\n"
    "    int r1 = min(r0 + 1, max(0, u_gCount - 1));\n"
    "    int b0 = int(floor(bin));\n"
    "    int b1 = min(b0 + 1, 127);\n"
    "    float rt = fract(record);\n"
    "    float bt = fract(bin);\n"
    "    float lo = mix(float(unpackG(r0, channel, b0)), float(unpackG(r1, channel, b0)), rt);\n"
    "    float hi = mix(float(unpackG(r0, channel, b1)), float(unpackG(r1, channel, b1)), rt);\n"
    "    return mix(lo, hi, bt);\n"
    "}\n\n"
    "bool resident(vec2 range, float x) {",
)
_shader = _replace_required(
    _shader,
    "    if (u_hasG != 0 && u_gCount > 0) {\n"
    "        int record = recordAt(u_gRecord0, u_gRecordsAcross, u_gCount);\n"
    "        int bin = clamp(int(floor((1.0 - localY) * 128.0)), 0, 127);\n"
    "        float intensity = clamp(\n"
    "            float(unpackG(record, channel, bin)) / 4095.0 * u_specGain,\n"
    "            0.0,\n"
    "            1.0\n"
    "        );",
    "    if (u_displayMode == 2 && u_hasG != 0 && u_gCount > 0) {\n"
    "        float record = clamp(\n"
    "            u_gRecord0 + v_uv.x * u_gRecordsAcross,\n"
    "            0.0,\n"
    "            float(max(0, u_gCount - 1))\n"
    "        );\n"
    "        float bin;\n"
    "        if (u_specFreqLog != 0) {\n"
    "            float minFreq = max(20.0, u_nyquist / 128.0);\n"
    "            float frequency = exp(mix(log(minFreq), log(max(minFreq + 1.0, u_nyquist)), 1.0 - localY));\n"
    "            bin = clamp(frequency * 128.0 / max(1.0, u_nyquist) - 0.5, 0.0, 127.0);\n"
    "        } else {\n"
    "            bin = clamp((1.0 - localY) * 128.0 - 0.5, 0.0, 127.0);\n"
    "        }\n"
    "        float code = sampleG(record, channel, bin);\n"
    "        float db = (code - 4095.5) * (10.0 / (88.92179516969081 * log(10.0))) + u_specGainDb;\n"
    "        float lo = min(u_specFloorDb, u_specCeilingDb - 0.001);\n"
    "        float hi = max(u_specCeilingDb, lo + 0.001);\n"
    "        float normalized = clamp((db - lo) / (hi - lo), 0.0, 1.0);\n"
    "        float intensity = clamp(pow(normalized, max(0.05, u_specContrast)) * u_specGain, 0.0, 1.0);",
)
_shader = _replace_required(
    _shader,
    "    if (u_hasSpectral != 0 && u_sCount > 0) {",
    "    if (u_displayMode == 1 && u_hasSpectral != 0 && u_sCount > 0) {",
)
_shader = _replace_required(
    _shader,
    "    if (u_hasWave != 0 && u_waveCount > 0) {",
    "    if (u_displayMode == 0 && u_hasWave != 0 && u_waveCount > 0) {",
)
_shader = _replace_required(
    _shader,
    "        float aa = max(fwidth(amplitude) * 1.5, 0.001);\n"
    "        float inside = smoothstep(mn - aa, mn + aa, amplitude)\n"
    "                     * (1.0 - smoothstep(mx - aa, mx + aa, amplitude));",
    "        float lower = amplitude - mn;\n"
    "        float upper = mx - amplitude;\n"
    "        float lowerAa = max(fwidth(lower) * 0.85, 0.001);\n"
    "        float upperAa = max(fwidth(upper) * 0.85, 0.001);\n"
    "        float inside = smoothstep(-lowerAa, lowerAa, lower)\n"
    "                     * smoothstep(-upperAa, upperAa, upper);",
)
_shader = _replace_required(
    _shader,
    "    if (u_pcmMode == 1 && u_pcmCount > 0) {",
    "    if (u_displayMode == 0 && u_pcmMode == 1 && u_pcmCount > 0) {",
)
_shader = _replace_required(
    _shader,
    "        float aa = max(fwidth(amplitude) * 1.5, 0.001);\n"
    "        float inside = smoothstep(extrema.g - aa, extrema.g + aa, amplitude)\n"
    "                     * (1.0 - smoothstep(extrema.r - aa, extrema.r + aa, amplitude));",
    "        float lower = amplitude - extrema.g;\n"
    "        float upper = extrema.r - amplitude;\n"
    "        float lowerAa = max(fwidth(lower) * 0.85, 0.001);\n"
    "        float upperAa = max(fwidth(upper) * 0.85, 0.001);\n"
    "        float inside = smoothstep(-lowerAa, lowerAa, lower)\n"
    "                     * smoothstep(-upperAa, upperAa, upper);",
)
_shader = _replace_required(
    _shader,
    "    } else if (u_pcmMode == 2 && u_pcmCount > 0) {",
    "    } else if (u_displayMode == 0 && u_pcmMode == 2 && u_pcmCount > 0) {",
)
_shader = _replace_required(
    _shader,
    "        float amplitudePerPixel = max(fwidth(amplitude), 1e-6);\n"
    "        float line = 1.0 - smoothstep(\n"
    "            amplitudePerPixel * 0.75,\n"
    "            amplitudePerPixel * 1.75,\n"
    "            abs(amplitude - lineSample)\n"
    "        );",
    "        float amplitudePerPixel = max(fwidth(amplitude), 1e-6);\n"
    "        float lineDistance = amplitude - lineSample;\n"
    "        float lineAa = max(fwidth(lineDistance), amplitudePerPixel);\n"
    "        float line = 1.0 - smoothstep(\n"
    "            lineAa * 0.35,\n"
    "            lineAa * 1.15,\n"
    "            abs(lineDistance)\n"
    "        );",
)
_shader = _replace_required(
    _shader,
    "    if (u_hasLoudness != 0 && u_rCount > 0) {",
    "    if (u_displayMode == 3 && u_hasLoudness != 0 && u_rCount > 0) {",
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
            self._set_optional_float("u_specGainDb", self._owner.spectrogram_gain_db)
            self._set_optional_float("u_specFloorDb", self._owner.spectrogram_floor_db)
            self._set_optional_float("u_specCeilingDb", self._owner.spectrogram_ceiling_db)
            self._set_optional_float("u_specContrast", self._owner.spectrogram_contrast)
            self._set_optional_int("u_specFreqLog", self._owner.spectrogram_frequency_log)
            self._set_optional_int("u_displayMode", self._owner.display_mode_index)
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
        self.display_mode = "waveform"
        self.display_mode_index = self.DISPLAY_MODES[self.display_mode]
        self.show_spectral = False
        self.show_loudness = False

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

    def set_spectrogram_gain_db(self, value: float):
        self.spectrogram_gain_db = max(-60.0, min(60.0, float(value)))
        self.update()

    def set_spectrogram_floor_db(self, value: float):
        self.spectrogram_floor_db = max(-200.0, min(-0.001, float(value)))
        if self.spectrogram_ceiling_db <= self.spectrogram_floor_db:
            self.spectrogram_ceiling_db = min(24.0, self.spectrogram_floor_db + 1.0)
        self.update()

    def set_spectrogram_ceiling_db(self, value: float):
        self.spectrogram_ceiling_db = max(-199.0, min(24.0, float(value)))
        if self.spectrogram_ceiling_db <= self.spectrogram_floor_db:
            self.spectrogram_floor_db = max(-200.0, self.spectrogram_ceiling_db - 1.0)
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