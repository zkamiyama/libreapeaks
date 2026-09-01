"""REAPER-style interaction wrapper for the direct GLSL analysis canvas.

The wrapper is the public demo surface. It normalizes two binding/runtime
quirks before the renderer is used: one Mesa-reserved GLSL identifier and
PySide6 6.11's scalar-uniform overload resolution by resolving names to integer
uniform locations before calling ``setUniformValue``.
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
)
GpuAnalysisCanvas = _gl.GpuAnalysisCanvas


class _UniformNameProxy:
    """Delegate QOpenGLShaderProgram with stable scalar uniform dispatch."""

    def __init__(self, program):
        self._program = program
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

    def setUniformValue(self, name, *values):  # noqa: N802 - Qt API
        return self._program.setUniformValue(self._location(name), *values)

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
    """Packed GLSL canvas with REAPER-like horizontal/vertical wheel zoom."""

    verticalScaleChanged = Signal(float)

    def paintGL(self):  # noqa: N802 - Qt API
        # Do not permanently replace the QObject-owned QOpenGLShaderProgram.
        # PySide6 6.11 needs location-based scalar uniform dispatch, but keeping
        # a Python proxy in ``self._program`` through QOpenGLWidget teardown can
        # outlive the C++ QObject child and crash during QApplication shutdown.
        program = self._program
        if program is None:
            return super().paintGL()
        self._program = _UniformNameProxy(program)
        try:
            return super().paintGL()
        finally:
            self._program = program

    def set_vertical_full_scale(self, value: float):
        self.vertical_full_scale = max(0.1, min(32.0, float(value)))
        self.update()

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        steps = wheel_steps(event)
        if steps == 0.0:
            event.ignore()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+wheel changes only the waveform vertical full-scale. Wheel
            # up makes the waveform taller by shrinking the displayed range.
            value = self.vertical_full_scale * (1.15 ** (-steps))
            self.set_vertical_full_scale(value)
            self.verticalScaleChanged.emit(self.vertical_full_scale)
        else:
            # Horizontal time zoom is anchored under the pointer, like REAPER.
            anchor = min(
                1.0,
                max(0.0, event.position().x() / max(1, self.width())),
            )
            self.zoom(0.72**steps, anchor)
        event.accept()
