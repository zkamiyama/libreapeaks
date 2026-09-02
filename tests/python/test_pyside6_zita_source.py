from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


def read_source(name: str) -> str:
    path = ROOT / "examples" / name
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    return source


class PySideZitaSourceTests(unittest.TestCase):
    def test_zita_overlay_disables_fragment_pcm_and_uses_real_geometry(self) -> None:
        source = read_source("pyside6_zita_gl_view.py")
        self.assertIn("class ZitaGpuAnalysisCanvas", source)
        self.assertIn("false && u_displayMode == 0 && u_pcmMode == 1", source)
        self.assertIn("false && u_displayMode == 0 && u_pcmMode == 2", source)
        self.assertIn("QPainterPath", source)
        self.assertIn("pen.setCosmetic(True)", source)
        self.assertIn("Qt.PenJoinStyle.BevelJoin", source)
        self.assertNotIn("segmentDistancePx", source)
        self.assertNotIn("prefilteredCoverage", source)

    def test_zita_envelope_aggregates_actual_screen_columns_and_connects_tips(self) -> None:
        source = read_source("pyside6_zita_gl_view.py")
        self.assertIn("physical_width =", source)
        self.assertIn("maxima = [-math.inf] * physical_width", source)
        self.assertIn("minima = [math.inf] * physical_width", source)
        self.assertIn("if top >= next_bottom:", source)
        self.assertIn("if bottom <= next_top:", source)
        self.assertIn("path.lineTo(next_x, next_bottom)", source)
        self.assertIn("path.lineTo(next_x, next_top)", source)
        self.assertIn("path.lineTo(x, bottom)", source)

    def test_high_resolution_source_is_plain_polyline_without_points(self) -> None:
        source = read_source("pyside6_zita_gl_view.py")
        self.assertIn('if window.mode == "samples":', source)
        self.assertIn("def _paint_sample_polyline", source)
        self.assertIn("path.lineTo(x, y)", source)
        self.assertNotIn("drawEllipse", source)
        self.assertNotIn("pointFade", source)

    def test_daw_demo_selects_zita_canvas(self) -> None:
        source = read_source("pyside6_daw_player.py")
        self.assertIn("from pyside6_zita_gl_view import ZitaGpuAnalysisCanvas", source)
        self.assertIn("_base.ReaperGpuAnalysisCanvas = ZitaGpuAnalysisCanvas", source)


if __name__ == "__main__":
    unittest.main()
