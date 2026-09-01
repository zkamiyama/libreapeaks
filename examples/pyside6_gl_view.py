"""Direct `.reapeaks` -> OpenGL renderer for the PySide6 demo.

The important property of this path is that it does not build display RGBA on
CPU. Waveform and `-'s'` bytes are uploaded as RGBA8UI, packed `-'g'` is uploaded
verbatim as R8UI, and `-'r'` energy pairs are uploaded as RG32F. GLSL performs
all byte decoding, 12-bit spectrogram unpacking, gain/palette transforms and
analysis overlays at draw time.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture, QOpenGLTimerQuery
from PySide6.QtOpenGLWidgets import QOpenGLWidget

import reapeaks


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
uniform float u_nyquist;

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
    float amp = mag <= 24576.0 ? mag / 24576.0 : exp2((mag - 24576.0) / 1024.0);
    return neg ? -amp : amp;
}

int recordAt(float record0, float across, int count) {
    return clamp(int(floor(record0 + v_uv.x * across)), 0, max(0, count - 1));
}

vec3 heat(float t) {
    t = clamp(t, 0.0, 1.0);
    if (u_heatmap == 0) {
        return vec3(t);
    }
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
    return (bin & 1) == 0 ? ((b0 << 4u) | (b1 >> 4u))
                          : ((b2 << 4u) | (b1 & 15u));
}

void main() {
    float lane = (1.0 - clamp(v_uv.y, 0.0, 0.999999)) * float(max(1, u_channels));
    int channel = clamp(int(floor(lane)), 0, max(0, u_channels - 1));
    float localY = fract(lane);

    vec3 color = vec3(0.025, 0.032, 0.045);

    if (u_hasG != 0 && u_gCount > 0) {
        int record = recordAt(u_gRecord0, u_gRecordsAcross, u_gCount);
        int bin = clamp(int(floor((1.0 - localY) * 128.0)), 0, 127);
        float intensity = clamp(float(unpackG(record, channel, bin)) / 4095.0 * u_specGain, 0.0, 1.0);
        color = mix(color, heat(intensity), 0.92);
    }

    if (u_hasSpectral != 0 && u_sCount > 0) {
        int record = recordAt(u_sRecord0, u_sRecordsAcross, u_sCount);
        uvec4 bytes = texelFetch(u_spectral, ivec2(channel, record), 0);
        uint code = bytes.r | (bytes.g << 8u) | (bytes.b << 16u) | (bytes.a << 24u);
        float frequency = float(code & 0x7fffu);
        float density = float((code >> 15u) & 0x3fffu) / 16383.0;
        if (frequency > 0.0) {
            float target = 1.0 - clamp(frequency / max(1.0, u_nyquist), 0.0, 1.0);
            float alpha = (1.0 - smoothstep(0.002, 0.012, abs(localY - target))) * (0.25 + 0.75 * density);
            color = mix(color, vec3(0.35, 0.72, 1.0), alpha);
        }
    }

    if (u_hasWave != 0 && u_waveCount > 0) {
        int record = recordAt(u_waveRecord0, u_waveRecordsAcross, u_waveCount);
        uvec4 bytes = texelFetch(u_wave, ivec2(channel, record), 0);
        float mx = decodeWave(s16(bytes.r, bytes.g));
        float mn = decodeWave(s16(bytes.b, bytes.a));
        float amplitudeAtPixel = (0.5 - localY) * 2.0 * u_verticalFs;
        float aa = max(fwidth(amplitudeAtPixel) * 1.5, 0.001);
        float inside = smoothstep(mn - aa, mn + aa, amplitudeAtPixel) *
                       (1.0 - smoothstep(mx - aa, mx + aa, amplitudeAtPixel));
        color = mix(color, vec3(0.43, 0.92, 0.67), inside);
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


class GpuAnalysisCanvas(QOpenGLWidget):
    """REAPER-like composite analysis display driven by raw `.reapeaks` bytes."""

    viewChanged = Signal(int, int)
    seekRequested = Signal(int)
    PAGE_RECORDS = 512

    def __init__(self, peaks_path: str, total_frames: int, parent=None):
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
        self.drag_last_x: float | None = None
        self.drag_distance = 0.0
        self._program: QOpenGLShaderProgram | None = None
        self._gl = None
        self._uploads: dict[str, UploadWindow] = {}
        self._dummy: dict[str, QOpenGLTexture] = {}
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
            self.set_view(self.view_start, min(self.view_end, self.total_frames), emit=False)

    def set_view(self, start: int, end: int, *, emit: bool = True):
        minimum_span = max(64, self.gpu.sample_rate // 50)
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
        self.vertical_full_scale = max(0.05, float(value))
        self.update()

    def set_spectrogram_gain(self, value: float):
        self.spectrogram_gain = max(0.01, float(value))
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

    def set_tile_debug(self, _enabled: bool):
        # Direct-GL diagnostics are reported as upload windows instead of
        # QPainter tile bands. Keep the method for backend interchangeability.
        self.update()

    def zoom(self, factor: float, anchor_ratio: float = 0.5):
        span = self.view_end - self.view_start
        new_span = max(
            max(64, self.gpu.sample_rate // 50),
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
                math.log(max(1.0, float(item[1][0])) / max(1.0, desired_division))
            ),
        )

    def _window_for(self, kind: str, layer_index: int, division: int, total: int):
        first_needed = max(0, self.view_start // max(1, division) - 2)
        last_needed = min(total, (self.view_end + division - 1) // max(1, division) + 3)
        page = self.PAGE_RECORDS
        first = (first_needed // page) * page
        minimum_end = min(total, first + page * 2)
        last = min(total, max(minimum_end, ((last_needed + page - 1) // page) * page))
        count = max(1, last - first)
        current = self._uploads.get(kind)
        if (
            current is not None
            and current.layer_index == layer_index
            and current.first_record == first
            and current.record_count == count
        ):
            return current

        t0 = time.perf_counter_ns()
        first_actual, records, channels, bytes_per_channel, raw = self.gpu.records(
            kind, layer_index, first, count
        )
        payload = bytes(raw)
        t1 = time.perf_counter_ns()
        texture = self._upload_texture(kind, records, channels, bytes_per_channel, payload)
        t2 = time.perf_counter_ns()
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
        self._read_ms = (t1 - t0) / 1e6
        self._upload_ms = (t2 - t1) / 1e6
        self._upload_bytes_total += len(payload)
        self._upload_count += 1
        return window

    def _upload_texture(
        self,
        kind: str,
        records: int,
        channels: int,
        bytes_per_channel: int,
        payload: bytes,
    ) -> QOpenGLTexture:
        texture = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
        texture.setMipLevels(1)
        texture.setMinMagFilters(QOpenGLTexture.Filter.Nearest, QOpenGLTexture.Filter.Nearest)
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
                raise ValueError(f"unexpected spectrogram record size {bytes_per_channel}")
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
                raise ValueError(f"unexpected loudness record size {bytes_per_channel}")
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

    def initializeGL(self):  # noqa: N802
        self._gl = self.context().functions()
        self._program = QOpenGLShaderProgram(self)
        if not self._program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Vertex, VERTEX_SHADER
        ):
            raise RuntimeError(self._program.log())
        if not self._program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT_SHADER
        ):
            raise RuntimeError(self._program.log())
        if not self._program.link():
            raise RuntimeError(self._program.log())
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
        for texture in self._dummy.values():
            texture.destroy()
        self._dummy.clear()
        if self._gpu_query is not None and self._gpu_query.isCreated():
            self._gpu_query.destroy()
        self.doneCurrent()

    def _set_window_uniforms(self, prefix: str, window: UploadWindow | None):
        assert self._program is not None
        if window is None:
            self._program.setUniformValue(f"u_has{prefix}", 0)
            self._program.setUniformValue(f"u_{prefix.lower()}Count", 0)
            return
        self._program.setUniformValue(f"u_has{prefix}", 1)
        short = {"Wave": "wave", "Spectral": "s", "G": "g", "Loudness": "r"}[prefix]
        record_at_view = self.view_start / max(1, window.division) - window.first_record
        across = (self.view_end - self.view_start) / max(1, window.division)
        self._program.setUniformValue(f"u_{short}Record0", float(record_at_view))
        self._program.setUniformValue(f"u_{short}RecordsAcross", float(across))
        self._program.setUniformValue(f"u_{short}Count", int(window.record_count))

    def paintGL(self):  # noqa: N802
        start_ns = time.perf_counter_ns()
        assert self._gl is not None and self._program is not None
        if self._gpu_query_pending and self._gpu_query is not None:
            if self._gpu_query.isResultAvailable():
                self._gpu_ms = self._gpu_query.waitForResult() / 1e6
                self._gpu_query_pending = False

        span = max(1, self.view_end - self.view_start)
        desired = span / max(1, self.width())
        wave_choice = self._nearest_level(self.gpu.levels("waveform"), desired)
        wave = None
        target_division = desired
        if wave_choice is not None:
            wave_index, (wave_division, wave_total, _bpc) = wave_choice
            target_division = wave_division
            wave = self._window_for(
                "waveform", wave_index, int(wave_division), int(wave_total)
            )

        spectral = None
        if self.show_spectral:
            choice = self._nearest_level(self.gpu.levels("spectral"), target_division)
            if choice is not None:
                index, (division, total, _bpc) = choice
                spectral = self._window_for("spectral", index, int(division), int(total))

        spectrogram = None
        choice = self._nearest_level(self.gpu.levels("spectrogram"), target_division)
        if choice is not None:
            index, (division, total, _bpc) = choice
            spectrogram = self._window_for(
                "spectrogram", index, int(division), int(total)
            )

        loudness = None
        if self.show_loudness:
            choice = self._nearest_level(self.gpu.levels("loudness"), target_division)
            if choice is not None:
                index, (division, total, _bpc) = choice
                loudness = self._window_for("loudness", index, int(division), int(total))

        self._gl.glClearColor(0.02, 0.025, 0.035, 1.0)
        self._gl.glClear(0x00004000)  # GL_COLOR_BUFFER_BIT
        self._program.bind()
        self._program.setUniformValue("u_channels", int(self.gpu.channels))
        self._program.setUniformValue("u_waveEncoding", 0)  # generated demo spectrograms are RPKN
        self._program.setUniformValue("u_verticalFs", float(self.vertical_full_scale))
        self._program.setUniformValue("u_specGain", float(self.spectrogram_gain))
        self._program.setUniformValue("u_heatmap", 1 if self.heatmap else 0)
        self._program.setUniformValue("u_nyquist", float(self.gpu.sample_rate) * 0.5)

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

        self._set_window_uniforms("Wave", wave)
        self._set_window_uniforms("Spectral", spectral)
        self._set_window_uniforms("G", spectrogram)
        self._set_window_uniforms("Loudness", loudness)

        if self._gpu_query is not None and not self._gpu_query_pending:
            self._gpu_query.begin()
            self._gl.glDrawArrays(0x0004, 0, 3)  # GL_TRIANGLES
            self._gpu_query.end()
            self._gpu_query_pending = True
        else:
            self._gl.glDrawArrays(0x0004, 0, 3)

        for window, _uniform, unit in bindings:
            if window is not None:
                window.texture.release(unit)
        self._program.release()

        self._cpu_ms = (time.perf_counter_ns() - start_ns) / 1e6
        self._frame_count += 1
        windows = ", ".join(
            f"{kind}:{value.layer_index}@{value.first_record}+{value.record_count}"
            for kind, value in self._uploads.items()
        )
        mib = self._upload_bytes_total / (1024.0 * 1024.0)
        self.diagnostics = (
            f"GLSL packed path cpu={self._cpu_ms:.3f}ms gpu={self._gpu_ms:.3f}ms | "
            f"last read={self._read_ms:.3f}ms upload={self._upload_ms:.3f}ms | "
            f"uploads={self._upload_count} {mib:.2f}MiB | {windows}"
        )
