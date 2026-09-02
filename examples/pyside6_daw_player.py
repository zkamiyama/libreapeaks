"""DAW-oriented PySide6 demo built on the packed libreapeaks player.

This keeps the existing playback/cache preparation path, but adds display-only
controls suitable for DAW inspection:

- exclusive waveform / spectral / spectrogram / loudness views;
- REAPER-like spectral peaks: waveform color follows dominant frequency while
  tonality controls how strongly the spectral color replaces the normal peak;
- REAPER-like loudness peaks/graph with LUFS-M or LUFS-S, opacity, LU offset,
  graph range, and band-transition controls;
- spectrogram gain, floor/ceiling, contrast, heatmap, and frequency axis.

When started without an audio path, the demo opens a modern drop target. Drop a
local media file there and the same window immediately becomes a progress view,
builds/reuses the complete cache, and opens the player without a second dialog.

The cache is never rewritten by display controls. Multichannel material remains
on one shared timeline with one vertically stacked lane per channel.

Examples:
    python examples/pyside6_daw_player.py
    python examples/pyside6_daw_player.py mix.wav
"""
from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSlider,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import pyside6_player as _base
from pyside6_prepare import CacheWorker
from pyside6_zita_gl_view import ZitaGpuAnalysisCanvas

# The DAW demo uses geometry-based waveform rendering while keeping the
# lower-level/reference player unchanged.
_base.ReaperGpuAnalysisCanvas = ZitaGpuAnalysisCanvas
_BasePlayerWindow = _base.PlayerWindow


