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


class AdversarialIniTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_bytes(self, payload: bytes) -> Path:
        path = self.root / "reaper.ini"
        path.write_bytes(payload)
        return path

    def test_utf8_bom_crlf_mixed_case_and_duplicate_keys(self) -> None:
        ini = self.write_bytes(
            b"\xef\xbb\xbf[REAPER]\r\n"
            b"PeakCacheGenRS=300\r\n"
            b"peakcachegenrs=501\r\n"
            b"ALTPEAKS=0x11\r\n"
            b"peakcachegenmode=0x3\r\n"
        )
        settings = rc.load_reaper_ini(ini)
        self.assertEqual(settings.peak_rate, 501)
        self.assertEqual(settings.altpeaks_flags, 0x11)
        self.assertEqual(settings.generation_mode, 3)

    def test_cp1252_ini_is_accepted(self) -> None:
        payload = (
            "[REAPER]\n"
            "peakcachegenrs=500\n"
            "altpeakspath=caf\xe9 peaks\n"
        ).encode("cp1252")
        settings = rc.load_reaper_ini(self.write_bytes(payload))
        self.assertEqual(settings.peak_rate, 500)
        self.assertEqual(
            settings.alternate_cache_path,
            (self.root / "caf\xe9 peaks").resolve(),
        )

    def test_numeric_boundaries_and_python_integer_bombs_fail_closed(self) -> None:
        valid = ["1", str(rc.MAX_PEAK_RATE), "+300", "0x12c"]
        for value in valid:
            with self.subTest(valid=value):
                ini = self.write_bytes(
                    f"[REAPER]\npeakcachegenrs={value}\n".encode()
                )
                rc.load_reaper_ini(ini)
        invalid = [
            "0",
            "-1",
            str(rc.MAX_PEAK_RATE + 1),
            "1e3",
            "nan",
            "inf",
            "_300",
            "0x",
            "9" * 10_000,
        ]
        for value in invalid:
            with self.subTest(invalid=value):
                ini = self.write_bytes(
                    f"[REAPER]\npeakcachegenrs={value}\n".encode()
                )
                with self.assertRaises(rc.ReaperConfigError):
                    rc.load_reaper_ini(ini)

    def test_path_list_normalizes_delimiters_deduplicates_and_ignores_empty(self) -> None:
        ini = self.write_bytes(
            b"[REAPER]\naltpeaksopathlist= one ; ;two|one;three;two \n"
        )
        settings = rc.load_reaper_ini(ini)
        self.assertEqual(
            settings.alternate_source_paths,
            tuple((self.root / name).resolve() for name in ("one", "two", "three")),
        )

    def test_nul_corrupted_ini_fails_closed(self) -> None:
        ini = self.write_bytes(b"[REAPER]\npeakcachegenrs=300\n\x00garbage\n")
        with self.assertRaises(rc.ReaperConfigError):
            rc.load_reaper_ini(ini)

    def test_quoted_and_environment_expanded_altpeakspath(self) -> None:
        ini = self.write_bytes(
            b'[REAPER]\naltpeakspath="$LIBREAPEAKS_TEST_CACHE/with space"\n'
        )
        with mock.patch.dict(
            "os.environ",
            {"LIBREAPEAKS_TEST_CACHE": str(self.root / "cache")},
            clear=False,
        ):
            settings = rc.load_reaper_ini(ini)
        self.assertEqual(
            settings.alternate_cache_path,
            (self.root / "cache/with space").resolve(),
        )

    def test_auto_discovery_falls_back_to_default_without_ini(self) -> None:
        with mock.patch.object(rc, "discover_reaper_ini", return_value=None):
            policy = rc.resolve_peak_rate(
                explicit_peak_rate=None,
                auto_reaper_ini=True,
            )
        self.assertEqual(policy.peak_rate, rc.DEFAULT_PEAK_RATE)
        self.assertEqual(policy.source, "default")
        self.assertIsNone(policy.settings)


class AdversarialCacheMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / "audio.wav"
        self.media.write_bytes(b"audio")
        self.central = self.root / "central"
        self.ini = self.root / "reaper.ini"
        self.ini.write_text(
            "[REAPER]\naltpeaks=1\n" f"altpeakspath={self.central}\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_map(self, payload: object) -> Path:
        path = self.root / "map.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_unsupported_declared_map_version_is_rejected(self) -> None:
        path = self.write_map(
            {
                "version": rc.CACHE_MAP_VERSION + 1,
                "entries": {
                    str(self.media): {"write": str(self.central / "a.reapeaks")}
                },
            }
        )
        with self.assertRaises(rc.ReaperConfigError):
            rc.load_reaper_cache_map(path)

    def test_map_field_types_and_missing_write_are_rejected(self) -> None:
        bad_entries = [
            {"write": 123},
            {"read": [], "write": "x"},
            {"write": "x", "source_type": {}},
            {"write": "x", "origin": 1},
            {"read": "", "write": ""},
            [],
            123,
            None,
        ]
        for entry in bad_entries:
            with self.subTest(entry=entry):
                path = self.write_map({"entries": {str(self.media): entry}})
                with self.assertRaises(rc.ReaperConfigError):
                    rc.load_reaper_cache_map(path)

    def test_central_validation_rejects_prefix_confusion(self) -> None:
        settings = rc.load_reaper_ini(self.ini)
        outside = self.root / "central-evil" / "cache.reapeaks"
        paths = rc.ReaperPeakPaths(
            media=self.media,
            read=None,
            write=outside,
        )
        with self.assertRaises(rc.ReaperConfigError):
            rc.validate_central_peak_paths(paths, settings)

    def test_central_validation_rejects_read_escape_even_when_write_is_valid(self) -> None:
        settings = rc.load_reaper_ini(self.ini)
        paths = rc.ReaperPeakPaths(
            media=self.media,
            read=self.root / "outside.reapeaks",
            write=self.central / "inside.reapeaks",
        )
        with self.assertRaises(rc.ReaperConfigError):
            rc.validate_central_peak_paths(paths, settings)

    def test_player_unknown_cache_mode_and_invalid_rate_fail_closed(self) -> None:
        with self.assertRaises(rc.ReaperConfigError):
            integration.resolve_player_peak_policy(
                self.media,
                cache_mode="../../escape",  # type: ignore[arg-type]
            )
        with self.assertRaises(rc.ReaperConfigError):
            integration.resolve_player_peak_policy(
                self.media,
                explicit_peak_rate=0,
            )


class AdversarialLiveQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / "audio.wav"
        self.media.write_bytes(b"audio")
        self.executable = self.root / "reaper"
        self.executable.write_bytes(b"fake")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_with_payload(self, payload: object, returncode: int = 0):
        def fake_run(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            environment = kwargs["env"]
            assert isinstance(environment, dict)
            result = Path(environment["LIBREAPEAKS_RESULT"])
            result.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(command, returncode, b"diagnostic")

        return fake_run

    def test_non_object_query_json_fails_closed(self) -> None:
        for payload in ([], [1, 2], "string", 123, None):
            with self.subTest(payload=payload):
                with mock.patch.object(
                    subprocess,
                    "run",
                    side_effect=self.run_with_payload(payload),
                ):
                    with self.assertRaises(rc.ReaperConfigError):
                        rc.query_reaper_peak_paths(
                            self.media,
                            reaper_executable=self.executable,
                        )

    def test_non_string_source_type_fails_closed(self) -> None:
        payload = {
            "write": str(self.root / "x.reapeaks"),
            "read": "",
            "source_type": {"unexpected": True},
        }
        with mock.patch.object(
            subprocess,
            "run",
            side_effect=self.run_with_payload(payload),
        ):
            with self.assertRaises(rc.ReaperConfigError):
                rc.query_reaper_peak_paths(
                    self.media,
                    reaper_executable=self.executable,
                )

    def test_timeout_zero_negative_and_actual_timeout_are_wrapped(self) -> None:
        for timeout in (0, -1, -0.001):
            with self.subTest(timeout=timeout):
                with self.assertRaises(rc.ReaperConfigError):
                    rc.query_reaper_peak_paths(
                        self.media,
                        reaper_executable=self.executable,
                        timeout=timeout,
                    )
        with mock.patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["reaper"], 0.01),
        ):
            with self.assertRaises(rc.ReaperConfigError):
                rc.query_reaper_peak_paths(
                    self.media,
                    reaper_executable=self.executable,
                    timeout=0.01,
                )

    def test_failed_process_or_missing_result_is_wrapped(self) -> None:
        with mock.patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["reaper"], 7, b"boom"),
        ):
            with self.assertRaises(rc.ReaperConfigError):
                rc.query_reaper_peak_paths(
                    self.media,
                    reaper_executable=self.executable,
                )


if __name__ == "__main__":
    unittest.main()
