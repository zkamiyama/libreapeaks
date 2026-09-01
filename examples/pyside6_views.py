"""Tiled PySide6 waveform/spectral widgets used by pyside6_player.py."""
from __future__ import annotations

from collections import OrderedDict
import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

import reapeaks

BG = QColor(17, 20, 26)
GRID = QColor(54, 62, 75)
WAVE = QColor(110, 218, 164)
SPECTRAL = QColor(95, 174, 255)
PLAYHEAD = QColor(255, 196, 64)
TILE_A = QColor(255, 255, 255, 10)
TILE_B = QColor(80, 160, 255, 12)
TILE_EDGE = QColor(230, 120, 255, 150)
TEXT = QColor(210, 218, 230)


def signed_i16(lo: int, hi: int) -> int:
    value = lo | (hi << 8)
    return value - 0x10000 if value & 0x8000 else value


def decode_amplitude(code: int, encoding: str) -> float:
    if encoding == "RPKN":
        return code / (32768.0 if code < 0 else 32767.0)
    neg = code < 0
    mag = abs(code)
    amp = mag / 24576.0 if mag <= 24576 else 2.0 ** ((mag - 24576) / 1024.0)
    return -amp if neg else amp


def decode_u32_le(raw: bytes, offset: int) -> int:
    return (
        raw[offset]
        | (raw[offset + 1] << 8)
        | (raw[offset + 2] << 16)
        | (raw[offset + 3] << 24)
    )


def wheel_steps(event) -> float:
    """Return continuous wheel steps; one conventional detent is 1.0."""

    delta = event.angleDelta().y()
    if delta:
        return delta / 120.0
    pixel_delta = event.pixelDelta().y()
    if pixel_delta:
        return pixel_delta / 120.0
    return 0.0


class TileLru:
    def __init__(self, capacity: int = 96):
        self.capacity = capacity
        self.items: OrderedDict[tuple, tuple] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple, loader):
        if key in self.items:
            value = self.items.pop(key)
            self.items[key] = value
            self.hits += 1
            return value
        value = loader()
        self.items[key] = value
        self.misses += 1
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)
        return value


