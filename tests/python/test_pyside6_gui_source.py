from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PySideGuiSourceTests(unittest.TestCase):
    def test_glsl_wrapper_has_exclusive_analysis_modes(self) -> None:
        source = (ROOT / "examples" / "pyside6_reaper_gl_view.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"waveform": 0', source)
        self.assertIn('"spectral": 1', source)
        self.assertIn('"spectrogram": 2', source)
        self.assertIn('"loudness": 3', source)
        self.assertIn("u_displayMode == 0", source)
        self.assertIn("u_displayMode == 1", source)
        self.assertIn("u_displayMode == 2", source)
        self.assertIn("u_displayMode == 3", source)

    def test_glsl_wrapper_smooths_spectrogram_and_wave_lines(self) -> None:
        source = (ROOT / "examples" / "pyside6_reaper_gl_view.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("float sampleG(", source)
        self.assertIn("return mix(lo, hi, bt);", source)
        self.assertIn("fwidth(lineDistance)", source)
        self.assertIn("fwidth(lower)", source)
        self.assertIn("fwidth(upper)", source)

    def test_daw_toolbar_exposes_view_and_spectrogram_controls(self) -> None:
        source = (ROOT / "examples" / "pyside6_daw_player.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('QLabel("View"', source)
        self.assertIn('QLabel("Intensity"', source)
        self.assertIn('QCheckBox("Heatmap"', source)
        self.assertIn('("Waveform", "waveform", "waveform")', source)
        self.assertIn('("Spectrogram", "spectrogram", "spectrogram")', source)
        self.assertIn("set_display_mode", source)


if __name__ == "__main__":
    unittest.main()
