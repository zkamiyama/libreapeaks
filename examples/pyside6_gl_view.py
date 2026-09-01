"""Direct `.reapeaks` -> OpenGL renderer for the PySide6 demo.

This path intentionally avoids CPU display conversion. Waveform and `-'s'`
records are uploaded as RGBA8UI, packed `-'g'` frames remain their exact 192
on-disk bytes and are uploaded as R8UI, and `-'r'` records are uploaded as
RG32F. GLSL performs decoding, spectrogram unpacking, gain/palette transforms,
overlays, playhead drawing, and a realtime GPU-residency debug strip.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import math
import sys
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QSurfaceFormat, QVector2D
from PySide6.QtOpenGL import (
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLTimerQuery,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

import reapeaks
from pyside6_pcm_loader import PcmWindowLoader
from source_pcm import (
    MIN_SAMPLE_VIEW_FRAMES,
    PcmDisplayWindow,
    SourcePcmService,
    plan_pcm_draw,
)


VERTEX_SHADER = r"""
#version 330 core
out vec2 v_uv;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    v_uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

FRAGMENT_SHADER = r"""
#version 330 core
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
    int row = record * u_channels + channel;
    int pair = bin >> 1;
    int x = pair * 3;
    uint b0 = texelFetch(u_g, ivec2(x, row), 0).r;
    uint b1 = texelFetch(u_g, ivec2(x + 1, row), 0).r;
    uint b2 = texelFetch(u_g, ivec2(x + 2, row), 0).r;
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
    vec3 active = row == 0 ? vec3(0.30, 0.95, 0.56)
                : row == 1 ? vec3(0.28, 0.62, 1.00)
                : row == 2 ? vec3(0.95, 0.34, 0.90)
                : row == 3 ? vec3(1.00, 0.58, 0.18)
                           : vec3(0.98, 0.90, 0.28);
    vec3 base = resident(r, x) ? active : vec3(0.10, 0.11, 0.14);
    if (x >= u_viewGlobal.x && x <= u_viewGlobal.y) {
        base = mix(base, vec3(1.0), 0.20);
    }
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
        float intensity = clamp(
            float(unpackG(record, channel, bin)) / 4095.0 * u_specGain,
            0.0,
            1.0
        );
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
            float target = 1.0 - clamp(
                (log(max(20.0, frequency)) - logLo) / max(1e-6, logHi - logLo),
                0.0,
                1.0
            );
            float alpha = (1.0 - smoothstep(0.002, 0.012, abs(localY - target)))
                        * (0.25 + 0.75 * density);
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
        float inside = smoothstep(mn - aa, mn + aa, amplitude)
                     * (1.0 - smoothstep(mx - aa, mx + aa, amplitude));
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
}
"""


@dataclass
class UploadWindow:
    kind: str
    layer_index: int
    division: int
    first_record: int
    record_count: int
    texture: QOpenGLTexture
    byte_count: int

    def normalized_range(self, total_frames: int) -> tuple[float, float]:
        denominator = max(1, total_frames)
        start = self.first_record * self.division / denominator
        end = (self.first_record + self.record_count) * self.division / denominator
        return max(0.0, start), min(1.0, end)


@dataclass
class PcmUploadWindow:
    key: tuple[int, int, int]
    first_frame: int
    frame_count: int
    division: int
    record_count: int
    mode: str
    backend: str
    texture: QOpenGLTexture
    byte_count: int

    def normalized_range(self, total_frames: int) -> tuple[float, float]:
        denominator = max(1, total_frames)
        return (
            max(0.0, self.first_frame / denominator),
            min(1.0, (self.first_frame + self.frame_count) / denominator),
        )


