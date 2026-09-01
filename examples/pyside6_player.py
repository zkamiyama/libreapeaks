"""PySide6 reference audio player for libreapeaks.

Examples:
    python examples/pyside6_player.py /path/to/audio.wav
    python examples/pyside6_player.py song.mp3 --cache-decoder ffmpeg
    python examples/pyside6_player.py song.opus --cache-decoder ffmpeg \
        --playback-decoder ffmpeg --cache-mode central --cache-dir ~/.cache/libreapeaks
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import reapeaks
from player_common import (
    DEFAULT_DECODE_TIMEOUT,
    DEFAULT_MAX_DECODE_BYTES,
    PlayerCacheError,
    available_spectral_levels,
    exact_audio_frames,
    format_time,
    prepare_playback_audio,
)
from player_native_cache import NativeGenerationMode
from pyside6_prepare import CachePreparationDialog
from pyside6_views import OverviewWidget, PeaksCanvas


class PlayerWindow(QMainWindow):
    def __init__(
        self,
        audio_path: Path,
        playback_path: Path,
        peaks_path: Path,
        generated: bool,
        *,
        generation_mode: str,
        cache_decoder: str,
        playback_decoder: str,
    ):
        super().__init__()
        self.audio_path = audio_path
        self.playback_path = playback_path
        self.peaks_path = peaks_path
        self.generation_mode = generation_mode
        self.rp = reapeaks.ReaPeaks.open(str(peaks_path))
        levels = self.rp.levels()
        if not levels:
            raise PlayerCacheError(
                f"cache has no decodable RPKN/RPKL waveform layers: {peaks_path}"
            )
        estimated_frames = max(1, levels[0][0] * levels[0][1])
        self.total_frames = (
            exact_audio_frames(playback_path, self.rp.sample_rate)
            or exact_audio_frames(audio_path, self.rp.sample_rate)
            or estimated_frames
        )
        self.follow = True

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.8)
        self.player.setSource(QUrl.fromLocalFile(str(playback_path)))

        self.canvas = PeaksCanvas(self.rp, self.total_frames)
        # Only advertise spectral layers that are actually readable. Waveform-only
        # caches therefore show no spectral lane instead of probing indefinitely.
        self.canvas.native_levels = [
            (level_index, division, peak_count)
            for _layer, level_index, division, peak_count in available_spectral_levels(
                self.rp, levels
            )
        ]
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
        self.follow_checkbox.toggled.connect(
            lambda value: setattr(self, "follow", value)
        )
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
        self.player.errorOccurred.connect(
            lambda _error, text: self.statusBar().showMessage(text)
        )

        coarsest = len(levels) - 1
        env_w, env_h, env_raw = self.rp.envelope_texture(coarsest)
        defaults = reapeaks.default_divisions(self.rp.sample_rate)
        native_count = sum(1 for _division, _count, native in levels if native)
        derived_count = len(levels) - native_count
        self.api_label.setText(
            f"cache={peaks_path.name}"
            f"{' (generated by libreapeaks)' if generated else ' (reused)'} | "
            f"native_mode={generation_mode} | "
            f"cache_decoder={cache_decoder} playback_decoder={playback_decoder} | "
            f"encoding={self.rp.wave_encoding} channels={self.rp.channels} "
            f"sr={self.rp.sample_rate} | tile_peaks={self.rp.tile_peaks} | "
            f"levels={len(levels)} (native={native_count}, lazy-derived={derived_count}) | "
            f"default_divisions={defaults} | coarsest envelope_texture={env_w}×{env_h} "
            f"({len(bytes(env_raw))} RGBA8 bytes)"
        )

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._refresh_status)
        self.ui_timer.start(150)
        self._view_changed(self.canvas.view_start, self.canvas.view_end)
        self._position_changed(0)

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            # A click on the waveform/overview calls seek_frame(), so Play starts
            # exactly from the selected DAW-style timeline position.
            self.player.play()

    def stop(self) -> None:
        self.player.stop()
        self.seek_frame(0)

    def seek_frame(self, frame: int) -> None:
        frame = max(0, min(int(frame), self.total_frames))
        ms = int(frame * 1000 / max(1, self.rp.sample_rate))
        self.player.setPosition(ms)
        self.canvas.set_playhead(frame)
        self._view_changed(self.canvas.view_start, self.canvas.view_end)

    def _slider_seek(self, value: int) -> None:
        self.seek_frame(int(value / 100_000 * self.total_frames))

    def _playback_state_changed(self, state) -> None:
        self.play_button.setText(
            "Pause"
            if state == QMediaPlayer.PlaybackState.PlayingState
            else "Play"
        )

    def _duration_changed(self, duration_ms: int) -> None:
        if duration_ms <= 0:
            return
        duration_frames = int(duration_ms * self.rp.sample_rate / 1000)
        exact = exact_audio_frames(self.playback_path, self.rp.sample_rate)
        self.total_frames = exact or max(1, duration_frames)
        self.canvas.set_total_frames(self.total_frames)
        self.overview.total_frames = self.total_frames

    def _position_changed(self, position_ms: int) -> None:
        frame = int(position_ms * self.rp.sample_rate / 1000)
        self.canvas.set_playhead(frame)
        if self.follow and not (
            self.canvas.view_start <= frame <= self.canvas.view_end
        ):
            span = self.canvas.view_end - self.canvas.view_start
            self.canvas.set_view(frame - span // 4, frame + 3 * span // 4)
        self.position_slider.blockSignals(True)
        self.position_slider.setValue(
            int(min(1.0, frame / max(1, self.total_frames)) * 100_000)
        )
        self.position_slider.blockSignals(False)
        self.time_label.setText(
            f"{format_time(position_ms / 1000)} / "
            f"{format_time(self.total_frames / self.rp.sample_rate)}"
        )
        self.overview.set_state(
            self.canvas.view_start,
            self.canvas.view_end,
            frame,
            self.total_frames,
        )

    def _view_changed(self, start: int, end: int) -> None:
        self.overview.set_state(start, end, self.canvas.playhead, self.total_frames)

    def _refresh_status(self) -> None:
        span_seconds = (
            self.canvas.view_end - self.canvas.view_start
        ) / self.rp.sample_rate
        self.status_label.setText(
            f"viewport={format_time(self.canvas.view_start / self.rp.sample_rate)}…"
            f"{format_time(self.canvas.view_end / self.rp.sample_rate)} "
            f"({span_seconds:.3f}s) | {self.canvas.diagnostics}"
        )


def parse_divisions(value: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("divisions must be comma-separated integers") from exc
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("divisions must contain positive integers")
    return values


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--peaks", type=Path, help="existing or target .reapeaks path")
    parser.add_argument("--rebuild-cache", action="store_true")
    parser.add_argument(
        "--generation-mode",
        choices=("waveform", "spectral", "spectrogram"),
        default="spectral",
        help="initial value for the cache-data dropdown",
    )
    parser.add_argument(
        "--cache-decoder", choices=("auto", "wav", "ffmpeg"), default="auto"
    )
    parser.add_argument(
        "--playback-decoder", choices=("native", "ffmpeg"), default="native"
    )
    parser.add_argument(
        "--cache-mode",
        choices=("auto", "sidecar", "subdir", "central", "reaper"),
        default="auto",
    )
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--reaper-cache-map", type=Path)
    parser.add_argument("--allow-stale-cache", action="store_true")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--decode-timeout", type=float, default=DEFAULT_DECODE_TIMEOUT)
    parser.add_argument(
        "--max-decode-bytes", type=int, default=DEFAULT_MAX_DECODE_BYTES
    )
    parser.add_argument(
        "--wave-encoding", choices=("auto", "rpkn", "rpkl"), default="auto"
    )
    parser.add_argument("--divisions", type=parse_divisions)
    parser.add_argument("--fine-peaks-per-second", type=int, default=300)
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    audio = args.audio.expanduser().resolve(strict=False)
    prepared = None
    app = QApplication([sys.argv[0]])
    try:
        prepare_dialog = CachePreparationDialog(
            audio,
            initial_mode=args.generation_mode,  # type: ignore[arg-type]
            options={
                "peaks_path": args.peaks,
                "rebuild": args.rebuild_cache,
                "decoder": args.cache_decoder,
                "cache_mode": args.cache_mode,
                "cache_directory": args.cache_dir,
                "reaper_cache_map": args.reaper_cache_map,
                "allow_stale_cache": args.allow_stale_cache,
                "ffmpeg": args.ffmpeg,
                "ffprobe": args.ffprobe,
                "decode_timeout": args.decode_timeout,
                "max_decode_bytes": args.max_decode_bytes,
                "wave_encoding": args.wave_encoding,
                "divisions": args.divisions,
                "fine_peaks_per_second": args.fine_peaks_per_second,
                "lock_timeout": args.lock_timeout,
            },
        )
        if prepare_dialog.exec() != CachePreparationDialog.DialogCode.Accepted:
            return 1
        if prepare_dialog.peaks_path is None:
            raise PlayerCacheError("cache preparation completed without a cache path")

        prepared = prepare_playback_audio(
            audio,
            decoder=args.playback_decoder,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            timeout=args.decode_timeout,
            max_decode_bytes=args.max_decode_bytes,
        )
        window = PlayerWindow(
            audio,
            prepared.path,
            prepare_dialog.peaks_path,
            prepare_dialog.generated,
            generation_mode=prepare_dialog.generation_mode,
            cache_decoder=args.cache_decoder,
            playback_decoder=args.playback_decoder,
        )
        window.show()
        return app.exec()
    except PlayerCacheError as exc:
        print(f"pyside6_player: {exc}", file=sys.stderr)
        return 2
    finally:
        if prepared is not None:
            prepared.close()


if __name__ == "__main__":
    raise SystemExit(main())
