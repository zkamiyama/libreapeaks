"""PySide6 reference audio player for libreapeaks.

Run:
    python examples/pyside6_player.py /path/to/audio.wav
    python examples/pyside6_player.py /path/to/audio.wav --peaks /path/to/file.reapeaks
    python examples/pyside6_player.py /path/to/audio.wav --rebuild-cache

The canvas uses plan_view -> tiles_for_view -> tile_texture and the matching
spectral tile API. Tile IDs and LRU statistics are shown on screen.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDoubleSpinBox, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

import reapeaks
from player_common import ensure_reapeaks, exact_audio_frames, format_time
from pyside6_views import OverviewWidget, PeaksCanvas

class PlayerWindow(QMainWindow):
    def __init__(self, audio_path: Path, peaks_path: Path, generated: bool):
        super().__init__()
        self.audio_path = audio_path
        self.peaks_path = peaks_path
        self.rp = reapeaks.ReaPeaks.open(str(peaks_path))
        levels = self.rp.levels()
        estimated_frames = max(1, levels[0][0] * levels[0][1])
        self.total_frames = exact_audio_frames(audio_path, self.rp.sample_rate) or estimated_frames
        self.follow = True

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.8)
        self.player.setSource(QUrl.fromLocalFile(str(audio_path)))

        self.canvas = PeaksCanvas(self.rp, self.total_frames)
        self.canvas.seekRequested.connect(self.seek_frame)
        self.canvas.viewChanged.connect(self._view_changed)

        overview_raw = self.rp.render_rgba(
            1200,
            84,
            0,
            self.total_frames,
            background=(17, 20, 26, 255),
            waveform=(110, 218, 164, 255),
        )
        overview_image = QImage(
            bytes(overview_raw), 1200, 84, 1200 * 4, QImage.Format.Format_RGBA8888
        ).copy()
        self.overview = OverviewWidget(overview_image, self.total_frames)
        self.overview.seekRequested.connect(self.seek_frame)

        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.toggle_play)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop)
        self.zoom_in = QPushButton("Zoom +")
        self.zoom_in.clicked.connect(lambda: self.canvas.zoom(0.5))
        self.zoom_out = QPushButton("Zoom −")
        self.zoom_out.clicked.connect(lambda: self.canvas.zoom(2.0))
        self.tiles_checkbox = QCheckBox("Show tile boundaries")
        self.tiles_checkbox.setChecked(True)
        self.tiles_checkbox.toggled.connect(self.canvas.set_tile_debug)
        self.follow_checkbox = QCheckBox("Follow playhead")
        self.follow_checkbox.setChecked(True)
        self.follow_checkbox.toggled.connect(lambda value: setattr(self, "follow", value))
        self.vertical_scale = QDoubleSpinBox()
        self.vertical_scale.setRange(0.1, 32.0)
        self.vertical_scale.setDecimals(2)
        self.vertical_scale.setSingleStep(0.25)
        self.vertical_scale.setValue(1.0)
        self.vertical_scale.valueChanged.connect(self.canvas.set_vertical_full_scale)

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 100_000)
        self.position_slider.sliderMoved.connect(self._slider_seek)
        self.time_label = QLabel("00:00.00 / 00:00.00")
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.api_label = QLabel()
        self.api_label.setWordWrap(True)

        controls = QHBoxLayout()
        for widget in (
            self.play_button,
            self.stop_button,
            self.zoom_in,
            self.zoom_out,
            self.tiles_checkbox,
            self.follow_checkbox,
        ):
            controls.addWidget(widget)
        controls.addWidget(QLabel("Vertical FS"))
        controls.addWidget(self.vertical_scale)
        controls.addStretch(1)
        controls.addWidget(self.time_label)

        layout = QVBoxLayout()
        layout.addWidget(self.overview)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(controls)
        layout.addWidget(self.position_slider)
        layout.addWidget(self.status_label)
        layout.addWidget(self.api_label)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
        self.resize(1320, 760)
        self.setWindowTitle(f"libreapeaks tiled player — {audio_path.name}")

        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.errorOccurred.connect(lambda _err, text: self.statusBar().showMessage(text))

        # Exercise the complete-level data texture API only on the coarsest
        # level so this remains bounded even for long recordings.
        coarsest = len(levels) - 1
        env_w, env_h, env_raw = self.rp.envelope_texture(coarsest)
        defaults = reapeaks.default_divisions(self.rp.sample_rate)
        native_count = sum(1 for _d, _n, native in levels if native)
        derived_count = len(levels) - native_count
        self.api_label.setText(
            f"cache={peaks_path.name}{' (generated by libreapeaks)' if generated else ''} | "
            f"encoding={self.rp.wave_encoding} channels={self.rp.channels} sr={self.rp.sample_rate} | "
            f"tile_peaks={self.rp.tile_peaks} | levels={len(levels)} "
            f"(native={native_count}, lazy-derived={derived_count}) | "
            f"default_divisions={defaults} | coarsest envelope_texture={env_w}×{env_h} "
            f"({len(bytes(env_raw))} RGBA8 bytes)"
        )

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._refresh_status)
        self.ui_timer.start(150)
        self._view_changed(self.canvas.view_start, self.canvas.view_end)
        self._position_changed(0)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self):
        self.player.stop()
        self.seek_frame(0)

    def seek_frame(self, frame: int):
        ms = int(frame * 1000 / max(1, self.rp.sample_rate))
        self.player.setPosition(ms)

    def _slider_seek(self, value: int):
        self.seek_frame(int(value / 100_000 * self.total_frames))

    def _playback_state_changed(self, state):
        self.play_button.setText(
            "Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play"
        )

    def _duration_changed(self, duration_ms: int):
        if duration_ms <= 0:
            return
        duration_frames = int(duration_ms * self.rp.sample_rate / 1000)
        exact = exact_audio_frames(self.audio_path, self.rp.sample_rate)
        self.total_frames = exact or max(1, duration_frames)
        self.canvas.set_total_frames(self.total_frames)
        self.overview.total_frames = self.total_frames

    def _position_changed(self, position_ms: int):
        frame = int(position_ms * self.rp.sample_rate / 1000)
        self.canvas.set_playhead(frame)
        if self.follow and not (self.canvas.view_start <= frame <= self.canvas.view_end):
            span = self.canvas.view_end - self.canvas.view_start
            self.canvas.set_view(frame - span // 4, frame + 3 * span // 4)
        self.position_slider.blockSignals(True)
        self.position_slider.setValue(int(min(1.0, frame / max(1, self.total_frames)) * 100_000))
        self.position_slider.blockSignals(False)
        self.time_label.setText(
            f"{format_time(position_ms / 1000)} / "
            f"{format_time(self.total_frames / self.rp.sample_rate)}"
        )
        self.overview.set_state(
            self.canvas.view_start, self.canvas.view_end, frame, self.total_frames
        )

    def _view_changed(self, start: int, end: int):
        self.overview.set_state(start, end, self.canvas.playhead, self.total_frames)

    def _refresh_status(self):
        span_s = (self.canvas.view_end - self.canvas.view_start) / self.rp.sample_rate
        self.status_label.setText(
            f"viewport={format_time(self.canvas.view_start / self.rp.sample_rate)}…"
            f"{format_time(self.canvas.view_end / self.rp.sample_rate)} ({span_s:.3f}s) | "
            + self.canvas.diagnostics
        )


def parse_args(argv: list[str]):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--peaks", type=Path, help="existing or target .reapeaks path")
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="rebuild PCM16/float32 WAV cache with libreapeaks before opening",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    audio = args.audio.resolve()
    peaks, generated = ensure_reapeaks(
        audio, args.peaks, rebuild=args.rebuild_cache, spectral=True
    )
    app = QApplication(sys.argv)
    window = PlayerWindow(audio, peaks, generated)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