class GpuAnalysisCanvas(QOpenGLWidget):
    """Composite REAPER-style analysis display driven by packed cache bytes."""

    viewChanged = Signal(int, int)
    seekRequested = Signal(int)
    PAGE_RECORDS = 512

    def __init__(
        self,
        peaks_path: str,
        total_frames: int,
        parent=None,
        *,
        pcm_service: SourcePcmService | None = None,
    ):
        super().__init__(parent)
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setDepthBufferSize(0)
        fmt.setStencilBufferSize(0)
        self.setFormat(fmt)

        self.gpu = reapeaks.GpuCacheView.open(peaks_path)
        self.total_frames = max(1, int(total_frames))
        self.view_start = 0
        self.view_end = self.total_frames
        self.playhead = 0
        self.vertical_full_scale = 1.0
        self.spectrogram_gain = 1.0
        self.heatmap = True
        self.show_spectral = True
        self.show_loudness = True
        self.show_tile_debug = True
        self.drag_last_x: float | None = None
        self.drag_distance = 0.0

        self._program: QOpenGLShaderProgram | None = None
        self._vao: QOpenGLVertexArrayObject | None = None
        self._gl = None
        self._uploads: dict[str, UploadWindow] = {}
        self._pcm_upload: PcmUploadWindow | None = None
        self.pcm_loader = (
            PcmWindowLoader(pcm_service, self) if pcm_service is not None else None
        )
        if self.pcm_loader is not None:
            self.pcm_loader.changed.connect(self.update)
        self._gpu_query: QOpenGLTimerQuery | None = None
        self._gpu_query_pending = False
        self._gpu_ms = 0.0
        self._cpu_ms = 0.0
        self._read_ms = 0.0
        self._upload_ms = 0.0
        self._upload_bytes_total = 0
        self._upload_count = 0
        self._frame_count = 0
        self.diagnostics = "GL waiting for context"
        self.setMinimumHeight(430)
        self.setMouseTracking(True)

    def set_total_frames(self, frames: int):
        old = self.total_frames
        self.total_frames = max(1, int(frames))
        if self.view_end >= old - 1:
            self.set_view(0, self.total_frames, emit=False)
        else:
            self.set_view(
                self.view_start,
                min(self.view_end, self.total_frames),
                emit=False,
            )

    def set_view(self, start: int, end: int, *, emit: bool = True):
        minimum_span = (
            MIN_SAMPLE_VIEW_FRAMES
            if self.pcm_loader is not None
            else max(64, self.gpu.sample_rate // 50)
        )
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
        self.playhead = max(0, min(int(frame), self.total_frames))
        self.update()

    def set_vertical_full_scale(self, value: float):
        self.vertical_full_scale = max(0.1, min(32.0, float(value)))
        self.update()

    def set_spectrogram_gain(self, value: float):
        self.spectrogram_gain = max(0.01, min(64.0, float(value)))
        self.update()

    def set_heatmap(self, enabled: bool):
        self.heatmap = bool(enabled)
        self.update()

    def set_spectral_overlay(self, enabled: bool):
        self.show_spectral = bool(enabled)
        self.update()

    def set_loudness_overlay(self, enabled: bool):
        self.show_loudness = bool(enabled)
        self.update()

    def set_tile_debug(self, enabled: bool):
        self.show_tile_debug = bool(enabled)
        self.update()

    def zoom(self, factor: float, anchor_ratio: float = 0.5):
        span = self.view_end - self.view_start
        minimum_span = (
            MIN_SAMPLE_VIEW_FRAMES
            if self.pcm_loader is not None
            else max(64, self.gpu.sample_rate // 50)
        )
        new_span = max(
            minimum_span,
            min(int(span * factor), self.total_frames),
        )
        anchor = self.view_start + span * anchor_ratio
        start = int(anchor - new_span * anchor_ratio)
        self.set_view(start, start + new_span)

    def wheelEvent(self, event):  # noqa: N802
        ratio = min(1.0, max(0.0, event.position().x() / max(1, self.width())))
        self.zoom(0.72 if event.angleDelta().y() > 0 else 1.0 / 0.72, ratio)
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
        shift = int(-dx * (self.view_end - self.view_start) / max(1, self.width()))
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

    @staticmethod
    def _nearest_level(levels, desired_division: float):
        if not levels:
            return None
        return min(
            enumerate(levels),
            key=lambda item: abs(
                math.log(
                    max(1.0, float(item[1][0])) / max(1.0, desired_division)
                )
            ),
        )

    def _window_for(self, kind: str, layer_index: int, division: int, total: int):
        first_needed = max(0, self.view_start // max(1, division) - 2)
        last_needed = min(
            total,
            (self.view_end + division - 1) // max(1, division) + 3,
        )
        page = self.PAGE_RECORDS
        first = (first_needed // page) * page
        minimum_end = min(total, first + page * 2)
        last = min(
            total,
            max(minimum_end, ((last_needed + page - 1) // page) * page),
        )
        count = max(1, last - first)
        current = self._uploads.get(kind)
        if (
            current is not None
            and current.layer_index == layer_index
            and current.first_record == first
            and current.record_count == count
        ):
            return current

        read_start = time.perf_counter_ns()
        first_actual, records, channels, bytes_per_channel, raw = self.gpu.records(
            kind,
            layer_index,
            first,
            count,
        )
        payload = bytes(raw)
        read_end = time.perf_counter_ns()
        texture = self._upload_texture(
            kind,
            int(records),
            int(channels),
            int(bytes_per_channel),
            payload,
        )
        upload_end = time.perf_counter_ns()

        if current is not None:
            current.texture.destroy()
        window = UploadWindow(
            kind,
            layer_index,
            division,
            int(first_actual),
            int(records),
            texture,
            len(payload),
        )
        self._uploads[kind] = window
        self._read_ms = (read_end - read_start) / 1e6
        self._upload_ms = (upload_end - read_end) / 1e6
        self._upload_bytes_total += len(payload)
        self._upload_count += 1
        return window

    @staticmethod
    def _upload_texture(
        kind: str,
        records: int,
        channels: int,
        bytes_per_channel: int,
        payload: bytes,
    ) -> QOpenGLTexture:
        texture = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
        texture.setMipLevels(1)
        texture.setMinMagFilters(
            QOpenGLTexture.Filter.Nearest,
            QOpenGLTexture.Filter.Nearest,
        )
        texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
        if kind in ("waveform", "spectral"):
            if bytes_per_channel != 4:
                raise ValueError(f"unexpected {kind} record size {bytes_per_channel}")
            texture.setFormat(QOpenGLTexture.TextureFormat.RGBA8U)
            texture.setSize(channels, records)
            texture.allocateStorage(
                QOpenGLTexture.PixelFormat.RGBA_Integer,
                QOpenGLTexture.PixelType.UInt8,
            )
            texture.setData(
                QOpenGLTexture.PixelFormat.RGBA_Integer,
                QOpenGLTexture.PixelType.UInt8,
                payload,
            )
        elif kind == "spectrogram":
            if bytes_per_channel != 192:
                raise ValueError(
                    f"unexpected spectrogram record size {bytes_per_channel}"
                )
            texture.setFormat(QOpenGLTexture.TextureFormat.R8U)
            texture.setSize(192, records * channels)
            texture.allocateStorage(
                QOpenGLTexture.PixelFormat.Red_Integer,
                QOpenGLTexture.PixelType.UInt8,
            )
            texture.setData(
                QOpenGLTexture.PixelFormat.Red_Integer,
                QOpenGLTexture.PixelType.UInt8,
                payload,
            )
        elif kind == "loudness":
            if bytes_per_channel != 8:
                raise ValueError(
                    f"unexpected loudness record size {bytes_per_channel}"
                )
            texture.setFormat(QOpenGLTexture.TextureFormat.RG32F)
            texture.setSize(channels, records)
            texture.allocateStorage(
                QOpenGLTexture.PixelFormat.RG,
                QOpenGLTexture.PixelType.Float32,
            )
            texture.setData(
                QOpenGLTexture.PixelFormat.RG,
                QOpenGLTexture.PixelType.Float32,
                payload,
            )
        else:
            raise ValueError(f"unknown GPU texture kind {kind}")
        return texture

    @staticmethod
    def _upload_pcm_texture(window: PcmDisplayWindow) -> QOpenGLTexture:
        expected = (
            window.record_count * window.channels * window.components * 4
        )
        if len(window.data_f32le) != expected:
            raise ValueError(
                f"source PCM payload {len(window.data_f32le)} != expected {expected}"
            )
        payload = window.data_f32le
        if sys.byteorder != "little":
            converted = array("f")
            converted.frombytes(payload)
            converted.byteswap()
            payload = converted.tobytes()
        texture = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
        texture.setMipLevels(1)
        texture.setMinMagFilters(
            QOpenGLTexture.Filter.Nearest,
            QOpenGLTexture.Filter.Nearest,
        )
        texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
        texture.setSize(window.channels, window.record_count)
        if window.components == 1:
            texture.setFormat(QOpenGLTexture.TextureFormat.R32F)
            pixel_format = QOpenGLTexture.PixelFormat.Red
        elif window.components == 2:
            texture.setFormat(QOpenGLTexture.TextureFormat.RG32F)
            pixel_format = QOpenGLTexture.PixelFormat.RG
        else:
            raise ValueError(
                f"unexpected source PCM component count {window.components}"
            )
        texture.allocateStorage(pixel_format, QOpenGLTexture.PixelType.Float32)
        texture.setData(pixel_format, QOpenGLTexture.PixelType.Float32, payload)
        return texture

    def _source_upload_for_view(self) -> PcmUploadWindow | None:
        if self.pcm_loader is None:
            return None
        levels = self.gpu.levels("waveform")
        if not levels:
            return None
        fine_division = min(max(1, int(level[0])) for level in levels)
        plan = self.pcm_loader.plan(
            self.view_start,
            self.view_end,
            max(1, self.width()),
            self.total_frames,
            fine_division,
        )
        window = self.pcm_loader.ensure(plan)
        if not plan.active or plan.key is None or window is None:
            return None
        if self._pcm_upload is not None and self._pcm_upload.key == plan.key:
            return self._pcm_upload
        upload_start = time.perf_counter_ns()
        texture = self._upload_pcm_texture(window)
        if self._pcm_upload is not None:
            self._pcm_upload.texture.destroy()
        self._pcm_upload = PcmUploadWindow(
            plan.key,
            window.first_frame,
            window.frame_count,
            window.division,
            window.record_count,
            window.mode,
            window.backend,
            texture,
            window.byte_count,
        )
        self._upload_ms = (time.perf_counter_ns() - upload_start) / 1e6
        self._upload_bytes_total += window.byte_count
        self._upload_count += 1
        return self._pcm_upload

    def initializeGL(self):  # noqa: N802
        self._gl = self.context().functions()
        self._program = QOpenGLShaderProgram(self)
        if not self._program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Vertex,
            VERTEX_SHADER,
        ):
            raise RuntimeError(self._program.log())
        if not self._program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Fragment,
            FRAGMENT_SHADER,
        ):
            raise RuntimeError(self._program.log())
        if not self._program.link():
            raise RuntimeError(self._program.log())

        self._vao = QOpenGLVertexArrayObject(self)
        if not self._vao.create():
            raise RuntimeError("cannot create OpenGL core-profile VAO")

        self._gpu_query = QOpenGLTimerQuery(self)
        if not self._gpu_query.create():
            self._gpu_query = None
        self.context().aboutToBeDestroyed.connect(self.cleanup)

    def cleanup(self):
        if self.context() is None:
            return
        self.makeCurrent()
        for upload in self._uploads.values():
            upload.texture.destroy()
        self._uploads.clear()
        if self._pcm_upload is not None:
            self._pcm_upload.texture.destroy()
            self._pcm_upload = None
        if self._vao is not None and self._vao.isCreated():
            self._vao.destroy()
        if self._gpu_query is not None and self._gpu_query.isCreated():
            self._gpu_query.destroy()
        self.doneCurrent()

    def _set_window_uniforms(
        self,
        has_name: str,
        short: str,
        window: UploadWindow | None,
    ) -> None:
        assert self._program is not None
        if window is None:
            self._program.setUniformValue(f"u_has{has_name}", 0)
            self._program.setUniformValue(f"u_{short}Count", 0)
            self._program.setUniformValue(f"u_{short}Record0", 0.0)
            self._program.setUniformValue(f"u_{short}RecordsAcross", 0.0)
            return
        self._program.setUniformValue(f"u_has{has_name}", 1)
        record_at_view = (
            self.view_start / max(1, window.division) - window.first_record
        )
        across = (self.view_end - self.view_start) / max(1, window.division)
        self._program.setUniformValue(f"u_{short}Record0", float(record_at_view))
        self._program.setUniformValue(f"u_{short}RecordsAcross", float(across))
        self._program.setUniformValue(f"u_{short}Count", int(window.record_count))

    def _resident_uniform(self, kind: str) -> QVector2D:
        window = self._uploads.get(kind)
        if window is None:
            return QVector2D(0.0, 0.0)
        start, end = window.normalized_range(self.total_frames)
        return QVector2D(float(start), float(end))

    def paintGL(self):  # noqa: N802
        start_ns = time.perf_counter_ns()
        assert self._gl is not None and self._program is not None
        assert self._vao is not None

        if self._gpu_query_pending and self._gpu_query is not None:
            if self._gpu_query.isResultAvailable():
                self._gpu_ms = self._gpu_query.waitForResult() / 1e6
                self._gpu_query_pending = False

        span = max(1, self.view_end - self.view_start)
        desired = span / max(1, self.width())
        pcm_upload = self._source_upload_for_view()
        pcm_draw = None
        if (
            pcm_upload is not None
            and self.pcm_loader is not None
            and self.pcm_loader.ready_window is not None
        ):
            pcm_draw = plan_pcm_draw(
                self.pcm_loader.ready_window,
                self.view_start,
                self.view_end,
                max(1, self.width()),
            )
        wave_choice = (
            None
            if pcm_upload is not None
            else self._nearest_level(self.gpu.levels("waveform"), desired)
        )
        wave = None
        target_division = desired
        if wave_choice is not None:
            wave_index, (division, total, _bpc) = wave_choice
            target_division = int(division)
            wave = self._window_for(
                "waveform",
                wave_index,
                int(division),
                int(total),
            )

        spectral = None
        if pcm_upload is None and self.show_spectral:
            choice = self._nearest_level(
                self.gpu.levels("spectral"),
                target_division,
            )
            if choice is not None:
                index, (division, total, _bpc) = choice
                spectral = self._window_for(
                    "spectral",
                    index,
                    int(division),
                    int(total),
                )

        spectrogram = None
        choice = (
            None
            if pcm_upload is not None
            else self._nearest_level(
                self.gpu.levels("spectrogram"),
                target_division,
            )
        )
        if choice is not None:
            index, (division, total, _bpc) = choice
            spectrogram = self._window_for(
                "spectrogram",
                index,
                int(division),
                int(total),
            )

        loudness = None
        if pcm_upload is None and self.show_loudness:
            choice = self._nearest_level(
                self.gpu.levels("loudness"),
                target_division,
            )
            if choice is not None:
                index, (division, total, _bpc) = choice
                loudness = self._window_for(
                    "loudness",
                    index,
                    int(division),
                    int(total),
                )

        self._gl.glClearColor(0.02, 0.025, 0.035, 1.0)
        self._gl.glClear(0x00004000)
        self._program.bind()
        self._vao.bind()

        self._program.setUniformValue("u_channels", int(self.gpu.channels))
        self._program.setUniformValue(
            "u_waveEncoding",
            0 if self.gpu.wave_encoding == "RPKN" else 1,
        )
        self._program.setUniformValue(
            "u_verticalFs",
            float(self.vertical_full_scale),
        )
        self._program.setUniformValue(
            "u_specGain",
            float(self.spectrogram_gain),
        )
        self._program.setUniformValue("u_heatmap", 1 if self.heatmap else 0)
        self._program.setUniformValue(
            "u_nyquist",
            float(self.gpu.sample_rate) * 0.5,
        )
        playhead_ratio = (self.playhead - self.view_start) / span
        self._program.setUniformValue("u_playhead", float(playhead_ratio))

        total = max(1, self.total_frames)
        self._program.setUniformValue(
            "u_tileDebug",
            1 if self.show_tile_debug else 0,
        )
        self._program.setUniformValue(
            "u_viewGlobal",
            QVector2D(self.view_start / total, self.view_end / total),
        )
        self._program.setUniformValue(
            "u_waveResident",
            self._resident_uniform("waveform"),
        )
        self._program.setUniformValue(
            "u_sResident",
            self._resident_uniform("spectral"),
        )
        self._program.setUniformValue(
            "u_gResident",
            self._resident_uniform("spectrogram"),
        )
        self._program.setUniformValue(
            "u_rResident",
            self._resident_uniform("loudness"),
        )
        if self._pcm_upload is None:
            pcm_resident = QVector2D(0.0, 0.0)
        else:
            pcm_first, pcm_last = self._pcm_upload.normalized_range(
                self.total_frames
            )
            pcm_resident = QVector2D(float(pcm_first), float(pcm_last))
        self._program.setUniformValue("u_pcmResident", pcm_resident)

        bindings = [
            (wave, "u_wave", 0),
            (spectral, "u_spectral", 1),
            (spectrogram, "u_g", 2),
            (loudness, "u_loudness", 3),
        ]
        for window, uniform, unit in bindings:
            if window is not None:
                window.texture.bind(unit)
            self._program.setUniformValue(uniform, unit)
        if pcm_upload is not None:
            pcm_upload.texture.bind(4)
        self._program.setUniformValue("u_pcm", 4)

        self._set_window_uniforms("Wave", "wave", wave)
        self._set_window_uniforms("Spectral", "s", spectral)
        self._set_window_uniforms("G", "g", spectrogram)
        self._set_window_uniforms("Loudness", "r", loudness)
        if pcm_upload is None or pcm_draw is None:
            self._program.setUniformValue("u_pcmMode", 0)
            self._program.setUniformValue("u_pcmRecord0", 0.0)
            self._program.setUniformValue("u_pcmRecordsAcross", 0.0)
            self._program.setUniformValue("u_pcmCount", 0)
            self._program.setUniformValue("u_pcmPixelsPerFrame", 0.0)
            self._program.setUniformValue("u_pcmDrawPoints", 0)
        else:
            self._program.setUniformValue(
                "u_pcmMode", 2 if pcm_upload.mode == "samples" else 1
            )
            self._program.setUniformValue(
                "u_pcmRecord0",
                float(pcm_draw.record0),
            )
            self._program.setUniformValue(
                "u_pcmRecordsAcross",
                float(pcm_draw.records_across),
            )
            self._program.setUniformValue(
                "u_pcmCount", int(pcm_upload.record_count)
            )
            self._program.setUniformValue(
                "u_pcmPixelsPerFrame", float(pcm_draw.pixels_per_frame)
            )
            self._program.setUniformValue(
                "u_pcmDrawPoints", 1 if pcm_draw.draw_points else 0
            )

        if self._gpu_query is not None and not self._gpu_query_pending:
            self._gpu_query.begin()
            self._gl.glDrawArrays(0x0004, 0, 3)
            self._gpu_query.end()
            self._gpu_query_pending = True
        else:
            self._gl.glDrawArrays(0x0004, 0, 3)

        for window, _uniform, unit in bindings:
            if window is not None:
                window.texture.release(unit)
        if pcm_upload is not None:
            pcm_upload.texture.release(4)
        self._vao.release()
        self._program.release()

        self._cpu_ms = (time.perf_counter_ns() - start_ns) / 1e6
        self._frame_count += 1
        windows = ", ".join(
            f"{kind}:{value.layer_index}@{value.first_record}+{value.record_count}"
            for kind, value in self._uploads.items()
        )
        mib = self._upload_bytes_total / (1024.0 * 1024.0)
        source = (
            self.pcm_loader.diagnostics()
            if self.pcm_loader is not None
            else "PCM disabled"
        )
        self.diagnostics = (
            f"GLSL {'source' if pcm_upload is not None else 'packed'} "
            f"cpu={self._cpu_ms:.3f}ms gpu={self._gpu_ms:.3f}ms | "
            f"last read={self._read_ms:.3f}ms upload={self._upload_ms:.3f}ms | "
            f"uploads={self._upload_count} {mib:.2f}MiB | resident [{windows}] | "
            f"{source}"
        )
