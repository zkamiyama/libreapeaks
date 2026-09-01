"""Headless-friendly smoke/telemetry benchmark for GpuAnalysisCanvas.

Run under a real display or Xvfb/Mesa. The reported llvmpipe GPU time is only a
regression signal; production recommendations should be based on the raw data
path plus measurements on the target GPU.
"""
from __future__ import annotations

import array
from pathlib import Path
import sys
import tempfile
import time

from PySide6.QtWidgets import QApplication

import reapeaks
from pyside6_gl_view import GpuAnalysisCanvas


def make_cache(path: Path, seconds: int = 4) -> int:
    sample_rate = 48_000
    channels = 2
    frames = sample_rate * seconds + 137
    pcm = array.array("h")
    for frame in range(frames):
        pcm.append(((frame * 97) % 65_535) - 32_768)
        pcm.append(((frame * 313) % 65_535) - 32_768)
    if sys.byteorder != "little":
        pcm.byteswap()
    blob = reapeaks.generate_pcm16_reaper(
        pcm.tobytes(),
        sample_rate=sample_rate,
        channels=channels,
        divisions=reapeaks.default_divisions(sample_rate, 300),
        mode="spectrogram",
    )
    path.write_bytes(bytes(blob))
    return frames


def main() -> int:
    app = QApplication([sys.argv[0]])
    with tempfile.TemporaryDirectory(prefix="libreapeaks-gl-bench-") as directory:
        cache = Path(directory) / "fixture.reapeaks"
        total_frames = make_cache(cache)
        widget = GpuAnalysisCanvas(str(cache), total_frames)
        widget.resize(1280, 640)
        widget.show()
        for _ in range(8):
            app.processEvents()
            time.sleep(0.01)
        if not widget.isValid():
            raise RuntimeError("QOpenGLWidget failed to create a valid OpenGL context")

        spans = [total_frames, 48_000 * 10, 48_000 * 2, 48_000 // 2]
        for index in range(32):
            span = min(total_frames, spans[index % len(spans)])
            maximum_start = max(0, total_frames - span)
            start = (maximum_start * index) // 31 if maximum_start else 0
            widget.set_view(start, start + span)
            widget.set_spectrogram_gain(0.6 + (index % 8) * 0.35)
            widget.set_playhead(start + span // 2)
            app.processEvents()
            widget.grabFramebuffer()
            app.processEvents()

        print("GLSL_RENDER_BENCH " + widget.diagnostics)
        widget.close()
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
