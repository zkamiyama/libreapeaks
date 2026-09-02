"""zita-scope-inspired source-PCM waveform overlay for the PySide6 DAW demo.

The packed `.reapeaks` shader remains responsible for normal cache LODs and
analysis views.  Once source PCM becomes resident, this module deliberately
stops reconstructing the waveform as a fragment-distance field.  Instead it
uses the same two visual primitives described by zita-scope/Ardour:

* sub-sample zoom: aggregate min/max into actual screen-pixel columns, drawing
  vertical extents and connecting adjacent non-overlapping extents;
* sample zoom: draw the decoded source samples as one ordinary polyline.

Both paths use a one-device-pixel cosmetic pen.  Geometry decides the shape;
QPainter's rasterizer supplies ordinary line antialiasing.  There is no
slope-dependent `fwidth()` or per-fragment nearest-segment selection.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

# Import the REAPER wrapper first: it installs its display-mode shader patches
# into pyside6_gl_view.FRAGMENT_SHADER before we suppress the old PCM branches.
from pyside6_reaper_gl_view import ReaperGpuAnalysisCanvas
import pyside6_gl_view as _gl
from source_pcm import PcmDisplayWindow, pcm_display_values


def _disable_fragment_pcm(source: str) -> str:
    """Keep source PCM resident but prevent the fullscreen shader drawing it."""

    replacements = (
        (
            "if (u_displayMode == 0 && u_pcmMode == 1 && u_pcmCount > 0) {",
            "if (false && u_displayMode == 0 && u_pcmMode == 1 && u_pcmCount > 0) {",
        ),
        (
            "} else if (u_displayMode == 0 && u_pcmMode == 2 && u_pcmCount > 0) {",
            "} else if (false && u_displayMode == 0 && u_pcmMode == 2 && u_pcmCount > 0) {",
        ),
    )
    patched = source
    for old, new in replacements:
        if new in patched:
            continue
        if old not in patched:
            raise RuntimeError("source-PCM shader layout changed")
        patched = patched.replace(old, new, 1)
    return patched


_gl.FRAGMENT_SHADER = _disable_fragment_pcm(_gl.FRAGMENT_SHADER)


class ZitaGpuAnalysisCanvas(ReaperGpuAnalysisCanvas):
    """REAPER analysis canvas with zita-style source waveform rasterization."""

    SOURCE_COLOR = QColor(248, 226, 70, 245)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._zita_values_window_id: int | None = None
        self._zita_values = None

    def paintGL(self):  # noqa: N802 - Qt API
        # The base pass still selects/loads the source-PCM LOD, suppresses the
        # packed waveform while PCM is active, and draws all non-PCM UI.  Its
        # PCM fragment branches were disabled above, so only real line geometry
        # is added here.
        super().paintGL()
        if self.display_mode == "waveform":
            self._paint_source_pcm_geometry()

    def _ready_source_window(self) -> PcmDisplayWindow | None:
        if self.pcm_loader is None or self._pcm_upload is None:
            return None
        window = self.pcm_loader.ready_window
        if window is None:
            return None
        if self._pcm_upload.key != (
            window.first_frame,
            window.frame_count,
            window.division,
        ):
            return None
        return window

    def _values_for(self, window: PcmDisplayWindow):
        window_id = id(window)
        if self._zita_values_window_id != window_id:
            self._zita_values = pcm_display_values(window)
            self._zita_values_window_id = window_id
        return self._zita_values

    def _paint_source_pcm_geometry(self) -> None:
        window = self._ready_source_window()
        if window is None or window.record_count <= 0:
            return

        width = max(1, self.width())
        height = max(1, self.height())
        span = max(1.0, float(self.view_end - self.view_start))
        channels = max(1, int(window.channels))
        dpr = max(1.0, float(self.devicePixelRatioF()))
        physical_width = max(1, int(round(width * dpr)))
        content_bottom = height * (0.90 if self.show_tile_debug else 1.0)
        values = self._values_for(window)
        if values is None:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(self.SOURCE_COLOR)
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        # A bevel join is intentional: sharp sample turns must not grow miter
        # spikes that look like transient "hairs" while zooming.
        pen.setJoinStyle(Qt.PenJoinStyle.BevelJoin)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        lane_height = height / channels
        for channel in range(channels):
            lane_top = channel * lane_height
            lane_bottom = (channel + 1) * lane_height
            visible_bottom = min(lane_bottom, content_bottom)
            if visible_bottom <= lane_top:
                continue
            painter.save()
            painter.setClipRect(QRectF(0.0, lane_top, float(width), visible_bottom - lane_top))
            if window.mode == "samples":
                self._paint_sample_polyline(
                    painter,
                    window,
                    values,
                    channel,
                    lane_top,
                    lane_height,
                    span,
                    width,
                )
            else:
                self._paint_zita_envelope(
                    painter,
                    window,
                    values,
                    channel,
                    lane_top,
                    lane_height,
                    span,
                    physical_width,
                    dpr,
                )
            painter.restore()
        painter.end()

    def _amplitude_y(self, value: float, lane_top: float, lane_height: float) -> float:
        full_scale = max(1e-9, float(self.vertical_full_scale))
        return lane_top + (0.5 - float(value) / (2.0 * full_scale)) * lane_height

    def _paint_sample_polyline(
        self,
        painter: QPainter,
        window: PcmDisplayWindow,
        values,
        channel: int,
        lane_top: float,
        lane_height: float,
        span: float,
        width: int,
    ) -> None:
        """High-resolution zita path: one ordinary line through raw samples."""

        relative_start = self.view_start - window.first_frame
        relative_end = self.view_end - window.first_frame
        first = max(0, int(math.floor(relative_start)) - 1)
        last = min(window.record_count, int(math.ceil(relative_end)) + 2)
        if last - first < 2:
            return

        path = QPainterPath()
        started = False
        for record in range(first, last):
            frame = window.first_frame + record
            x = (frame - self.view_start) * width / span
            offset = record * window.channels + channel
            y = self._amplitude_y(values[offset], lane_top, lane_height)
            if not started:
                path.moveTo(x, y)
                started = True
            else:
                path.lineTo(x, y)
        if started:
            painter.drawPath(path)

    def _paint_zita_envelope(
        self,
        painter: QPainter,
        window: PcmDisplayWindow,
        values,
        channel: int,
        lane_top: float,
        lane_height: float,
        span: float,
        physical_width: int,
        dpr: float,
    ) -> None:
        """Low-resolution zita path: per-pixel min/max plus connected extents."""

        maxima = [-math.inf] * physical_width
        minima = [math.inf] * physical_width
        present = bytearray(physical_width)
        division = max(1, int(window.division))
        data_end = window.first_frame + window.frame_count

        for record in range(window.record_count):
            frame0 = window.first_frame + record * division
            frame1 = min(data_end, frame0 + division)
            if frame1 <= self.view_start or frame0 >= self.view_end:
                continue

            clipped0 = max(float(frame0), float(self.view_start))
            clipped1 = min(float(frame1), float(self.view_end))
            x0 = (clipped0 - self.view_start) * physical_width / span
            x1 = (clipped1 - self.view_start) * physical_width / span
            first_col = max(0, min(physical_width - 1, int(math.floor(x0))))
            last_col = max(
                first_col,
                min(physical_width - 1, int(math.ceil(x1) - 1)),
            )

            base = (record * window.channels + channel) * 2
            maximum = float(values[base])
            minimum = float(values[base + 1])
            for column in range(first_col, last_col + 1):
                if not present[column]:
                    maxima[column] = maximum
                    minima[column] = minimum
                    present[column] = 1
                else:
                    maxima[column] = max(maxima[column], maximum)
                    minima[column] = min(minima[column], minimum)

        path = QPainterPath()
        for column in range(physical_width):
            if not present[column]:
                continue
            x = (column + 0.5) / dpr
            top = self._amplitude_y(maxima[column], lane_top, lane_height)
            bottom = self._amplitude_y(minima[column], lane_top, lane_height)

            if column + 1 < physical_width and present[column + 1]:
                next_x = (column + 1.5) / dpr
                next_top = self._amplitude_y(
                    maxima[column + 1], lane_top, lane_height
                )
                next_bottom = self._amplitude_y(
                    minima[column + 1], lane_top, lane_height
                )
                # zita-scope / Ardour Fig.3 rule: when neighboring min/max
                # intervals do not overlap, connect the nearest matching edge
                # instead of showing two disconnected vertical bars.
                if top >= next_bottom:
                    path.moveTo(x, bottom)
                    path.lineTo(next_x, next_bottom)
                    continue
                if bottom <= next_top:
                    path.moveTo(x, top)
                    path.lineTo(next_x, next_top)
                    continue

            path.moveTo(x, top)
            path.lineTo(x, bottom)

        painter.drawPath(path)
