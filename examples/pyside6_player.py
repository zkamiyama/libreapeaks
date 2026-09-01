"""PySide6 reference audio player for libreapeaks.

Examples:
    python examples/pyside6_player.py /path/to/audio.wav
    python examples/pyside6_player.py song.mp3 --cache-decoder ffmpeg
    python examples/pyside6_player.py song.opus --cache-decoder ffmpeg \
        --playback-decoder ffmpeg --cache-mode central --cache-dir ~/.cache/libreapeaks

For a full waveform + spectral + spectrogram + loudness cache, ``--renderer
auto`` prefers the packed GLSL path. That path indexes raw `.reapeaks` payloads
without materializing every analysis layer on the CPU.
"""
from __future__ import annotations

import argparse
import math
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
from pyside6_gpu_overview import build_gpu_overview_image
from pyside6_prepare import CachePreparationDialog
from pyside6_reaper_gl_view import ReaperGpuAnalysisCanvas
from pyside6_views import OverviewWidget, PeaksCanvas
from source_pcm import (
    DEFAULT_PCM_CACHE_BYTES,
    DEFAULT_PCM_MAX_WINDOW_BYTES,
    DEFAULT_PCM_TARGET_PAGE_BYTES,
    PcmRangeEvent,
    SourcePcmService,
    open_pcm_window_reader,
)


