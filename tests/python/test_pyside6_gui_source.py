from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


def read_source(name: str) -> str:
    path = ROOT / "examples" / name
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    return source


class PySideGuiSourceTests(unittest.TestCase):
    def test_glsl_wrapper_has_exclusive_analysis_modes(self) -> None:
        source = read_source("pyside6_reaper_gl_view.py")
        self.assertIn('"waveform": 0', source)
        self.assertIn('"spectral": 1', source)
        self.assertIn('"spectrogram": 2', source)
        self.assertIn('"loudness": 3', source)
        self.assertIn("u_displayMode == 0", source)
        self.assertIn("u_displayMode == 1", source)
        self.assertIn("u_displayMode == 2", source)
        self.assertIn("u_displayMode == 3", source)

    def test_glsl_wrapper_smooths_spectrogram_and_wave_lines(self) -> None:
        source = read_source("pyside6_reaper_gl_view.py")
        self.assertIn("float sampleG(", source)
        self.assertIn("return mix(lo, hi, bt);", source)
        self.assertIn("fwidth(lineDistance)", source)
        self.assertIn("fwidth(lower)", source)
        self.assertIn("fwidth(upper)", source)

    def test_shader_patch_targets_still_exist(self) -> None:
        base = read_source("pyside6_gl_view.py")
        self.assertIn("if (u_hasG != 0 && u_gCount > 0)", base)
        self.assertIn("if (u_hasWave != 0 && u_waveCount > 0)", base)
        self.assertIn("if (u_pcmMode == 2 && u_pcmCount > 0)", base)
        self.assertIn("if (u_hasLoudness != 0 && u_rCount > 0)", base)

    def test_daw_toolbar_exposes_view_and_spectrogram_controls(self) -> None:
        source = read_source("pyside6_daw_player.py")
        self.assertIn('QLabel("View"', source)
        self.assertIn('QLabel("Intensity"', source)
        self.assertIn('QCheckBox("Heatmap"', source)
        self.assertIn('("Waveform", "waveform", "waveform")', source)
        self.assertIn('("Spectrogram", "spectrogram", "spectrogram")', source)
        self.assertIn("set_display_mode", source)

    def test_cache_prepare_always_generates_complete_spectrogram_mode(self) -> None:
        source = read_source("pyside6_prepare.py")
        self.assertIn('FULL_GENERATION_MODE: NativeGenerationMode = "spectrogram"', source)
        self.assertIn("generation_mode=FULL_GENERATION_MODE", source)
        self.assertNotIn("MODE_ROWS", source)
        self.assertNotIn("mode_combo", source)
        self.assertIn("waveform, spectral peaks, spectrogram, and loudness", source)

    def test_daw_player_has_empty_drop_open_launcher(self) -> None:
        source = read_source("pyside6_daw_player.py")
        self.assertIn("class DropLaunchWindow(QMainWindow):", source)
        self.assertIn("self.setAcceptDrops(True)", source)
        self.assertIn("def dragEnterEvent", source)
        self.assertIn("def dropEvent", source)
        self.assertIn("QProcess.startDetached", source)
        self.assertIn("if not args:", source)
        self.assertIn("Drop an audio file here", source)


if __name__ == "__main__":
    unittest.main()
