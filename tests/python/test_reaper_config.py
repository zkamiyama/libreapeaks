from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "examples"))

import player_reaper_integration as integration  # noqa: E402
import reaper_config as rc  # noqa: E402


class ReaperIniTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_ini(self, body: str) -> Path:
        path = self.root / "reaper.ini"
        path.write_text("[REAPER]\n" + body, encoding="utf-8")
        return path

    def test_loads_peak_rate_and_resolves_relative_paths(self) -> None:
        ini = self.write_ini(
            "peakcachegenrs=500\n"
            "peakcachegenmode=3\n"
            "altpeaks=7\n"
            "altpeakspath=cache/peaks\n"
            "altpeaksopathlist=media one|media two;media three\n"
        )
        settings = rc.load_reaper_ini(ini)
        self.assertEqual(settings.peak_rate, 500)
        self.assertEqual(settings.generation_mode, 3)
        self.assertEqual(settings.altpeaks_flags, 7)
        self.assertEqual(
            settings.alternate_cache_path,
            (self.root / "cache/peaks").resolve(),
        )
        self.assertEqual(
            settings.alternate_source_paths,
            (
                (self.root / "media one").resolve(),
                (self.root / "media two").resolve(),
                (self.root / "media three").resolve(),
            ),
        )

    def test_invalid_numeric_values_fail_closed(self) -> None:
        for value in ("0", "-1", "not-a-number", str(rc.MAX_PEAK_RATE + 1)):
            ini = self.write_ini(f"peakcachegenrs={value}\n")
            with self.subTest(value=value):
                with self.assertRaises(rc.ReaperConfigError):
                    rc.load_reaper_ini(ini)

    def test_explicit_rate_overrides_ini(self) -> None:
        ini = self.write_ini("peakcachegenrs=500\n")
        policy = rc.resolve_peak_rate(
            explicit_peak_rate=100,
            reaper_ini=ini,
        )
        self.assertEqual(policy.peak_rate, 100)
        self.assertEqual(policy.source, "explicit")
        self.assertIsNotNone(policy.settings)
        self.assertEqual(policy.settings.peak_rate, 500)

    def test_platform_discovery_and_environment_override(self) -> None:
        explicit = self.root / "explicit.ini"
        explicit.write_text("[REAPER]\n", encoding="utf-8")
        found = rc.discover_reaper_ini(
            environment={"REAPER_INI": str(explicit)},
            platform="linux",
            home=self.root / "home",
        )
        self.assertEqual(found, explicit.resolve())

        appdata = self.root / "AppData/Roaming"
        windows_ini = appdata / "REAPER/reaper.ini"
        windows_ini.parent.mkdir(parents=True)
        windows_ini.write_text("[REAPER]\n", encoding="utf-8")
        found = rc.discover_reaper_ini(
            environment={"APPDATA": str(appdata)},
            platform="win32",
            home=self.root / "unused",
        )
        self.assertEqual(found, windows_ini.resolve())


class ReaperPathQueryAndMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / "audio.wav"
        self.media.write_bytes(b"not decoded by the path query")
        self.executable = self.root / "reaper"
        self.executable.write_bytes(b"fake")
        self.ini = self.root / "reaper.ini"
        self.ini.write_text(
            "[REAPER]\npeakcachegenrs=500\naltpeaks=1\n"
            f"altpeakspath={self.root / 'central'}\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fake_run(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        result = Path(env["LIBREAPEAKS_RESULT"])
        result.write_text(
            json.dumps(
                {
                    "media": env["LIBREAPEAKS_MEDIA"],
                    "read": "",
                    "write": str(self.root / "central" / "canonical.reapeaks"),
                    "source_type": "WAVE",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, b"ok")

    def test_live_query_only_resolves_paths(self) -> None:
        with mock.patch.object(subprocess, "run", side_effect=self.fake_run):
            paths = rc.query_reaper_peak_paths(
                self.media,
                reaper_executable=self.executable,
                reaper_ini=self.ini,
            )
        self.assertEqual(paths.media, self.media.resolve())
        self.assertIsNone(paths.read)
        self.assertEqual(
            paths.write,
            (self.root / "central/canonical.reapeaks").resolve(),
        )
        self.assertFalse(paths.write.exists())
        self.assertEqual(paths.origin, "GetPeakFileNameEx")

    def test_cache_map_round_trip_and_legacy_compatibility(self) -> None:
        canonical = rc.ReaperPeakPaths(
            media=self.media.resolve(),
            read=None,
            write=(self.root / "central/canonical.reapeaks").resolve(),
            source_type="WAVE",
        )
        with mock.patch.object(rc, "query_reaper_peak_paths", return_value=canonical):
            payload = rc.build_reaper_cache_map(
                [self.media],
                reaper_executable=self.executable,
                reaper_ini=self.ini,
            )
        target = rc.write_reaper_cache_map(self.root / "map.json", payload)
        loaded = rc.peak_paths_from_cache_map(self.media, target)
        self.assertEqual(loaded.write, canonical.write)

        legacy = self.root / "legacy.json"
        legacy.write_text(
            json.dumps({str(self.media.resolve()): str(canonical.write)}),
            encoding="utf-8",
        )
        loaded = rc.peak_paths_from_cache_map(self.media, legacy)
        self.assertEqual(loaded.read, canonical.write)
        self.assertEqual(loaded.write, canonical.write)

    def test_strict_central_validation_rejects_sidecar(self) -> None:
        settings = rc.load_reaper_ini(self.ini)
        outside = rc.ReaperPeakPaths(
            media=self.media.resolve(),
            read=None,
            write=(self.root / "audio.wav.reapeaks").resolve(),
        )
        with self.assertRaises(rc.ReaperConfigError):
            rc.validate_central_peak_paths(outside, settings)


class PlayerPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / "audio.wav"
        self.media.write_bytes(b"audio")
        self.central = self.root / "central"
        self.ini = self.root / "reaper.ini"
        self.ini.write_text(
            "[REAPER]\n"
            "peakcachegenrs=500\n"
            "altpeaks=1\n"
            f"altpeakspath={self.central}\n",
            encoding="utf-8",
        )
        self.map_path = self.root / "map.json"
        self.canonical = self.central / "canonical.reapeaks"
        self.map_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "entries": {
                        str(self.media.resolve()): {
                            "read": "",
                            "write": str(self.canonical),
                            "source_type": "WAVE",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_explicit_path_and_rate_have_highest_precedence(self) -> None:
        explicit = self.root / "chosen.reapeaks"
        policy = integration.resolve_player_peak_policy(
            self.media,
            explicit_peaks=explicit,
            explicit_peak_rate=100,
            reaper_ini=self.ini,
        )
        self.assertEqual(policy.peaks_path, explicit.resolve())
        self.assertEqual(policy.peak_rate, 100)
        self.assertEqual(policy.path_origin, "explicit")

    def test_central_mode_uses_canonical_map_path_and_ini_rate(self) -> None:
        policy = integration.resolve_player_peak_policy(
            self.media,
            cache_mode="central",
            reaper_cache_map=self.map_path,
            reaper_ini=self.ini,
        )
        self.assertEqual(policy.peaks_path, self.canonical.resolve())
        self.assertEqual(policy.peak_rate, 500)
        self.assertIsNotNone(policy.canonical_paths)

    def test_central_mode_without_exact_resolver_fails(self) -> None:
        with self.assertRaises(rc.ReaperConfigError):
            integration.resolve_player_peak_policy(
                self.media,
                cache_mode="central",
                cache_directory=self.central,
                reaper_ini=self.ini,
            )

    def test_auto_does_not_silently_ignore_alternate_ini_policy(self) -> None:
        with self.assertRaises(rc.ReaperConfigError):
            integration.resolve_player_peak_policy(
                self.media,
                cache_mode="auto",
                reaper_ini=self.ini,
            )

    def test_private_central_is_explicitly_separate(self) -> None:
        policy = integration.resolve_player_peak_policy(
            self.media,
            cache_mode="private-central",
            cache_directory=self.central,
            explicit_peak_rate=200,
        )
        self.assertEqual(policy.peaks_path.parent, self.central.resolve())
        self.assertIn(".reapeaks", policy.peaks_path.name)
        self.assertEqual(policy.path_origin, "libreapeaks-private-sha256")
        self.assertEqual(policy.peak_rate, 200)


if __name__ == "__main__":
    unittest.main()
