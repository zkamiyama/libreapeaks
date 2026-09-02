"""DAW-oriented PySide6 demo built on the packed libreapeaks player.

This keeps the existing playback/cache preparation path, but adds display-only
controls suitable for DAW inspection:

- exclusive waveform / spectral / spectrogram / loudness views;
- spectrogram gain in dB with a directly exposed intensity slider;
- floor and ceiling in dB;
- contrast/gamma;
- heatmap toggle;
- linear or logarithmic frequency axis.

The cache is never rewritten by these controls. Multichannel material remains
on one shared timeline with one vertically stacked lane per channel, matching
the REAPER-style layout used by the base GLSL renderer.

Example:
    python examples/pyside6_daw_player.py mix.wav --generation-mode spectrogram
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QSlider,
    QToolBar,
)

import pyside6_player as _base

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

        self.spec_heatmap = QCheckBox("Heatmap", self)
        self.spec_heatmap.setChecked(True)
        self.spec_heatmap.setEnabled(spectrogram_supported)

        self.spec_intensity = QSlider(Qt.Orientation.Horizontal, self)
        self.spec_intensity.setRange(-600, 600)
        self.spec_intensity.setSingleStep(10)
        self.spec_intensity.setPageStep(50)
        self.spec_intensity.setValue(0)
        self.spec_intensity.setFixedWidth(170)
        self.spec_intensity.setToolTip("Spectrogram display gain (-60 dB to +60 dB)")
        self.spec_intensity.setEnabled(spectrogram_supported)

        self.spec_gain_db = QDoubleSpinBox(self)
        self.spec_gain_db.setRange(-60.0, 60.0)
        self.spec_gain_db.setDecimals(1)
        self.spec_gain_db.setSingleStep(1.0)
        self.spec_gain_db.setSuffix(" dB")
        self.spec_gain_db.setValue(0.0)
        self.spec_gain_db.setEnabled(spectrogram_supported)

        self.spec_floor_db = QDoubleSpinBox(self)
        self.spec_floor_db.setRange(-200.0, -0.1)
        self.spec_floor_db.setDecimals(1)
        self.spec_floor_db.setSingleStep(5.0)
        self.spec_floor_db.setSuffix(" dB")
        self.spec_floor_db.setValue(-100.0)
        self.spec_floor_db.setEnabled(spectrogram_supported)

        self.spec_ceiling_db = QDoubleSpinBox(self)
        self.spec_ceiling_db.setRange(-199.0, 24.0)
        self.spec_ceiling_db.setDecimals(1)
        self.spec_ceiling_db.setSingleStep(1.0)
        self.spec_ceiling_db.setSuffix(" dB")
        self.spec_ceiling_db.setValue(0.0)
        self.spec_ceiling_db.setEnabled(spectrogram_supported)

        self.spec_contrast = QDoubleSpinBox(self)
        self.spec_contrast.setRange(0.05, 8.0)
        self.spec_contrast.setDecimals(2)
        self.spec_contrast.setSingleStep(0.1)
        self.spec_contrast.setValue(1.0)
        self.spec_contrast.setEnabled(spectrogram_supported)

        self.spec_frequency = QComboBox(self)
        self.spec_frequency.addItem("Log frequency", True)
        self.spec_frequency.addItem("Linear frequency", False)
        self.spec_frequency.setEnabled(spectrogram_supported)

        toolbar.addWidget(QLabel("View", self))
        toolbar.addWidget(self.display_mode_combo)
        toolbar.addSeparator()
        toolbar.addWidget(self.spec_heatmap)
        toolbar.addWidget(QLabel("Intensity", self))
        toolbar.addWidget(self.spec_intensity)
        toolbar.addWidget(self.spec_gain_db)
        toolbar.addWidget(QLabel("Floor", self))
        toolbar.addWidget(self.spec_floor_db)
        toolbar.addWidget(QLabel("Ceiling", self))
        toolbar.addWidget(self.spec_ceiling_db)
        toolbar.addWidget(QLabel("Contrast", self))
        toolbar.addWidget(self.spec_contrast)
        toolbar.addWidget(self.spec_frequency)

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
        self._update_spectrogram_controls()

        self.setWindowTitle(
            self.windowTitle().replace("libreapeaks player", "libreapeaks DAW player")
        )

    def _display_mode_changed(self, _index: int) -> None:
        mode = self.display_mode_combo.currentData()
        if mode is not None and hasattr(self.canvas, "set_display_mode"):
            self.canvas.set_display_mode(str(mode))
        self._update_spectrogram_controls()

    def _canvas_display_mode_changed(self, mode: str) -> None:
        index = self.display_mode_combo.findData(mode)
        if index >= 0 and index != self.display_mode_combo.currentIndex():
            self.display_mode_combo.blockSignals(True)
            self.display_mode_combo.setCurrentIndex(index)
            self.display_mode_combo.blockSignals(False)
        self._update_spectrogram_controls()

    def _spectrogram_gain_changed(self, value: float) -> None:
        raw = int(round(float(value) * 10.0))
        if raw != self.spec_intensity.value():
            self.spec_intensity.blockSignals(True)
            self.spec_intensity.setValue(raw)
            self.spec_intensity.blockSignals(False)
        self.canvas.set_spectrogram_gain_db(float(value))

    def _update_spectrogram_controls(self) -> None:
        enabled = (
            self.display_mode_combo.isEnabled()
            and self.display_mode_combo.currentData() == "spectrogram"
            and hasattr(self.canvas, "set_spectrogram_gain_db")
        )
        for widget in (
            self.spec_heatmap,
            self.spec_intensity,
            self.spec_gain_db,
            self.spec_floor_db,
            self.spec_ceiling_db,
            self.spec_contrast,
            self.spec_frequency,
        ):
            widget.setEnabled(enabled)


def main(argv: list[str] | None = None) -> int:
    # Reuse the base application's argument parsing, cache preparation,
    # playback setup and cleanup while substituting only the window class.
    _base.PlayerWindow = DawPlayerWindow
    return _base.main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())