class PlayerWindow(QMainWindow):
    def __init__(
        self,
        audio_path: Path,
        playback_path: Path,
        peaks_path: Path,
        generated: bool,
        *,
        generation_mode: str,
        render_backend: str,
        cache_decoder: str,
        playback_decoder: str,
        source_pcm_enabled: bool,
        pcm_decoder: str,
        ffmpeg: str,
        ffprobe: str,
        decode_timeout: float,
        pcm_cache_bytes: int,
        pcm_max_window_bytes: int,
        pcm_target_page_bytes: int,
    ):
        super().__init__()
        self.audio_path = audio_path
        self.playback_path = playback_path
        self.peaks_path = peaks_path
        self.generation_mode = generation_mode
        self.follow = True

        if render_backend not in ("auto", "glsl", "qpainter"):
            raise PlayerCacheError(f"unknown renderer: {render_backend}")
        self.render_backend = (
            "glsl"
            if render_backend == "auto" and generation_mode == "spectrogram"
            else "qpainter"
            if render_backend == "auto"
            else render_backend
        )

        self.rp = None
        self.gpu = None
        if self.render_backend == "glsl":
            try:
                self.gpu = reapeaks.GpuCacheView.open(str(peaks_path))
            except Exception as exc:
                if render_backend == "glsl":
                    raise PlayerCacheError(
                        f"cannot open packed GPU cache view: {exc}"
                    ) from exc
                self.render_backend = "qpainter"

        if self.render_backend == "glsl":
            assert self.gpu is not None
            waveform_levels = self.gpu.levels("waveform")
            if not waveform_levels:
                raise PlayerCacheError(
                    f"cache has no direct-GPU waveform layers: {peaks_path}"
                )
            self.sample_rate = int(self.gpu.sample_rate)
            self.channels = int(self.gpu.channels)
            self.wave_encoding = str(self.gpu.wave_encoding)
            estimated_frames = max(
                1, int(waveform_levels[0][0]) * int(waveform_levels[0][1])
            )
            self.total_frames = (
                exact_audio_frames(playback_path, self.sample_rate)
                or exact_audio_frames(audio_path, self.sample_rate)
                or estimated_frames
            )
            overview_image = build_gpu_overview_image(self.gpu, 1200, 84)
            native_count = len(waveform_levels)
            derived_count = 0
            qpainter_native_levels = []
        else:
            self.rp = reapeaks.ReaPeaks.open(str(peaks_path))
            levels = self.rp.levels()
            if not levels:
                raise PlayerCacheError(
                    f"cache has no decodable RPKN/RPKL waveform layers: {peaks_path}"
                )
            self.sample_rate = int(self.rp.sample_rate)
            self.channels = int(self.rp.channels)
            self.wave_encoding = str(self.rp.wave_encoding)
            estimated_frames = max(1, int(levels[0][0]) * int(levels[0][1]))
            self.total_frames = (
                exact_audio_frames(playback_path, self.sample_rate)
                or exact_audio_frames(audio_path, self.sample_rate)
                or estimated_frames
            )
            # Only advertise spectral layers that are actually readable.
            qpainter_native_levels = [
                (level_index, division, peak_count)
                for _layer, level_index, division, peak_count in available_spectral_levels(
                    self.rp, levels
                )
            ]
            overview_raw = self.rp.render_rgba(
                1200,
                84,
                0,
                self.total_frames,
                background=(17, 20, 26, 255),
                waveform=(110, 218, 164, 255),
            )
            overview_image = QImage(
                bytes(overview_raw),
                1200,
                84,
                1200 * 4,
                QImage.Format.Format_RGBA8888,
            ).copy()
            native_count = sum(1 for _division, _count, native in levels if native)
            derived_count = len(levels) - native_count

        self.source_pcm: SourcePcmService | None = None
        self.source_pcm_error = "disabled by --no-source-pcm"
        if source_pcm_enabled:
            pcm_path = playback_path if playback_decoder == "ffmpeg" else audio_path
            try:
                reader = open_pcm_window_reader(
                    pcm_path,
                    decoder=pcm_decoder,  # type: ignore[arg-type]
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    timeout=decode_timeout,
                    total_frames_hint=self.total_frames,
                )
                self.source_pcm = SourcePcmService(
                    reader,
                    cache_bytes=pcm_cache_bytes,
                    max_window_bytes=pcm_max_window_bytes,
                    target_page_bytes=pcm_target_page_bytes,
                    expected_sample_rate=self.sample_rate,
                    expected_channels=self.channels,
                )
                self.source_pcm_error = ""
            except Exception as exc:
                # Extreme zoom should degrade to the existing peak renderer,
                # not prevent playback when an optional decoder is missing.
                self.source_pcm_error = f"{type(exc).__name__}: {exc}"

        if self.render_backend == "glsl":
            self.canvas = ReaperGpuAnalysisCanvas(
                str(peaks_path),
                self.total_frames,
                pcm_service=self.source_pcm,
            )
        else:
            assert self.rp is not None
            self.canvas = PeaksCanvas(
                self.rp,
                self.total_frames,
                pcm_service=self.source_pcm,
            )
            self.canvas.native_levels = qpainter_native_levels

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.8)
        self.player.setSource(QUrl.fromLocalFile(str(playback_path)))

        self.canvas.seekRequested.connect(self.seek_frame)
        self.canvas.viewChanged.connect(self._view_changed)

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
        self.tiles_checkbox = QCheckBox("Tile/page debug")
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
        if hasattr(self.canvas, "verticalScaleChanged"):
            self.canvas.verticalScaleChanged.connect(self._canvas_vertical_scale_changed)

        self.spectrogram_gain = QDoubleSpinBox()
        self.spectrogram_gain.setRange(0.05, 32.0)
        self.spectrogram_gain.setDecimals(2)
        self.spectrogram_gain.setSingleStep(0.25)
        self.spectrogram_gain.setValue(1.0)
        self.spectrogram_gain.setEnabled(self.render_backend == "glsl")
        if hasattr(self.canvas, "set_spectrogram_gain"):
            self.spectrogram_gain.valueChanged.connect(self.canvas.set_spectrogram_gain)

        self.heatmap_checkbox = QCheckBox("Heatmap")
        self.heatmap_checkbox.setChecked(True)
        self.heatmap_checkbox.setEnabled(self.render_backend == "glsl")
        if hasattr(self.canvas, "set_heatmap"):
            self.heatmap_checkbox.toggled.connect(self.canvas.set_heatmap)

        self.spectral_checkbox = QCheckBox("-'s' overlay")
        self.spectral_checkbox.setChecked(True)
        self.spectral_checkbox.setEnabled(self.render_backend == "glsl")
        if hasattr(self.canvas, "set_spectral_overlay"):
            self.spectral_checkbox.toggled.connect(self.canvas.set_spectral_overlay)

        self.loudness_checkbox = QCheckBox("-'r' overlay")
        self.loudness_checkbox.setChecked(True)
        self.loudness_checkbox.setEnabled(self.render_backend == "glsl")
        if hasattr(self.canvas, "set_loudness_overlay"):
            self.loudness_checkbox.toggled.connect(self.canvas.set_loudness_overlay)

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 100_000)
        self.position_slider.sliderMoved.connect(self._slider_seek)
        self.time_label = QLabel("00:00.00 / 00:00.00")
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.pcm_range_label = QLabel("PCM range events: none")
        self.pcm_range_label.setWordWrap(True)
        self.pcm_range_access_count = 0
        self.pcm_range_decode_count = 0
        self.api_label = QLabel()
        self.api_label.setWordWrap(True)
        self.gesture_label = QLabel(
            "Wheel: horizontal zoom at cursor  |  Ctrl+Wheel: vertical waveform zoom"
        )

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
        controls.addWidget(QLabel("Spectrogram gain"))
        controls.addWidget(self.spectrogram_gain)
        controls.addWidget(self.heatmap_checkbox)
        controls.addWidget(self.spectral_checkbox)
        controls.addWidget(self.loudness_checkbox)
        controls.addStretch(1)
        controls.addWidget(self.time_label)

        layout = QVBoxLayout()
        layout.addWidget(self.overview)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(controls)
        layout.addWidget(self.position_slider)
        layout.addWidget(self.gesture_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.pcm_range_label)
        layout.addWidget(self.api_label)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
        self.resize(1480, 820)
        self.setWindowTitle(
            f"libreapeaks player [{self.render_backend}] — {audio_path.name}"
        )

        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.errorOccurred.connect(
            lambda _error, text: self.statusBar().showMessage(text)
        )
        pcm_loader = getattr(self.canvas, "pcm_loader", None)
        if pcm_loader is not None:
            pcm_loader.rangeAccess.connect(self._pcm_range_access)
            pcm_loader.rangeDecoded.connect(self._pcm_range_decoded)

        defaults = reapeaks.default_divisions(self.sample_rate)
        if self.source_pcm is not None:
            source_summary = (
                f"source_pcm={self.source_pcm.info.backend}/"
                f"{self.source_pcm.info.codec} "
                f"LRU={self.source_pcm.cache.capacity_bytes / 1048576:g}MiB "
                f"window≤{self.source_pcm.max_window_bytes / 1048576:g}MiB"
            )
        else:
            source_summary = f"source_pcm=unavailable ({self.source_pcm_error})"
        if self.render_backend == "glsl":
            assert self.gpu is not None
            layer_summary = ", ".join(
                f"{kind}={len(self.gpu.levels(kind))}"
                for kind in ("waveform", "spectral", "spectrogram", "loudness")
            )
            self.api_label.setText(
                f"cache={peaks_path.name}"
                f"{' (generated by libreapeaks)' if generated else ' (reused)'} | "
                f"native_mode={generation_mode} renderer=packed-GLSL | "
                f"cache_decoder={cache_decoder} playback_decoder={playback_decoder} | "
                f"encoding={self.wave_encoding} channels={self.channels} "
                f"sr={self.sample_rate} | raw_cache={self.gpu.raw_bytes:,} bytes | "
                f"{layer_summary} | default_divisions={defaults} | "
                "display transforms stay on GPU (gain/palette/overlays are uniforms) | "
                f"{source_summary}"
            )
        else:
            assert self.rp is not None
            coarsest = len(self.rp.levels()) - 1
            env_w, env_h, env_raw = self.rp.envelope_texture(coarsest)
            self.api_label.setText(
                f"cache={peaks_path.name}"
                f"{' (generated by libreapeaks)' if generated else ' (reused)'} | "
                f"native_mode={generation_mode} renderer=QPainter | "
                f"cache_decoder={cache_decoder} playback_decoder={playback_decoder} | "
                f"encoding={self.wave_encoding} channels={self.channels} "
                f"sr={self.sample_rate} | "
                f"levels={native_count + derived_count} "
                f"(native={native_count}, lazy-derived={derived_count}) | "
                f"default_divisions={defaults} | coarsest envelope_texture="
                f"{env_w}×{env_h} ({len(bytes(env_raw))} RGBA8 bytes) | "
                f"{source_summary}"
            )

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._refresh_status)
        self.ui_timer.start(150)
        self._view_changed(self.canvas.view_start, self.canvas.view_end)
        self._position_changed(0)

    def _canvas_vertical_scale_changed(self, value: float) -> None:
        self.vertical_scale.blockSignals(True)
        self.vertical_scale.setValue(float(value))
        self.vertical_scale.blockSignals(False)

    def _pcm_range_access(self, event: PcmRangeEvent) -> None:
        self.pcm_range_access_count += 1
        if event.reader_ran:
            self.pcm_range_decode_count += 1
        raw_end = event.raw_first_frame + event.raw_frame_count
        self.pcm_range_label.setText(
            f"PCM range events: accesses={self.pcm_range_access_count} "
            f"decoded={self.pcm_range_decode_count} | #{event.event_id} "
            f"{event.cache_disposition} {event.backend} "
            f"frames {event.raw_first_frame}…{raw_end} "
            f"reader={event.reader_ms:.2f}ms"
        )

    def _pcm_range_decoded(self, event: PcmRangeEvent) -> None:
        raw_end = event.raw_first_frame + event.raw_frame_count
        self.statusBar().showMessage(
            f"source PCM range decode #{event.event_id}: "
            f"{event.raw_first_frame}…{raw_end} ({event.reader_ms:.2f} ms)",
            4000,
        )

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self) -> None:
        self.player.stop()
        self.seek_frame(0)

    def seek_frame(self, frame: int) -> None:
        frame = max(0, min(int(frame), self.total_frames))
        ms = int(frame * 1000 / max(1, self.sample_rate))
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
        duration_frames = int(duration_ms * self.sample_rate / 1000)
        exact = exact_audio_frames(self.playback_path, self.sample_rate)
        self.total_frames = exact or max(1, duration_frames)
        self.canvas.set_total_frames(self.total_frames)
        self.overview.total_frames = self.total_frames

    def _position_changed(self, position_ms: int) -> None:
        frame = int(position_ms * self.sample_rate / 1000)
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
            f"{format_time(self.total_frames / self.sample_rate)}"
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
        span_seconds = (self.canvas.view_end - self.canvas.view_start) / self.sample_rate
        self.status_label.setText(
            f"viewport={format_time(self.canvas.view_start / self.sample_rate)}…"
            f"{format_time(self.canvas.view_end / self.sample_rate)} "
            f"({span_seconds:.3f}s) | {self.canvas.diagnostics}"
        )


