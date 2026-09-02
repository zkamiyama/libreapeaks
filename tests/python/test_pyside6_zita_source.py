from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


def read_source(name: str) -> str:
    path = ROOT / "examples" / name
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    return source


class PySideZitaSourceTests(unittest.TestCase):
    def test_contour_overlay_disables_fragment_waveform_and_pcm(self) -> None:
        source = read_source("pyside6_zita_gl_view.py")
        self.assertIn("class ZitaGpuAnalysisCanvas", source)
        self.assertIn("def _disable_fragment_waveform", source)
        self.assertIn("false && u_displayMode == 0 && u_hasWave", source)
        self.assertIn("false && u_displayMode == 0 && u_pcmMode == 1", source)
        self.assertIn("false && u_displayMode == 0 && u_pcmMode == 2", source)
        self.assertIn("QPainterPath", source)
        self.assertIn("pen.setCosmetic(True)", source)
        self.assertIn("pen.setWidthF(1.0)", source)
        self.assertIn("Qt.PenJoinStyle.BevelJoin", source)
        self.assertNotIn("segmentDistancePx", source)
        self.assertNotIn("prefilteredCoverage", source)

    def test_source_envelope_is_filled_between_minmax_contours(self) -> None:
        source = read_source("pyside6_zita_gl_view.py")
        self.assertIn("def _paint_source_contours", source)
        self.assertIn('if window.mode == "samples":', source)
        self.assertIn("maxima: list[QPointF] = []", source)
        self.assertIn("minima: list[QPointF] = []", source)
        self.assertIn("def _draw_filled_minmax", source)
        self.assertIn("for point in reversed(minima):", source)
        self.assertIn("fill_path.closeSubpath()", source)
        self.assertIn("painter.fillPath(fill_path, color)", source)
        self.assertNotIn("physical_width =", source)
        self.assertNotIn("_paint_zita_envelope", source)

    def test_high_resolution_source_collapses_to_plain_polyline(self) -> None:
        source = read_source("pyside6_zita_gl_view.py")
        self.assertIn('if window.mode == "samples":', source)
        self.assertIn("path = QPainterPath()", source)
        self.assertIn("path.lineTo(x, y)", source)
        self.assertNotIn("drawEllipse", source)
        self.assertNotIn("pointFade", source)

    def test_packed_cache_uses_same_filled_minmax_geometry(self) -> None:
        source = read_source("pyside6_zita_gl_view.py")
        self.assertIn("def _paint_packed_contours", source)
        self.assertIn('self.gpu.records(\n            "waveform",', source)
        self.assertIn('struct.unpack_from("<hh", payload, offset)', source)
        self.assertIn("self._draw_filled_minmax(painter, maxima, minima, self.CACHE_COLOR)", source)
        self.assertIn("CACHE_COLOR", source)
        self.assertIn("SOURCE_COLOR", source)

    def test_stale_source_window_is_not_drawn_after_lod_exit(self) -> None:
        source = read_source("pyside6_zita_gl_view.py")
        self.assertIn("if loader is None or self._pcm_upload is None or not loader.source_active:", source)
        self.assertIn("requested = loader.requested_plan", source)
        self.assertIn("ready = loader.ready_plan", source)
        self.assertIn("or ready.key != requested.key", source)
        self.assertIn("or self._pcm_upload.key != requested.key", source)

    def test_daw_demo_selects_zita_canvas(self) -> None:
        source = read_source("pyside6_daw_player.py")
        self.assertIn("from pyside6_zita_gl_view import ZitaGpuAnalysisCanvas", source)
        self.assertIn("_base.ReaperGpuAnalysisCanvas = ZitaGpuAnalysisCanvas", source)


if __name__ == "__main__":
    unittest.main()
