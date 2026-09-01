"""Build the small player overview from only one raw waveform layer.

The direct GLSL player path should not materialize every `.reapeaks` analysis
layer merely to draw an 84-pixel overview. This helper reads only the coarsest
waveform layer through `GpuCacheView` and rasterizes it once.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QImage, QPainter, QPen


def _signed_i16(lo: int, hi: int) -> int:
    value = lo | (hi << 8)
    return value - 0x10000 if value & 0x8000 else value


def _decode_amplitude(code: int, encoding: str) -> float:
    if encoding == "RPKN":
        return code / (32768.0 if code < 0 else 32767.0)
    negative = code < 0
    magnitude = abs(code)
    amplitude = (
        magnitude / 24576.0
        if magnitude <= 24576
        else 2.0 ** ((magnitude - 24576) / 1024.0)
    )
    return -amplitude if negative else amplitude


def build_gpu_overview_image(gpu, width: int = 1200, height: int = 84) -> QImage:
    width = max(1, int(width))
    height = max(1, int(height))
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(QColor(17, 20, 26, 255))

    levels = gpu.levels("waveform")
    if not levels:
        return image
    layer_index = len(levels) - 1
    _division, record_count, bytes_per_channel = levels[layer_index]
    if record_count <= 0 or bytes_per_channel != 4:
        return image

    first, records, channels, bpc, raw = gpu.records(
        "waveform", layer_index, 0, int(record_count)
    )
    if first != 0 or bpc != 4 or records <= 0 or channels <= 0:
        return image
    payload = bytes(raw)
    encoding = gpu.wave_encoding

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setPen(QPen(QColor(110, 218, 164, 255), 1))
    lane_height = height / channels
    for channel in range(channels):
        center = lane_height * (channel + 0.5)
        scale = lane_height * 0.45
        for x in range(width):
            start_record = min(records - 1, (x * records) // width)
            end_record = min(records, max(start_record + 1, ((x + 1) * records + width - 1) // width))
            maximum = -float("inf")
            minimum = float("inf")
            for record in range(start_record, end_record):
                offset = (record * channels + channel) * 4
                mx = _signed_i16(payload[offset], payload[offset + 1])
                mn = _signed_i16(payload[offset + 2], payload[offset + 3])
                maximum = max(maximum, _decode_amplitude(mx, encoding))
                minimum = min(minimum, _decode_amplitude(mn, encoding))
            if maximum == -float("inf"):
                continue
            y0 = int(center - maximum * scale)
            y1 = int(center - minimum * scale)
            painter.drawLine(x, y0, x, y1)
    painter.end()
    return image
