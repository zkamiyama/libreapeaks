"""REAPER-style interaction wrapper for the direct GLSL analysis canvas.

The wrapper is the public demo surface. It normalizes two binding/runtime
quirks before the renderer is used: one Mesa-reserved GLSL identifier and
PySide6 6.11's scalar-uniform overload resolution by resolving names to integer
uniform locations and dispatching scalar values through the explicit
``setUniformValue1f``/``setUniformValue1i`` APIs.

It also layers DAW-style spectrogram display transforms on top of the exact
packed `-'g'` bytes: display gain in dB, floor/ceiling range, contrast, and a
linear/log frequency-axis switch. These are shader-only transforms and never
rewrite the cache.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal

import pyside6_gl_view as _gl

_gl.FRAGMENT_SHADER = _gl.FRAGMENT_SHADER.replace(
    "vec3 active =",
    "vec3 loadedColor =",
).replace(
    "? active :",
    "? loadedColor :",
).replace(
    "uniform float u_specGain;\nuniform int u_heatmap;",
    "uniform float u_specGain;\n"
    "uniform float u_specGainDb;\n"
    "uniform float u_specFloorDb;\n"
    "uniform float u_specCeilingDb;\n"
    "uniform float u_specContrast;\n"
    "uniform int u_specFreqLog;\n"
    "uniform int u_heatmap;",
).replace(
    "        int bin = clamp(int(floor((1.0 - localY) * 128.0)), 0, 127);\n"
    "        float intensity = clamp(\n"
    "            float(unpackG(record, channel, bin)) / 4095.0 * u_specGain,\n"
    "            0.0,\n"
    "            1.0\n"
    "        );",
    "        int bin;\n"
    "        if (u_specFreqLog != 0) {\n"
    "            float minFreq = max(20.0, u_nyquist / 128.0);\n"
    "            float frequency = exp(mix(log(minFreq), log(max(minFreq + 1.0, u_nyquist)), 1.0 - localY));\n"
    "            bin = clamp(int(floor(frequency * 128.0 / max(1.0, u_nyquist))) - 1, 0, 127);\n"
    "        } else {\n"
    "            bin = clamp(int(floor((1.0 - localY) * 128.0)), 0, 127);\n"
    "        }\n"
    "        float code = float(unpackG(record, channel, bin));\n"
    "        float db = (code - 4095.5) * (10.0 / (88.92179516969081 * log(10.0))) + u_specGainDb;\n"
    "        float lo = min(u_specFloorDb, u_specCeilingDb - 0.001);\n"
    "        float hi = max(u_specCeilingDb, lo + 0.001);\n"
    "        float normalized = clamp((db - lo) / (hi - lo), 0.0, 1.0);\n"
    "        float intensity = clamp(pow(normalized, max(0.05, u_specContrast)) * u_specGain, 0.0, 1.0);",
)
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spectrogram_gain_db = 0.0
        self.spectrogram_floor_db = -100.0
        self.spectrogram_ceiling_db = 0.0
        self.spectrogram_contrast = 1.0
        self.spectrogram_frequency_log = True

    def paintGL(self):  # noqa: N802 - Qt API
        program = self._program
        if program is None:
            return super().paintGL()
        self._program = _UniformNameProxy(program, self)
        try:
            return super().paintGL()
        finally:
            self._program = program

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
