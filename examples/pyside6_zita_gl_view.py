"""Min/max contour waveform overlay for the PySide6 DAW demo.

The packed cache and source-PCM loaders still choose the data LOD.  Waveform
rasterization is intentionally simpler: every envelope record contributes one
maximum point and one minimum point, and adjacent records are connected as two
ordinary one-device-pixel contour lines.  Exact source samples are the degenerate
case where min == max, so they become one ordinary polyline.

This removes filled-envelope edge AA, zita-style vertical bars, and fragment
nearest-segment reconstruction from the DAW waveform view.  Geometry determines
the shape and QPainter performs normal cosmetic-line rasterization.
"""
from __future__ import annotations

import math
import struct

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

# Import the REAPER wrapper first: it installs its display-mode shader patches
# into pyside6_gl_view.FRAGMENT_SHADER before this module suppresses waveform
# drawing in the fullscreen fragment pass.
from pyside6_reaper_gl_view import ReaperGpuAnalysisCanvas
import pyside6_gl_view as _gl
from source_pcm import PcmDisplayWindow, pcm_display_values


def _disable_fragment_waveform(source: str) -> str:
    """Keep waveform textures resident but render waveform geometry with QPainter."""

    replacements = (
        (
            "if (u_displayMode == 0 && u_hasWave != 0 && u_waveCount > 0) {",
            "if (false && u_displayMode == 0 && u_hasWave != 0 && u_waveCount > 0) {",
        ),
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
            raise RuntimeError("waveform shader layout changed")
        patched = patched.replace(old, new, 1)
    return patched


_gl.FRAGMENT_SHADER = _disable_fragment_waveform(_gl.FRAGMENT_SHADER)


class ZitaGpuAnalysisCanvas(ReaperGpuAnalysisCanvas):
    """REAPER analysis canvas with continuous min/max waveform contours."""

    CACHE_COLOR = QColor(110, 235, 171, 245)
    SOURCE_COLOR = QColor(248, 226, 70, 245)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._source_values_window_id: int | None = None
        self._source_values = None
        self._packed_wave_key: tuple[object, ...] | None = None
        self._packed_wave_values: tuple[int, int, int, list[float]] | None = None

    def paintGL(self):  # noqa: N802 - Qt API
        # The base pass still performs LOD selection, paging, analysis rendering,
        # playhead/debug drawing, and source-PCM loading.  Its waveform branches
        # are disabled above; one contour overlay is added after the GL pass.
        super().paintGL()
        if self.display_mode == "waveform":
            self._paint_waveform_contours()

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

    def _source_values_for(self, window: PcmDisplayWindow):
        window_id = id(window)
        if self._source_values_window_id != window_id:
            self._source_values = pcm_display_values(window)
            self._source_values_window_id = window_id
        return self._source_values

    @staticmethod
    def _decode_wave_code(code: int, encoding: str) -> float:
        if encoding == "RPKN":
            if code == 0:
                return 0.0
            return code / (32768.0 if code < 0 else 32767.0)
        negative = code < 0
        magnitude = float(abs(code))
        amplitude = (
            magnitude / 24576.0
            if magnitude <= 24576.0
            else 2.0 ** ((magnitude - 24576.0) / 1024.0)
        )
        return -amplitude if negative else amplitude

    def _packed_values_for(self, upload):
        key = (
            int(upload.layer_index),
            int(upload.first_record),
            int(upload.record_count),
            str(self.gpu.wave_encoding),
            int(self.gpu.channels),
        )
        if self._packed_wave_key == key and self._packed_wave_values is not None:
            return self._packed_wave_values

        first, records, channels, bytes_per_channel, raw = self.gpu.records(
            "waveform",
            int(upload.layer_index),
            int(upload.first_record),
            int(upload.record_count),
        )
        first = int(first)
        records = int(records)
        channels = int(channels)
        if int(bytes_per_channel) != 4:
            raise ValueError(f"unexpected waveform record size {bytes_per_channel}")
        payload = bytes(raw)
        expected = records * channels * 4
        if len(payload) != expected:
            raise ValueError(
                f"waveform payload {len(payload)} bytes != expected {expected}"
            )

        values = [0.0] * (records * channels * 2)
        encoding = str(self.gpu.wave_encoding)
        for record in range(records):
            for channel in range(channels):
                offset = (record * channels + channel) * 4
                maximum_code, minimum_code = struct.unpack_from("<hh", payload, offset)
                target = (record * channels + channel) * 2
                values[target] = self._decode_wave_code(maximum_code, encoding)
                values[target + 1] = self._decode_wave_code(minimum_code, encoding)

        result = (first, records, channels, values)
        self._packed_wave_key = key
        self._packed_wave_values = result
        return result

    @staticmethod
    def _make_pen(color: QColor) -> QPen:
        pen = QPen(color)
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        pen.setJoinStyle(Qt.PenJoinStyle.BevelJoin)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        return pen

    def _paint_waveform_contours(self) -> None:
        source = self._ready_source_window()
        width = max(1, self.width())
        height = max(1, self.height())
        span = max(1.0, float(self.view_end - self.view_start))
        content_bottom = height * (0.90 if self.show_tile_debug else 1.0)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if source is not None and source.record_count > 0:
            painter.setPen(self._make_pen(self.SOURCE_COLOR))
            self._paint_source_contours(
                painter,
                source,
                width,
                height,
                span,
                content_bottom,
            )
        else:
            upload = self._uploads.get("waveform")
            if upload is not None and upload.record_count > 0:
                painter.setPen(self._make_pen(self.CACHE_COLOR))
                self._paint_packed_contours(
                    painter,
                    upload,
                    width,
                    height,
                    span,
                    content_bottom,
                )
        painter.end()

    def _amplitude_y(self, value: float, lane_top: float, lane_height: float) -> float:
        full_scale = max(1e-9, float(self.vertical_full_scale))
        return lane_top + (0.5 - float(value) / (2.0 * full_scale)) * lane_height

    def _paint_source_contours(
        self,
        painter: QPainter,
        window: PcmDisplayWindow,
        width: int,
        height: int,
        span: float,
        content_bottom: float,
    ) -> None:
        values = self._source_values_for(window)
        if values is None:
            return
        channels = max(1, int(window.channels))
        lane_height = height / channels
        division = max(1, int(window.division))

        for channel in range(channels):
            lane_top = channel * lane_height
            visible_bottom = min((channel + 1) * lane_height, content_bottom)
            if visible_bottom <= lane_top:
                continue
            painter.save()
            painter.setClipRect(
                QRectF(0.0, lane_top, float(width), visible_bottom - lane_top)
            )

            if window.mode == "samples":
                path = QPainterPath()
                started = False
                for record in range(window.record_count):
                    frame = window.first_frame + record
                    if frame < self.view_start - 1 or frame > self.view_end + 1:
                        continue
                    x = (frame - self.view_start) * width / span
                    offset = record * channels + channel
                    y = self._amplitude_y(values[offset], lane_top, lane_height)
                    if started:
                        path.lineTo(x, y)
                    else:
                        path.moveTo(x, y)
                        started = True
                if started:
                    painter.drawPath(path)
            else:
                max_path = QPainterPath()
                min_path = QPainterPath()
                started = False
                data_end = window.first_frame + window.frame_count
                for record in range(window.record_count):
                    frame0 = window.first_frame + record * division
                    frame1 = min(data_end, frame0 + division)
                    center = 0.5 * (frame0 + frame1)
                    if center < self.view_start - division or center > self.view_end + division:
                        continue
                    x = (center - self.view_start) * width / span
                    base = (record * channels + channel) * 2
                    maximum = float(values[base])
                    minimum = float(values[base + 1])
                    top = self._amplitude_y(maximum, lane_top, lane_height)
                    bottom = self._amplitude_y(minimum, lane_top, lane_height)
                    if started:
                        max_path.lineTo(x, top)
                        min_path.lineTo(x, bottom)
                    else:
                        max_path.moveTo(x, top)
                        min_path.moveTo(x, bottom)
                        started = True
                if started:
                    painter.drawPath(max_path)
                    painter.drawPath(min_path)
            painter.restore()

    def _paint_packed_contours(
        self,
        painter: QPainter,
        upload,
        width: int,
        height: int,
        span: float,
        content_bottom: float,
    ) -> None:
        first, records, channels, values = self._packed_values_for(upload)
        if records <= 0 or channels <= 0:
            return
        lane_height = height / channels
        division = max(1, int(upload.division))

        for channel in range(channels):
            lane_top = channel * lane_height
            visible_bottom = min((channel + 1) * lane_height, content_bottom)
            if visible_bottom <= lane_top:
                continue
            painter.save()
            painter.setClipRect(
                QRectF(0.0, lane_top, float(width), visible_bottom - lane_top)
            )
            max_path = QPainterPath()
            min_path = QPainterPath()
            started = False
            for record in range(records):
                center = (first + record + 0.5) * division
                if center < self.view_start - division or center > self.view_end + division:
                    continue
                x = (center - self.view_start) * width / span
                base = (record * channels + channel) * 2
                top = self._amplitude_y(values[base], lane_top, lane_height)
                bottom = self._amplitude_y(values[base + 1], lane_top, lane_height)
                if started:
                    max_path.lineTo(x, top)
                    min_path.lineTo(x, bottom)
                else:
                    max_path.moveTo(x, top)
                    min_path.moveTo(x, bottom)
                    started = True
            if started:
                painter.drawPath(max_path)
                painter.drawPath(min_path)
            painter.restore()
