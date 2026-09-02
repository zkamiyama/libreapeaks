"""Headless-friendly smoke/telemetry benchmark for the Demo Player GLSL path.

Run under a real display or Xvfb/Mesa. The reported llvmpipe GPU time is only a
regression signal; production recommendations should be based on the raw data
path plus measurements on the target GPU.
"""
from __future__ import annotations

import array
import faulthandler
from pathlib import Path
import sys
import tempfile
import time
import wave

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

import reapeaks
from pyside6_zita_gl_view import ZitaGpuAnalysisCanvas
from source_pcm import SourcePcmService, WavPcmWindowReader


# Keep this enabled in CI: if a Qt/OpenGL binding crashes inside native code,
# GitHub Actions still records the Python call site that entered the extension.
faulthandler.enable(all_threads=True)


def make_cache(path: Path, audio_path: Path, seconds: int = 4) -> int:
    sample_rate = 48_000
    channels = 2
    frames = sample_rate * seconds + 137
    pcm = array.array("h")
    for frame in range(frames):
        pcm.append(((frame * 97) % 65_535) - 32_768)
        pcm.append(((frame * 313) % 65_535) - 32_768)
    if sys.byteorder != "little":
        pcm.byteswap()
    pcm_bytes = pcm.tobytes()
    with wave.open(str(audio_path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm_bytes)
    blob = reapeaks.generate_pcm16_reaper(
        pcm_bytes,
        sample_rate=sample_rate,
        channels=channels,
        divisions=reapeaks.default_divisions(sample_rate, 300),
        mode="spectrogram",
    )
    path.write_bytes(bytes(blob))
    return frames


def _wait_for_pcm_mode(app: QApplication, widget: ZitaGpuAnalysisCanvas, mode: str) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        app.processEvents()
        widget.grabFramebuffer()
        if widget._pcm_upload is not None and widget._pcm_upload.mode == mode:
            return
        time.sleep(0.01)
    raise RuntimeError(
        f"source PCM {mode} texture did not become ready: {widget.diagnostics}"
    )


def main() -> int:
    app = QApplication([sys.argv[0]])
    with tempfile.TemporaryDirectory(prefix="libreapeaks-gl-bench-") as directory:
        cache = Path(directory) / "fixture.reapeaks"
        audio = Path(directory) / "fixture.wav"
        total_frames = make_cache(cache, audio)
        pcm_service = SourcePcmService(
            WavPcmWindowReader(audio),
            expected_sample_rate=48_000,
            expected_channels=2,
        )
        widget = ZitaGpuAnalysisCanvas(
            str(cache), total_frames, pcm_service=pcm_service
        )
        decoded_events: list[object] = []
        assert widget.pcm_loader is not None
        widget.pcm_loader.rangeDecoded.connect(decoded_events.append)
        # This benchmark creates a parentless top-level QOpenGLWidget. Ensure it
        # and its GL context die while QApplication is still fully alive rather
        # than leaving Qt to tear them down during Python interpreter shutdown.
        widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        widget.resize(1280, 640)
        widget.show()
        for _ in range(8):
            app.processEvents()
            time.sleep(0.01)
        if not widget.isValid():
            raise RuntimeError("QOpenGLWidget failed to create a valid OpenGL context")

        spans = [total_frames, 48_000 * 2, 48_000 // 2, 1200]
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

        # Exercise zita-style per-screen-column min/max geometry at the source
        # envelope LOD, not just shader compilation and packed-cache views.
        widget.set_view(1000, 97_000)
        _wait_for_pcm_mode(app, widget, "envelope")
        widget.grabFramebuffer()
        if widget._zita_values_window_id is None:
            raise RuntimeError("zita envelope painter did not consume source PCM")

        # Exercise the deep-zoom raw-sample polyline as a separate geometry
        # path. The fragment-distance PCM renderer is disabled by the zita
        # wrapper, so this also catches QPainter/OpenGL interop regressions.
        widget.set_view(1000, 1200)
        _wait_for_pcm_mode(app, widget, "samples")
        widget.grabFramebuffer()
        if widget._zita_values_window_id is None:
            raise RuntimeError("zita sample painter did not consume source PCM")
        if not decoded_events:
            raise RuntimeError("PySide6 emitted no PCM rangeDecoded debug signal")

        print("GLSL_RENDER_BENCH " + widget.diagnostics, flush=True)

        # QOpenGLWidget destruction on Mesa/PySide6 6.11 is safest when Qt GL
        # wrappers are destroyed while the context is unquestionably healthy.
        # Do that explicitly, then prevent the aboutToBeDestroyed callback from
        # trying to enter the already-cleaned widget a second time.
        context = widget.context()
        widget.cleanup()
        try:
            context.aboutToBeDestroyed.disconnect(widget.cleanup)
        except (RuntimeError, TypeError):
            pass
        widget.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()

    app.quit()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