def parse_divisions(value: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "divisions must be comma-separated integers"
        ) from exc
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("divisions must contain positive integers")
    return values


def parse_mib(value: str, *, allow_zero: bool) -> int:
    try:
        mib = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("MiB value must be numeric") from exc
    if not math.isfinite(mib) or mib < 0 or (mib == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise argparse.ArgumentTypeError(f"MiB value must be {qualifier}")
    result = int(mib * 1024 * 1024)
    if result == 0 and not allow_zero:
        raise argparse.ArgumentTypeError("MiB value rounds to zero bytes")
    return result


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
        "--renderer",
        choices=("auto", "glsl", "qpainter"),
        default="auto",
        help="auto prefers packed GLSL for spectrogram caches",
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
    parser.add_argument(
        "--no-source-pcm",
        action="store_true",
        help="disable automatic high-zoom source PCM windows",
    )
    parser.add_argument(
        "--pcm-decoder",
        choices=("auto", "wav", "ffmpeg"),
        default="auto",
        help="decoder used only for high-zoom source windows",
    )
    parser.add_argument(
        "--pcm-cache-mib",
        dest="pcm_cache_bytes",
        type=lambda value: parse_mib(value, allow_zero=True),
        default=DEFAULT_PCM_CACHE_BYTES,
        metavar="MIB",
        help="byte-bounded decoded-window LRU (default: 64 MiB)",
    )
    parser.add_argument(
        "--pcm-window-mib",
        dest="pcm_max_window_bytes",
        type=lambda value: parse_mib(value, allow_zero=False),
        default=DEFAULT_PCM_MAX_WINDOW_BYTES,
        metavar="MIB",
        help="hard limit for one decoded source window (default: 16 MiB)",
    )
    parser.add_argument(
        "--pcm-page-mib",
        dest="pcm_target_page_bytes",
        type=lambda value: parse_mib(value, allow_zero=False),
        default=DEFAULT_PCM_TARGET_PAGE_BYTES,
        metavar="MIB",
        help="target prefetch-page size (default: 1 MiB)",
    )
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
            render_backend=args.renderer,
            cache_decoder=args.cache_decoder,
            playback_decoder=args.playback_decoder,
            source_pcm_enabled=not args.no_source_pcm,
            pcm_decoder=args.pcm_decoder,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            decode_timeout=args.decode_timeout,
            pcm_cache_bytes=args.pcm_cache_bytes,
            pcm_max_window_bytes=args.pcm_max_window_bytes,
            pcm_target_page_bytes=args.pcm_target_page_bytes,
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
