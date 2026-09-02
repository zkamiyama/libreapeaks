"""DAW-oriented PySide6 demo built on the packed libreapeaks player.

This keeps the existing playback/cache preparation path, but adds display-only
spectrogram controls suitable for DAW inspection:

- gain in dB;
- floor and ceiling in dB;
- contrast/gamma;
- linear or logarithmic frequency axis.

The cache is never rewritten by these controls. Multichannel material remains
on one shared timeline with one vertically stacked lane per channel, matching
the REAPER-style layout used by the base GLSL renderer.

Example:
    python examples/pyside6_daw_player.py mix.wav --generation-mode spectrogram
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QLabel, QToolBar

import pyside6_player as _base

_BasePlayerWindow = _base.PlayerWindow


class DawPlayerWindow(_BasePlayerWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        toolbar = QToolBar("DAW spectrogram display", self)
        toolbar.setMovable(True)
        self.addToolBar(toolbar)

        supported = all(
            hasattr(self.canvas, name)
            for name in (
                "set_spectrogram_gain_db",
                "set_spectrogram_floor_db",
                "set_spectrogram_ceiling_db",
                "set_spectrogram_contrast",
                "set_spectrogram_frequency_log",
            )
        )

        self.spec_gain_db = QDoubleSpinBox(self)
        self.spec_gain_db.setRange(-60.0, 60.0)
        self.spec_gain_db.setDecimals(1)
        self.spec_gain_db.setSingleStep(1.0)
        self.spec_gain_db.setSuffix(" dB")
        self.spec_gain_db.setValue(0.0)
        self.spec_gain_db.setEnabled(supported)

        self.spec_floor_db = QDoubleSpinBox(self)
        self.spec_floor_db.setRange(-200.0, -0.1)
        self.spec_floor_db.setDecimals(1)
        self.spec_floor_db.setSingleStep(5.0)
        self.spec_floor_db.setSuffix(" dB")
        self.spec_floor_db.setValue(-100.0)
        self.spec_floor_db.setEnabled(supported)

        self.spec_ceiling_db = QDoubleSpinBox(self)
        self.spec_ceiling_db.setRange(-199.0, 24.0)
        self.spec_ceiling_db.setDecimals(1)
        self.spec_ceiling_db.setSingleStep(1.0)
        self.spec_ceiling_db.setSuffix(" dB")
        self.spec_ceiling_db.setValue(0.0)
        self.spec_ceiling_db.setEnabled(supported)

        self.spec_contrast = QDoubleSpinBox(self)
        self.spec_contrast.setRange(0.05, 8.0)
        self.spec_contrast.setDecimals(2)
        self.spec_contrast.setSingleStep(0.1)
        self.spec_contrast.setValue(1.0)
        self.spec_contrast.setEnabled(supported)

        self.spec_frequency = QComboBox(self)
        self.spec_frequency.addItem("Log frequency", True)
        self.spec_frequency.addItem("Linear frequency", False)
        self.spec_frequency.setEnabled(supported)

        for label, widget in (
            ("Spec gain", self.spec_gain_db),
            ("Floor", self.spec_floor_db),
            ("Ceiling", self.spec_ceiling_db),
            ("Contrast", self.spec_contrast),
            ("Frequency", self.spec_frequency),
        ):
            toolbar.addWidget(QLabel(label, self))
            toolbar.addWidget(widget)

        if supported:
            # The legacy multiplicative demo gain remains at unity; the DAW
            # toolbar owns calibrated dB-domain display gain instead.
            self.spectrogram_gain.blockSignals(True)
            self.spectrogram_gain.setValue(1.0)
            self.spectrogram_gain.setEnabled(False)
            self.spectrogram_gain.blockSignals(False)

            self.spec_gain_db.valueChanged.connect(self.canvas.set_spectrogram_gain_db)
            self.spec_floor_db.valueChanged.connect(self.canvas.set_spectrogram_floor_db)
            self.spec_ceiling_db.valueChanged.connect(self.canvas.set_spectrogram_ceiling_db)
            self.spec_contrast.valueChanged.connect(self.canvas.set_spectrogram_contrast)
            self.spec_frequency.currentIndexChanged.connect(
                lambda _index: self.canvas.set_spectrogram_frequency_log(
                    bool(self.spec_frequency.currentData())
                )
            )

            self.canvas.set_spectrogram_gain_db(self.spec_gain_db.value())
            self.canvas.set_spectrogram_floor_db(self.spec_floor_db.value())
            self.canvas.set_spectrogram_ceiling_db(self.spec_ceiling_db.value())
            self.canvas.set_spectrogram_contrast(self.spec_contrast.value())
            self.canvas.set_spectrogram_frequency_log(True)

        self.setWindowTitle(self.windowTitle().replace("libreapeaks player", "libreapeaks DAW player"))


def main(argv: list[str] | None = None) -> int:
    # Reuse the base application's argument parsing, cache preparation,
    # playback setup and cleanup while substituting only the window class.
    _base.PlayerWindow = DawPlayerWindow
    return _base.main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
