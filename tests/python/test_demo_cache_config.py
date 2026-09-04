from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

import sys

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
if str(EXAMPLES) not in sys.path:
    sys.path.insert(0, str(EXAMPLES))

import demo_cache_config as dc  # noqa: E402


class DemoCacheConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media" / "Song.WAV"
        self.media.parent.mkdir(parents=True)
        self.media.write_bytes(b"RIFF-demo")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_ini(
        self,
        *,
        flags: int,
        cache: Path | None = None,
        source_list: str = "",
        rate: int = 500,
    ) -> Path:
        ini = self.root / "reaper.ini"
        ini.write_text(
            "[REAPER]\n"
            f"peakcachegenrs={rate}\n"
            "peakcachegenmode=3\n"
            f"altpeaks={flags}\n"
            f"altpeakspath={'' if cache is None else cache}\n"
            f"altpeaksopathlist={source_list}\n",
            encoding="utf-8",
        )
        return ini

    def test_default_is_sidecar(self) -> None:
        plan = dc.resolve_demo_cache_plan(self.media, dc.DemoCacheConfig())
        self.assertEqual(plan.policy, "sidecar")
        self.assertEqual(plan.peaks_path, Path(str(self.media.resolve()) + ".reapeaks"))
        self.assertEqual(plan.peak_rate, 300)

    def test_reaper_central_filename_is_offline_sha1(self) -> None:
        cache = self.root / "central"
        plan = dc.resolve_demo_cache_plan(
            self.media,
            dc.DemoCacheConfig(
                policy="reaper-central",
                cache_directory=str(cache),
            ),
        )
        source = str(self.media.resolve())
        digest = hashlib.sha1(source.lower().encode("utf-8")).hexdigest()
        self.assertEqual(
            plan.peaks_path,
            cache.resolve() / digest[:2] / f"{digest}.reapeaks",
        )
        self.assertEqual(plan.path_origin, "reaper-central-sha1")

    def test_follow_ini_global_alternate_cache_and_peak_rate(self) -> None:
        cache = self.root / "central"
        ini = self.write_ini(flags=1, cache=cache, rate=444)
        plan = dc.resolve_demo_cache_plan(
            self.media,
            dc.DemoCacheConfig(policy="reaper-config", reaper_ini=str(ini)),
        )
        self.assertTrue(str(plan.peaks_path).startswith(str(cache.resolve())))
        self.assertEqual(plan.peak_rate, 444)
        self.assertEqual(plan.path_origin, "reaper.ini-central-sha1")

    def test_follow_ini_default_sidecar(self) -> None:
        ini = self.write_ini(flags=0, rate=321)
        plan = dc.resolve_demo_cache_plan(
            self.media,
            dc.DemoCacheConfig(policy="reaper-config", reaper_ini=str(ini)),
        )
        self.assertEqual(plan.peaks_path, Path(str(self.media.resolve()) + ".reapeaks"))
        self.assertEqual(plan.peak_rate, 321)
        self.assertEqual(plan.path_origin, "reaper.ini-sidecar")

    def test_follow_ini_source_list_selects_central_offline(self) -> None:
        cache = self.root / "central"
        ini = self.write_ini(
            flags=0,
            cache=cache,
            source_list=str(self.media.parent),
        )
        plan = dc.resolve_demo_cache_plan(
            self.media,
            dc.DemoCacheConfig(policy="reaper-config", reaper_ini=str(ini)),
        )
        self.assertTrue(str(plan.peaks_path).startswith(str(cache.resolve())))
        self.assertEqual(plan.path_origin, "reaper.ini-central-sha1")

    def test_unknown_ini_flags_require_oracle_instead_of_guessing(self) -> None:
        ini = self.write_ini(flags=2, cache=self.root / "central")
        with self.assertRaisesRegex(dc.DemoConfigError, "does not reproduce"):
            dc.resolve_demo_cache_plan(
                self.media,
                dc.DemoCacheConfig(policy="reaper-config", reaper_ini=str(ini)),
            )

    def test_browser_upload_reaper_policy_is_rejected(self) -> None:
        upload_root = self.root / "libreapeaks-web-daw-test"
        upload_root.mkdir()
        upload = upload_root / "upload-deadbeef.wav"
        upload.write_bytes(b"RIFF-upload")
        with self.assertRaisesRegex(dc.DemoConfigError, "original absolute path"):
            dc.resolve_demo_cache_plan(
                upload,
                dc.DemoCacheConfig(
                    policy="reaper-central",
                    cache_directory=str(self.root / "central"),
                ),
            )

    def test_saved_config_round_trip(self) -> None:
        path = self.root / "config.json"
        config = dc.DemoCacheConfig(
            policy="reaper-central",
            cache_directory=str(self.root / "central"),
            peak_rate=777,
        )
        dc.save_demo_cache_config(config, path)
        self.assertEqual(dc.load_demo_cache_config(path), config)

    def test_cli_override_beats_saved_policy(self) -> None:
        config = dc.DemoCacheConfig(
            policy="reaper-central",
            cache_directory=str(self.root / "central"),
        )
        plan = dc.resolve_demo_cache_plan(
            self.media,
            config,
            legacy_cache_mode="subdir",
            explicit_peak_rate=222,
        )
        self.assertEqual(
            plan.peaks_path,
            self.media.parent.resolve() / "peaks" / (self.media.name + ".reapeaks"),
        )
        self.assertEqual(plan.peak_rate, 222)


if __name__ == "__main__":
    unittest.main()