class PeaksCanvas(QWidget):
    viewChanged = Signal(int, int)
    seekRequested = Signal(int)
    verticalScaleChanged = Signal(float)

    def __init__(self, rp: reapeaks.ReaPeaks, total_frames: int, parent=None):
        super().__init__(parent)
        self.rp = rp
        self.total_frames = max(1, total_frames)
        self.view_start = 0
        self.view_end = self.total_frames
        self.playhead = 0
        self.show_tiles = True
        self.vertical_full_scale = 1.0
        self.cache = TileLru()
        self.levels = rp.levels()
        self.native_levels = [
            (level_index, division, peak_count)
            for level_index, (division, peak_count, native) in enumerate(self.levels)
            if native
        ]
        self.drag_last_x: float | None = None
        self.drag_distance = 0.0
        self.diagnostics = ""
        self.setMinimumHeight(430)
        self.setMouseTracking(True)

    def set_total_frames(self, frames: int):
        old_total = self.total_frames
        self.total_frames = max(1, frames)
        if self.view_end >= old_total - 1:
            self.set_view(0, self.total_frames, emit=False)
        else:
            self.set_view(self.view_start, min(self.view_end, self.total_frames), emit=False)

    def set_view(self, start: int, end: int, *, emit: bool = True):
        minimum_span = max(64, self.rp.sample_rate // 50)
        span = max(minimum_span, int(end) - int(start))
        span = min(span, self.total_frames)
        start = max(0, min(int(start), self.total_frames - span))
        end = start + span
        if (start, end) == (self.view_start, self.view_end):
            return
        self.view_start, self.view_end = start, end
        if emit:
            self.viewChanged.emit(start, end)
        self.update()

    def set_playhead(self, frame: int):
        self.playhead = max(0, min(frame, self.total_frames))
        self.update()

    def set_tile_debug(self, enabled: bool):
        self.show_tiles = enabled
        self.update()

    def set_vertical_full_scale(self, value: float):
        self.vertical_full_scale = max(0.1, min(32.0, float(value)))
        self.update()

    def zoom(self, factor: float, anchor_ratio: float = 0.5):
        span = self.view_end - self.view_start
        new_span = int(span * factor)
        minimum_span = max(64, self.rp.sample_rate // 50)
        new_span = max(minimum_span, min(new_span, self.total_frames))
        anchor = self.view_start + span * anchor_ratio
        new_start = int(anchor - new_span * anchor_ratio)
        self.set_view(new_start, new_start + new_span)

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        steps = wheel_steps(event)
        if steps == 0.0:
            event.ignore()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # REAPER-like vertical waveform zoom: wheel up makes the waveform
            # taller, which corresponds to a smaller full-scale range.
            value = self.vertical_full_scale * (1.15 ** (-steps))
            self.set_vertical_full_scale(value)
            self.verticalScaleChanged.emit(self.vertical_full_scale)
        else:
            ratio = min(1.0, max(0.0, event.position().x() / max(1, self.width())))
            self.zoom(0.72**steps, ratio)
        event.accept()

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_last_x = event.position().x()
            self.drag_distance = 0.0
            event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self.drag_last_x is None:
            return
        x = event.position().x()
        dx = x - self.drag_last_x
        self.drag_last_x = x
        self.drag_distance += abs(dx)
        frames_per_px = (self.view_end - self.view_start) / max(1, self.width())
        shift = int(-dx * frames_per_px)
        self.set_view(self.view_start + shift, self.view_end + shift)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self.drag_last_x is None:
            return
        if self.drag_distance < 4.0:
            ratio = min(1.0, max(0.0, event.position().x() / max(1, self.width())))
            frame = int(self.view_start + ratio * (self.view_end - self.view_start))
            self.seekRequested.emit(frame)
        self.drag_last_x = None
        event.accept()

    def _wave_tile(self, level_index: int, tile_index: int):
        key = ("wave", level_index, tile_index)

        def load():
            first, width, height, raw = self.rp.tile_texture(level_index, tile_index)
            return first, width, height, bytes(raw)

        return self.cache.get(key, load)

    def _spectral_tile(self, layer_index: int, tile_index: int):
        key = ("spectral", layer_index, tile_index)

        def load():
            first, width, height, raw = self.rp.spectral_tile_texture(layer_index, tile_index)
            return first, width, height, bytes(raw)

        return self.cache.get(key, load)

    def _x_for_frame(self, frame: float) -> float:
        return (frame - self.view_start) * self.width() / max(1, self.view_end - self.view_start)

    def _draw_background(self, painter: QPainter, top: float, height: float, channels: int):
        painter.fillRect(0, int(top), self.width(), int(height), BG)
        painter.setPen(QPen(GRID, 1))
        for c in range(channels + 1):
            y = top + height * c / channels
            painter.drawLine(0, int(y), self.width(), int(y))
        for c in range(channels):
            y = top + height * (c + 0.5) / channels
            painter.drawLine(0, int(y), self.width(), int(y))

    def _draw_tile_band(self, painter: QPainter, x0: float, x1: float, y0: float, h: float, label: str, odd: bool):
        if not self.show_tiles:
            return
        painter.fillRect(int(x0), int(y0), max(1, int(x1 - x0)), int(h), TILE_B if odd else TILE_A)
        painter.setPen(QPen(TILE_EDGE, 1, Qt.PenStyle.DashLine))
        painter.drawLine(int(x0), int(y0), int(x0), int(y0 + h))
        painter.setPen(TEXT)
        painter.drawText(int(x0) + 4, int(y0) + 15, label)

    def _draw_waveform(self, painter: QPainter, top: float, height: float):
        channels = max(1, int(self.rp.channels))
        self._draw_background(painter, top, height, channels)
        try:
            level_index, division, first_peak, peak_count, ppp = self.rp.plan_view(
                self.view_start, self.view_end, max(1, self.width())
            )
            tile_keys = self.rp.tiles_for_view(self.view_start, self.view_end, max(1, self.width()))
        except ValueError:
            return None, []

        painter.setPen(QPen(WAVE, 1))
        band_h = height / channels
        visible = []
        for _, tile_index in tile_keys:
            first, width, tex_h, raw = self._wave_tile(level_index, tile_index)
            visible.append(f"L{level_index}/T{tile_index}")
            x0 = self._x_for_frame(first * division)
            x1 = self._x_for_frame((first + width) * division)
            self._draw_tile_band(
                painter, x0, x1, top, height, f"L{level_index} T{tile_index}", bool(tile_index & 1)
            )
            for c in range(min(channels, tex_h)):
                center = top + band_h * (c + 0.5)
                scale = band_h * 0.45 / self.vertical_full_scale
                row = c * width * 4
                for i in range(width):
                    frame = (first + i) * division
                    if frame < self.view_start or frame > self.view_end:
                        continue
                    off = row + i * 4
                    mx = signed_i16(raw[off], raw[off + 1])
                    mn = signed_i16(raw[off + 2], raw[off + 3])
                    a = decode_amplitude(mx, self.rp.wave_encoding)
                    b = decode_amplitude(mn, self.rp.wave_encoding)
                    x = int(self._x_for_frame(frame))
                    y0 = int(center - a * scale)
                    y1 = int(center - b * scale)
                    painter.drawLine(x, y0, x, y1)

        return (level_index, division, first_peak, peak_count, ppp), visible

    def _nearest_spectral_level(self, desired_division: int):
        if not self.native_levels:
            return None
        best_native_index = min(
            range(len(self.native_levels)),
            key=lambda i: abs(math.log(max(1, self.native_levels[i][1]) / max(1, desired_division))),
        )
        level_index, division, _wave_count = self.native_levels[best_native_index]
        return best_native_index, level_index, division

    def _draw_spectrum(self, painter: QPainter, top: float, height: float, plan):
        channels = max(1, int(self.rp.channels))
        self._draw_background(painter, top, height, channels)
        if plan is None:
            return None, []
        chosen = self._nearest_spectral_level(plan[1])
        if chosen is None:
            return None, []
        layer_index, level_index, division = chosen
        tile_peaks = max(1, int(self.rp.tile_peaks))
        first_peak = self.view_start // division
        last_peak = (self.view_end + division - 1) // division
        first_tile = first_peak // tile_peaks
        last_tile = last_peak // tile_peaks
        band_h = height / channels
        log_lo = math.log(20.0)
        log_hi = math.log(max(21.0, self.rp.sample_rate * 0.5))
        visible = []

        for tile_index in range(first_tile, last_tile + 1):
            try:
                first, width, tex_h, raw = self._spectral_tile(layer_index, tile_index)
            except ValueError:
                continue
            visible.append(f"S{layer_index}/T{tile_index}")
            x0 = self._x_for_frame(first * division)
            x1 = self._x_for_frame((first + width) * division)
            self._draw_tile_band(
                painter, x0, x1, top, height, f"S{layer_index} T{tile_index}", bool(tile_index & 1)
            )
            for c in range(min(channels, tex_h)):
                row = c * width * 4
                lane_top = top + c * band_h
                for i in range(width):
                    frame = (first + i) * division
                    if frame < self.view_start or frame > self.view_end:
                        continue
                    code = decode_u32_le(raw, row + i * 4)
                    frequency = code & 0x7FFF
                    density = (code >> 15) & 0x3FFF
                    if frequency <= 0:
                        continue
                    frac = (math.log(max(20.0, float(frequency))) - log_lo) / max(1e-9, log_hi - log_lo)
                    frac = min(1.0, max(0.0, frac))
                    y = lane_top + band_h * (1.0 - frac)
                    x = self._x_for_frame(frame)
                    alpha = 45 + int(210 * density / 16383.0)
                    color = QColor(SPECTRAL)
                    color.setAlpha(alpha)
                    painter.setPen(QPen(color, 1))
                    painter.drawPoint(int(x), int(y))

        return (layer_index, level_index, division), visible

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        h = self.height()
        wave_h = h * 0.60
        spec_top = wave_h + 4
        spec_h = h - spec_top
        plan, wave_tiles = self._draw_waveform(painter, 0, wave_h)
        spectral_plan, spectral_tiles = self._draw_spectrum(painter, spec_top, spec_h, plan)

        x = self._x_for_frame(self.playhead)
        if 0 <= x <= self.width():
            painter.setPen(QPen(PLAYHEAD, 2))
            painter.drawLine(int(x), 0, int(x), h)

        if plan:
            spectral_text = "none" if spectral_plan is None else f"layer={spectral_plan[0]} div={spectral_plan[2]}"
            self.diagnostics = (
                f"wave level={plan[0]} div={plan[1]} peaks={plan[3]} ppp={plan[4]:.2f} | "
                f"tiles [{', '.join(wave_tiles)}] | spectral {spectral_text} "
                f"tiles [{', '.join(spectral_tiles)}] | LRU {len(self.cache.items)}/{self.cache.capacity} "
                f"hit={self.cache.hits} miss={self.cache.misses}"
            )
        painter.end()


class OverviewWidget(QWidget):
    seekRequested = Signal(int)

    def __init__(self, image: QImage, total_frames: int, parent=None):
        super().__init__(parent)
        self.image = image
        self.total_frames = max(1, total_frames)
        self.view_start = 0
        self.view_end = self.total_frames
        self.playhead = 0
        self.setFixedHeight(max(76, image.height()))

    def set_state(self, start: int, end: int, playhead: int, total_frames: int):
        self.total_frames = max(1, total_frames)
        self.view_start, self.view_end, self.playhead = start, end, playhead
        self.update()

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            frame = int(event.position().x() / max(1, self.width()) * self.total_frames)
            self.seekRequested.emit(frame)

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.drawImage(self.rect(), self.image)
        x0 = self.view_start / self.total_frames * self.width()
        x1 = self.view_end / self.total_frames * self.width()
        painter.fillRect(int(x0), 0, max(1, int(x1 - x0)), self.height(), QColor(255, 255, 255, 24))
        painter.setPen(QPen(TILE_EDGE, 1))
        painter.drawRect(int(x0), 0, max(1, int(x1 - x0)), self.height() - 1)
        xp = self.playhead / self.total_frames * self.width()
        painter.setPen(QPen(PLAYHEAD, 2))
        painter.drawLine(int(xp), 0, int(xp), self.height())
        painter.end()
