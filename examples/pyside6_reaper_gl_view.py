"""REAPER-style interaction wrapper for the direct GLSL analysis canvas."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal

from pyside6_gl_view import GpuAnalysisCanvas


def wheel_steps(event) -> float:
    delta = event.angleDelta().y()
    if delta:
        return delta / 120.0
    pixel_delta = event.pixelDelta().y()
    if pixel_delta:
        return pixel_delta / 120.0
    return 0.0


class ReaperGpuAnalysisCanvas(GpuAnalysisCanvas):
    """GpuAnalysisCanvas with REAPER-like horizontal/vertical wheel zoom."""

    verticalScaleChanged = Signal(float)

    def set_vertical_full_scale(self, value: float):
        self.vertical_full_scale = max(0.1, min(32.0, float(value)))
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
            anchor = min(1.0, max(0.0, event.position().x() / max(1, self.width())))
            self.zoom(0.72**steps, anchor)
        event.accept()