class DawPlayerWindow(_BasePlayerWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        toolbar = QToolBar("DAW analysis display", self)
        toolbar.setMovable(True)
        self.addToolBar(toolbar)

        display_supported = hasattr(self.canvas, "set_display_mode")
        spectrogram_supported = all(
            hasattr(self.canvas, name)
            for name in (
                "set_spectrogram_gain_db",
                "set_spectrogram_floor_db",
                "set_spectrogram_ceiling_db",
                "set_spectrogram_contrast",
                "set_spectrogram_frequency_log",
            )
        )
        spectral_supported = all(
            hasattr(self.canvas, name)
            for name in (
                "set_peak_display_zoom_db",
                "set_analysis_opacity",
                "set_spectral_low_hz",
                "set_spectral_high_hz",
                "set_spectral_range_mode",
                "set_spectral_reverse",
                "set_spectral_fade_noise",
            )
        )
        loudness_supported = all(
            hasattr(self.canvas, name)
            for name in (
                "set_peak_display_zoom_db",
                "set_analysis_opacity",
                "set_loudness_metric",
                "set_loudness_view",
                "set_loudness_floor_lu",
                "set_loudness_ceiling_lu",
                "set_loudness_offset_lu",
                "set_loudness_transition_lu",
            )
        )

        self.display_mode_combo = QComboBox(self)
        mode_specs = (
            ("Waveform", "waveform", "waveform"),
            ("Spectral peaks", "spectral", "spectral"),
            ("Spectrogram", "spectrogram", "spectrogram"),
            ("Loudness", "loudness", "loudness"),
        )
        gpu = getattr(self.canvas, "gpu", None)
        for label, mode, kind in mode_specs:
            available = mode == "waveform"
            if gpu is not None:
                try:
                    available = available or bool(gpu.levels(kind))
                except (TypeError, ValueError):
                    available = False
            if available:
                self.display_mode_combo.addItem(label, mode)
        self.display_mode_combo.setEnabled(display_supported)

        view_label = QLabel("View", self)
        toolbar.addWidget(view_label)
        toolbar.addWidget(self.display_mode_combo)
        toolbar.addSeparator()

        # REAPER exposes display zoom and color opacity across colored peak
        # modes. Keep these controls shared by spectral and loudness.
        common_zoom_label = QLabel("Peak zoom", self)
        self.analysis_zoom_db = QDoubleSpinBox(self)
        self.analysis_zoom_db.setRange(-24.0, 24.0)
        self.analysis_zoom_db.setDecimals(1)
        self.analysis_zoom_db.setSingleStep(1.0)
        self.analysis_zoom_db.setSuffix(" dB")
        self.analysis_zoom_db.setValue(0.0)

        common_opacity_label = QLabel("Opacity", self)
        self.analysis_opacity = QDoubleSpinBox(self)
        self.analysis_opacity.setRange(0.0, 100.0)
        self.analysis_opacity.setDecimals(0)
        self.analysis_opacity.setSingleStep(5.0)
        self.analysis_opacity.setSuffix(" %")
        self.analysis_opacity.setValue(92.0)

        self._common_analysis_widgets = [
            common_zoom_label,
            self.analysis_zoom_db,
            common_opacity_label,
            self.analysis_opacity,
        ]
        for widget in self._common_analysis_widgets:
            toolbar.addWidget(widget)
        toolbar.addSeparator()

        # Spectral peaks: frequency colors are painted inside the normal waveform
        # silhouette. Density/tonality controls saturation, matching REAPER's
        # spectral-peaks concept rather than plotting frequency as a Y trace.
        spectral_range_label = QLabel("Range", self)
        self.spectral_range = QComboBox(self)
        self.spectral_range.addItem("Full spectrum", 0)
        self.spectral_range.addItem("Every octave", 1)

        spectral_low_label = QLabel("Low", self)
        self.spectral_low_hz = QDoubleSpinBox(self)
        self.spectral_low_hz.setRange(10.0, 20000.0)
        self.spectral_low_hz.setDecimals(0)
        self.spectral_low_hz.setSingleStep(10.0)
        self.spectral_low_hz.setSuffix(" Hz")
        self.spectral_low_hz.setValue(20.0)

        spectral_high_label = QLabel("High", self)
        self.spectral_high_hz = QDoubleSpinBox(self)
        self.spectral_high_hz.setRange(20.0, 30000.0)
        self.spectral_high_hz.setDecimals(0)
        self.spectral_high_hz.setSingleStep(100.0)
        self.spectral_high_hz.setSuffix(" Hz")
        self.spectral_high_hz.setValue(10000.0)

        self.spectral_reverse = QCheckBox("Reverse", self)
        self.spectral_fade_noise = QCheckBox("Fade noise", self)
        self.spectral_fade_noise.setChecked(True)

        self._spectral_widgets = [
            spectral_range_label,
            self.spectral_range,
            spectral_low_label,
            self.spectral_low_hz,
            spectral_high_label,
            self.spectral_high_hz,
            self.spectral_reverse,
            self.spectral_fade_noise,
        ]
        for widget in self._spectral_widgets:
            toolbar.addWidget(widget)
        toolbar.addSeparator()

        # Loudness mirrors REAPER's two useful presentations: color the peaks by
        # one loudness measure, or retain normal peaks and overlay one LUFS graph.
        loudness_metric_label = QLabel("Measure", self)
        self.loudness_metric = QComboBox(self)
        self.loudness_metric.addItem("LUFS-M", 0)
        self.loudness_metric.addItem("LUFS-S", 1)

        loudness_style_label = QLabel("Style", self)
        self.loudness_style = QComboBox(self)
        self.loudness_style.addItem("Graph + peaks", 1)
        self.loudness_style.addItem("Colored peaks", 0)

        loudness_low_label = QLabel("Low", self)
        self.loudness_floor_lu = QDoubleSpinBox(self)
        self.loudness_floor_lu.setRange(-70.0, -0.1)
        self.loudness_floor_lu.setDecimals(1)
        self.loudness_floor_lu.setSingleStep(1.0)
        self.loudness_floor_lu.setSuffix(" LUFS")
        self.loudness_floor_lu.setValue(-48.0)

        loudness_high_label = QLabel("High", self)
        self.loudness_ceiling_lu = QDoubleSpinBox(self)
        self.loudness_ceiling_lu.setRange(-69.0, 6.0)
        self.loudness_ceiling_lu.setDecimals(1)
        self.loudness_ceiling_lu.setSingleStep(1.0)
        self.loudness_ceiling_lu.setSuffix(" LUFS")
        self.loudness_ceiling_lu.setValue(0.0)

        loudness_offset_label = QLabel("Offset", self)
        self.loudness_offset_lu = QDoubleSpinBox(self)
        self.loudness_offset_lu.setRange(-24.0, 24.0)
        self.loudness_offset_lu.setDecimals(1)
        self.loudness_offset_lu.setSingleStep(1.0)
        self.loudness_offset_lu.setSuffix(" LU")
        self.loudness_offset_lu.setValue(0.0)

        loudness_transition_label = QLabel("Transition", self)
        self.loudness_transition_lu = QDoubleSpinBox(self)
        self.loudness_transition_lu.setRange(0.05, 12.0)
        self.loudness_transition_lu.setDecimals(2)
        self.loudness_transition_lu.setSingleStep(0.25)
        self.loudness_transition_lu.setSuffix(" LU")
        self.loudness_transition_lu.setValue(1.5)

        self._loudness_widgets = [
            loudness_metric_label,
            self.loudness_metric,
            loudness_style_label,
            self.loudness_style,
            loudness_low_label,
            self.loudness_floor_lu,
            loudness_high_label,
            self.loudness_ceiling_lu,
            loudness_offset_label,
            self.loudness_offset_lu,
            loudness_transition_label,
            self.loudness_transition_lu,
        ]
        for widget in self._loudness_widgets:
            toolbar.addWidget(widget)
        toolbar.addSeparator()

        # Spectrogram controls.
        self.spec_heatmap = QCheckBox("Heatmap", self)
        self.spec_heatmap.setChecked(True)

        spec_intensity_label = QLabel("Intensity", self)
        self.spec_intensity = QSlider(Qt.Orientation.Horizontal, self)
        self.spec_intensity.setRange(-600, 600)
        self.spec_intensity.setSingleStep(10)
        self.spec_intensity.setPageStep(50)
        self.spec_intensity.setValue(0)
        self.spec_intensity.setFixedWidth(170)
        self.spec_intensity.setToolTip("Spectrogram display gain (-60 dB to +60 dB)")

        self.spec_gain_db = QDoubleSpinBox(self)
        self.spec_gain_db.setRange(-60.0, 60.0)
        self.spec_gain_db.setDecimals(1)
        self.spec_gain_db.setSingleStep(1.0)
        self.spec_gain_db.setSuffix(" dB")
        self.spec_gain_db.setValue(0.0)

        spec_floor_label = QLabel("Floor", self)
        self.spec_floor_db = QDoubleSpinBox(self)
        self.spec_floor_db.setRange(-200.0, -0.1)
        self.spec_floor_db.setDecimals(1)
        self.spec_floor_db.setSingleStep(5.0)
        self.spec_floor_db.setSuffix(" dB")
        self.spec_floor_db.setValue(-100.0)

        spec_ceiling_label = QLabel("Ceiling", self)
        self.spec_ceiling_db = QDoubleSpinBox(self)
        self.spec_ceiling_db.setRange(-199.0, 24.0)
        self.spec_ceiling_db.setDecimals(1)
        self.spec_ceiling_db.setSingleStep(1.0)
        self.spec_ceiling_db.setSuffix(" dB")
        self.spec_ceiling_db.setValue(0.0)

        spec_contrast_label = QLabel("Contrast", self)
        self.spec_contrast = QDoubleSpinBox(self)
        self.spec_contrast.setRange(0.05, 8.0)
        self.spec_contrast.setDecimals(2)
        self.spec_contrast.setSingleStep(0.1)
        self.spec_contrast.setValue(1.0)

        self.spec_frequency = QComboBox(self)
        self.spec_frequency.addItem("Log frequency", True)
        self.spec_frequency.addItem("Linear frequency", False)

        self._spectrogram_widgets = [
            self.spec_heatmap,
            spec_intensity_label,
            self.spec_intensity,
            self.spec_gain_db,
            spec_floor_label,
            self.spec_floor_db,
            spec_ceiling_label,
            self.spec_ceiling_db,
            spec_contrast_label,
            self.spec_contrast,
            self.spec_frequency,
        ]
        for widget in self._spectrogram_widgets:
            toolbar.addWidget(widget)

        if display_supported:
            # Replace the legacy independent overlay controls with one explicit
            # analysis mode selector so the active visualization is unambiguous.
            for name in (
                "spectrogram_gain",
                "heatmap_checkbox",
                "spectral_checkbox",
                "loudness_checkbox",
            ):
                widget = getattr(self, name, None)
                if widget is not None:
                    widget.hide()
            for label in self.findChildren(QLabel):
                if label.text() == "Spectrogram gain":
                    label.hide()

            self.display_mode_combo.currentIndexChanged.connect(
                self._display_mode_changed
            )
            self.canvas.displayModeChanged.connect(self._canvas_display_mode_changed)

        if spectral_supported or loudness_supported:
            self.analysis_zoom_db.valueChanged.connect(
                self.canvas.set_peak_display_zoom_db
            )
            self.analysis_opacity.valueChanged.connect(
                lambda value: self.canvas.set_analysis_opacity(float(value) / 100.0)
            )
            self.canvas.set_peak_display_zoom_db(self.analysis_zoom_db.value())
            self.canvas.set_analysis_opacity(self.analysis_opacity.value() / 100.0)

        if spectral_supported:
            self.spectral_low_hz.valueChanged.connect(self.canvas.set_spectral_low_hz)
            self.spectral_high_hz.valueChanged.connect(self.canvas.set_spectral_high_hz)
            self.spectral_range.currentIndexChanged.connect(
                lambda _index: self.canvas.set_spectral_range_mode(
                    int(self.spectral_range.currentData())
                )
            )
            self.spectral_reverse.toggled.connect(self.canvas.set_spectral_reverse)
            self.spectral_fade_noise.toggled.connect(
                self.canvas.set_spectral_fade_noise
            )
            self.canvas.set_spectral_low_hz(self.spectral_low_hz.value())
            self.canvas.set_spectral_high_hz(self.spectral_high_hz.value())
            self.canvas.set_spectral_range_mode(int(self.spectral_range.currentData()))
            self.canvas.set_spectral_reverse(self.spectral_reverse.isChecked())
            self.canvas.set_spectral_fade_noise(self.spectral_fade_noise.isChecked())

        if loudness_supported:
            self.loudness_metric.currentIndexChanged.connect(
                lambda _index: self.canvas.set_loudness_metric(
                    int(self.loudness_metric.currentData())
                )
            )
            self.loudness_style.currentIndexChanged.connect(
                lambda _index: self.canvas.set_loudness_view(
                    int(self.loudness_style.currentData())
                )
            )
            self.loudness_floor_lu.valueChanged.connect(
                self.canvas.set_loudness_floor_lu
            )
            self.loudness_ceiling_lu.valueChanged.connect(
                self.canvas.set_loudness_ceiling_lu
            )
            self.loudness_offset_lu.valueChanged.connect(
                self.canvas.set_loudness_offset_lu
            )
            self.loudness_transition_lu.valueChanged.connect(
                self.canvas.set_loudness_transition_lu
            )
            self.canvas.set_loudness_metric(int(self.loudness_metric.currentData()))
            self.canvas.set_loudness_view(int(self.loudness_style.currentData()))
            self.canvas.set_loudness_floor_lu(self.loudness_floor_lu.value())
            self.canvas.set_loudness_ceiling_lu(self.loudness_ceiling_lu.value())
            self.canvas.set_loudness_offset_lu(self.loudness_offset_lu.value())
            self.canvas.set_loudness_transition_lu(
                self.loudness_transition_lu.value()
            )

        if spectrogram_supported:
            # The legacy multiplicative demo gain remains at unity; calibrated
            # display gain is controlled by the DAW toolbar instead.
            self.spectrogram_gain.blockSignals(True)
            self.spectrogram_gain.setValue(1.0)
            self.spectrogram_gain.blockSignals(False)

            self.spec_heatmap.toggled.connect(self.canvas.set_heatmap)
            self.spec_intensity.valueChanged.connect(
                lambda value: self.spec_gain_db.setValue(value / 10.0)
            )
            self.spec_gain_db.valueChanged.connect(self._spectrogram_gain_changed)
            self.spec_floor_db.valueChanged.connect(self.canvas.set_spectrogram_floor_db)
            self.spec_ceiling_db.valueChanged.connect(self.canvas.set_spectrogram_ceiling_db)
            self.spec_contrast.valueChanged.connect(self.canvas.set_spectrogram_contrast)
            self.spec_frequency.currentIndexChanged.connect(
                lambda _index: self.canvas.set_spectrogram_frequency_log(
                    bool(self.spec_frequency.currentData())
                )
            )

            self.canvas.set_heatmap(True)
            self.canvas.set_spectrogram_gain_db(self.spec_gain_db.value())
            self.canvas.set_spectrogram_floor_db(self.spec_floor_db.value())
            self.canvas.set_spectrogram_ceiling_db(self.spec_ceiling_db.value())
            self.canvas.set_spectrogram_contrast(self.spec_contrast.value())
            self.canvas.set_spectrogram_frequency_log(True)

        if display_supported and self.display_mode_combo.count() > 0:
            self.canvas.set_display_mode(str(self.display_mode_combo.currentData()))
        self._update_analysis_controls(
            spectral_supported=spectral_supported,
            loudness_supported=loudness_supported,
            spectrogram_supported=spectrogram_supported,
        )

        self.setWindowTitle(
            self.windowTitle().replace("libreapeaks player", "libreapeaks DAW player")
        )

    def _display_mode_changed(self, _index: int) -> None:
        mode = self.display_mode_combo.currentData()
        if mode is not None and hasattr(self.canvas, "set_display_mode"):
            self.canvas.set_display_mode(str(mode))
        self._update_analysis_controls()

    def _canvas_display_mode_changed(self, mode: str) -> None:
        index = self.display_mode_combo.findData(mode)
        if index >= 0 and index != self.display_mode_combo.currentIndex():
            self.display_mode_combo.blockSignals(True)
            self.display_mode_combo.setCurrentIndex(index)
            self.display_mode_combo.blockSignals(False)
        self._update_analysis_controls()

    def _spectrogram_gain_changed(self, value: float) -> None:
        raw = int(round(float(value) * 10.0))
        if raw != self.spec_intensity.value():
            self.spec_intensity.blockSignals(True)
            self.spec_intensity.setValue(raw)
            self.spec_intensity.blockSignals(False)
        self.canvas.set_spectrogram_gain_db(float(value))

    def _update_analysis_controls(
        self,
        *,
        spectral_supported: bool | None = None,
        loudness_supported: bool | None = None,
        spectrogram_supported: bool | None = None,
    ) -> None:
        mode = self.display_mode_combo.currentData()
        if spectral_supported is None:
            spectral_supported = hasattr(self.canvas, "set_spectral_range_mode")
        if loudness_supported is None:
            loudness_supported = hasattr(self.canvas, "set_loudness_metric")
        if spectrogram_supported is None:
            spectrogram_supported = hasattr(self.canvas, "set_spectrogram_gain_db")

        common_visible = mode in ("spectral", "loudness")
        spectral_visible = mode == "spectral"
        loudness_visible = mode == "loudness"
        spectrogram_visible = mode == "spectrogram"

        for widget in self._common_analysis_widgets:
            widget.setVisible(common_visible)
            widget.setEnabled(
                common_visible
                and ((spectral_visible and spectral_supported)
                     or (loudness_visible and loudness_supported))
            )
        for widget in self._spectral_widgets:
            widget.setVisible(spectral_visible)
            widget.setEnabled(spectral_visible and spectral_supported)
        for widget in self._loudness_widgets:
            widget.setVisible(loudness_visible)
            widget.setEnabled(loudness_visible and loudness_supported)
        for widget in self._spectrogram_widgets:
            widget.setVisible(spectrogram_visible)
            widget.setEnabled(spectrogram_visible and spectrogram_supported)


def _default_cache_options() -> dict[str, object]:
    """Match the no-option defaults of pyside6_player for drop-open startup."""
    return {
        "peaks_path": None,
        "rebuild": False,
        "decoder": "auto",
        "cache_mode": "auto",
        "cache_directory": None,
        "reaper_cache_map": None,
        "allow_stale_cache": False,
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "decode_timeout": _base.DEFAULT_DECODE_TIMEOUT,
        "max_decode_bytes": _base.DEFAULT_MAX_DECODE_BYTES,
        "wave_encoding": "auto",
        "divisions": None,
        "fine_peaks_per_second": 300,
        "lock_timeout": 30.0,
    }


class DropLaunchWindow(QMainWindow):
    """Drop target that turns directly into an in-place preparation view."""

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setWindowTitle("libreapeaks DAW player")
        self.resize(900, 520)

        self.audio_path: Path | None = None
        self._thread: QThread | None = None
        self._worker: CacheWorker | None = None
        self._player_window: DawPlayerWindow | None = None
        self._prepared_playback = None

        self.drop_label = QLabel(
            "Drop an audio file here\n\n"
            "A complete waveform + spectral + spectrogram + loudness cache "
            "will be prepared automatically."
        )
        self.drop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_label.setWordWrap(True)
        self.drop_label.setStyleSheet(
            "QLabel { border: 2px dashed #667080; border-radius: 12px; "
            "padding: 48px; font-size: 18px; }"
        )

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.hide()

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        self.status.hide()

        self.open_button = QPushButton("Open audio file…")
        self.open_button.clicked.connect(self._choose_file)

        layout = QVBoxLayout()
        layout.addStretch(1)
        layout.addWidget(self.drop_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addWidget(self.open_button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

    def _choose_file(self) -> None:
        if self._thread is not None:
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Open audio file",
            str(Path.home()),
            "Media files (*.*)",
        )
        if selected:
            self._begin_prepare(Path(selected))

    def _first_local_file(self, event) -> Path | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.is_file():
                    return path
        return None

    def dragEnterEvent(self, event):  # noqa: N802 - Qt API
        if self._thread is None and self._first_local_file(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):  # noqa: N802 - Qt API
        path = self._first_local_file(event)
        if path is None or self._thread is not None:
            event.ignore()
            return
        event.acceptProposedAction()
        self._begin_prepare(path)

    def _set_idle(self, message: str | None = None) -> None:
        self.setAcceptDrops(True)
        self.open_button.setEnabled(True)
        self.open_button.show()
        self.progress.hide()
        if message:
            self.status.setText(message)
            self.status.show()
        else:
            self.status.hide()
        self.drop_label.setText(
            "Drop an audio file here\n\n"
            "A complete waveform + spectral + spectrogram + loudness cache "
            "will be prepared automatically."
        )

    def _begin_prepare(self, audio_path: Path) -> None:
        if self._thread is not None:
            return
        audio = audio_path.expanduser().resolve(strict=False)
        if not audio.is_file():
            self._set_idle(f"File not found: {audio}")
            return

        self.audio_path = audio
        self.setAcceptDrops(False)
        self.open_button.setEnabled(False)
        self.open_button.hide()
        self.drop_label.setText(f"Preparing\n{audio.name}")
        self.progress.setValue(0)
        self.progress.show()
        self.status.setText("Starting full cache preparation…")
        self.status.show()

        thread = QThread(self)
        worker = CacheWorker(audio, _default_cache_options())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._worker_finished)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_progress(self, stage: str, value: int) -> None:
        self.progress.setValue(max(0, min(100, int(value))))
        self.status.setText(stage)

    def _on_completed(self, peaks_path: object, generated: bool, mode: str) -> None:
        audio = self.audio_path
        if audio is None:
            self._on_failed("cache preparation completed without an audio path")
            return

        self.progress.setValue(100)
        self.status.setText("Opening player…")
        prepared = None
        try:
            prepared = _base.prepare_playback_audio(
                audio,
                decoder="native",
                ffmpeg="ffmpeg",
                ffprobe="ffprobe",
                timeout=_base.DEFAULT_DECODE_TIMEOUT,
                max_decode_bytes=_base.DEFAULT_MAX_DECODE_BYTES,
            )
            player = DawPlayerWindow(
                audio,
                prepared.path,
                Path(peaks_path),
                bool(generated),
                generation_mode=mode,
                render_backend="auto",
                cache_decoder="auto",
                playback_decoder="native",
                source_pcm_enabled=True,
                pcm_decoder="auto",
                ffmpeg="ffmpeg",
                ffprobe="ffprobe",
                decode_timeout=_base.DEFAULT_DECODE_TIMEOUT,
                pcm_cache_bytes=_base.DEFAULT_PCM_CACHE_BYTES,
                pcm_max_window_bytes=_base.DEFAULT_PCM_MAX_WINDOW_BYTES,
                pcm_target_page_bytes=_base.DEFAULT_PCM_TARGET_PAGE_BYTES,
            )
        except Exception as exc:
            if prepared is not None:
                prepared.close()
            self._on_failed(f"Could not open player: {exc}")
            return

        self._prepared_playback = prepared
        self._player_window = player
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(prepared.close)
        player.show()
        self.hide()

    def _on_failed(self, message: str) -> None:
        self.progress.setValue(0)
        self._set_idle(f"Preparation failed: {message}")

    def _worker_finished(self) -> None:
        self._worker = None
        self._thread = None
        if self._player_window is None and self.isVisible():
            self.setAcceptDrops(True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    _base.PlayerWindow = DawPlayerWindow
    if not args:
        app = QApplication([sys.argv[0]])
        window = DropLaunchWindow()
        window.show()
        return app.exec()
    return _base.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
