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
        self.assertIn("fwidth(lower)", source)
        self.assertIn("fwidth(upper)", source)

    def test_glsl_source_pcm_uses_neighbor_screen_space_segments(self) -> None:
        source = read_source("pyside6_reaper_gl_view.py")
        self.assertIn("float segmentDistancePx(", source)
        self.assertIn("float prefilteredCoverage(", source)
        self.assertIn("float pcmNeighborSegmentDistancePx(", source)
        self.assertIn("for (int offset = -1; offset <= 1; ++offset)", source)
        self.assertIn("best = min(best, segmentDistancePx", source)
        self.assertIn("abs(dFdx(position))", source)
        self.assertIn("abs(dFdy(amplitude))", source)
        self.assertIn("pcmNeighborSegmentDistancePx(", source)
        self.assertIn("prefilteredCoverage(distancePx, 0.55)", source)
        self.assertIn("smoothstep(8.0, 11.0, pixelsPerFrame)", source)
        self.assertNotIn("fwidth(lineDistance)", source)

    def test_spectral_peaks_color_the_waveform_by_frequency_and_tonality(self) -> None:
        source = read_source("pyside6_reaper_gl_view.py")
        self.assertIn("vec3 spectralCodeColor(uint code)", source)
        self.assertIn("float frequency = float(code & 0x7fffu);", source)
        self.assertIn("float tonality = float((code >> 15u) & 0x3fffu)", source)
        self.assertIn("u_spectralRangeMode", source)
        self.assertIn("u_spectralReverse", source)
        self.assertIn("u_spectralFadeNoise", source)
        self.assertIn("vec3 peakColor = spectralColorAt", source)
        self.assertIn("inside * u_analysisOpacity", source)
        self.assertIn("mx *= u_peakDisplayGain;", source)

    def test_loudness_supports_colored_peaks_and_single_lufs_graph(self) -> None:
        source = read_source("pyside6_reaper_gl_view.py")
        self.assertIn("vec3 loudnessColor(float lu)", source)
        self.assertIn("u_loudnessMetric", source)
        self.assertIn("u_loudnessView", source)
        self.assertIn("u_loudnessOffsetLu", source)
        self.assertIn("u_loudnessTransitionLu", source)
        self.assertIn('REAPER\'s "normal peaks + LUFS graph"', source)
        self.assertIn("selectedEnergy = u_loudnessMetric == 0 ? energy.r : energy.g", source)
        self.assertIn("color = mix(color, luColor, inside * u_analysisOpacity)", source)
        self.assertIn("graph * u_analysisOpacity", source)

    def test_daw_waveform_uses_filled_minmax_geometry(self) -> None:
        source = read_source("pyside6_zita_gl_view.py")
        self.assertIn("def _disable_fragment_waveform", source)
        self.assertIn("false && u_displayMode == 0 && u_hasWave", source)
        self.assertIn("def _paint_source_contours", source)
        self.assertIn("def _paint_packed_contours", source)
        self.assertIn("def _draw_filled_minmax", source)
        self.assertIn("painter.fillPath(fill_path, color)", source)
        self.assertIn("max_path.lineTo(point)", source)
        self.assertIn("min_path.lineTo(point)", source)
        self.assertIn("pen.setCosmetic(True)", source)
        self.assertIn("pen.setWidthF(1.0)", source)
        self.assertIn("SAMPLE_POINT_MIN_DEVICE_PX = 8.0", source)
        self.assertNotIn("_paint_zita_envelope", source)
        self.assertNotIn("vertical extents and connecting", source)

    def test_glsl_waveform_uses_pixel_extrema_and_stable_lod(self) -> None:
        source = read_source("pyside6_reaper_gl_view.py")
        self.assertIn("void wavePixelExtrema(", source)
        self.assertIn("dFdx(recordPosition)", source)
        self.assertIn("mx = max(mx,", source)
        self.assertIn("mn = min(mn,", source)
        self.assertIn("coarsest cache level that is no coarser than one pixel", source)
        self.assertIn("float(item[1][0])) <= desired", source)
        self.assertIn("return max(eligible", source)

    def test_shader_patch_targets_still_exist(self) -> None:
        base = read_source("pyside6_gl_view.py")
        self.assertIn("if (u_hasG != 0 && u_gCount > 0)", base)
        self.assertIn("if (u_hasSpectral != 0 && u_sCount > 0)", base)
        self.assertIn("if (u_hasWave != 0 && u_waveCount > 0)", base)
        self.assertIn("if (u_pcmMode == 2 && u_pcmCount > 0)", base)
        self.assertIn("if (u_hasLoudness != 0 && u_rCount > 0)", base)

    def test_daw_toolbar_exposes_reaper_like_mode_parameters(self) -> None:
        source = read_source("pyside6_daw_player.py")
        self.assertIn('QLabel("View"', source)
        self.assertIn('("Waveform", "waveform", "waveform")', source)
        self.assertIn('("Spectral peaks", "spectral", "spectral")', source)
        self.assertIn('("Spectrogram", "spectrogram", "spectrogram")', source)
        self.assertIn('("Loudness", "loudness", "loudness")', source)
        self.assertIn('QLabel("Peak zoom"', source)
        self.assertIn('QLabel("Opacity"', source)
        self.assertIn('self.spectral_range.addItem("Full spectrum", 0)', source)
        self.assertIn('self.spectral_range.addItem("Every octave", 1)', source)
        self.assertIn('QCheckBox("Reverse"', source)
        self.assertIn('QCheckBox("Fade noise"', source)
        self.assertIn('self.loudness_metric.addItem("LUFS-M", 0)', source)
        self.assertIn('self.loudness_metric.addItem("LUFS-S", 1)', source)
        self.assertIn('self.loudness_style.addItem("Graph + peaks", 1)', source)
        self.assertIn('self.loudness_style.addItem("Colored peaks", 0)', source)
        self.assertIn('QLabel("Transition"', source)
        self.assertIn("def _update_analysis_controls(", source)

    def test_cache_prepare_always_generates_complete_spectrogram_mode(self) -> None:
        source = read_source("pyside6_prepare.py")
        self.assertIn('FULL_GENERATION_MODE: NativeGenerationMode = "spectrogram"', source)
        self.assertIn("generation_mode=FULL_GENERATION_MODE", source)
        self.assertNotIn("MODE_ROWS", source)
        self.assertNotIn("mode_combo", source)
        self.assertIn("waveform, spectral peaks, spectrogram, and loudness", source)

    def test_daw_player_drop_open_prepares_inline(self) -> None:
        source = read_source("pyside6_daw_player.py")
        self.assertIn("class DropLaunchWindow(QMainWindow):", source)
        self.assertIn("self.setAcceptDrops(True)", source)
        self.assertIn("def dragEnterEvent", source)
        self.assertIn("def dropEvent", source)
        self.assertIn("QProgressBar", source)
        self.assertIn("CacheWorker(audio, _default_cache_options())", source)
        self.assertIn("worker.progress.connect(self._on_progress)", source)
        self.assertIn("self._begin_prepare(path)", source)
        self.assertIn("self.status.setText(\"Opening player…\")", source)
        self.assertNotIn("QProcess.startDetached", source)
        self.assertIn("if not args:", source)
        self.assertIn("Drop an audio file here", source)


if __name__ == "__main__":
    unittest.main()
