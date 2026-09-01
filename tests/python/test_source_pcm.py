from __future__ import annotations

from array import array
import concurrent.futures
import json
import math
from pathlib import Path
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "examples"))

import source_pcm as sp  # noqa: E402


def riff_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    return (
        chunk_id
        + struct.pack("<I", len(payload))
        + payload
        + (b"\0" if len(payload) & 1 else b"")
    )


def pcm16_values(frames: int, channels: int) -> list[int]:
    values: list[int] = []
    for frame in range(frames):
        for channel in range(channels):
            values.append(((frame * 7919 + channel * 1237) % 65536) - 32768)
    return values


def make_pcm16_wav(
    path: Path,
    *,
    frames: int = 5000,
    channels: int = 2,
    sample_rate: int = 48_000,
    split_data: bool = False,
) -> list[int]:
    values = pcm16_values(frames, channels)
    pcm = struct.pack("<" + "h" * len(values), *values)
    align = channels * 2
    fmt = struct.pack(
        "<HHIIHH",
        1,
        channels,
        sample_rate,
        sample_rate * align,
        align,
        16,
    )
    chunks = [riff_chunk(b"fmt ", fmt)]
    if split_data:
        # Deliberately split between frames. The WAV reader treats multiple
        # data chunks as one logical stream, matching the cache reader.
        split = align * 137 + 2
        chunks.extend(
            [riff_chunk(b"data", pcm[:split]), riff_chunk(b"data", pcm[split:])]
        )
    else:
        chunks.append(riff_chunk(b"data", pcm))
    body = b"WAVE" + b"".join(chunks)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return values


def make_extensible_pcm16_wav(path: Path, *, frames: int = 64) -> list[int]:
    channels = 2
    sample_rate = 48_000
    values = pcm16_values(frames, channels)
    pcm = struct.pack("<" + "h" * len(values), *values)
    align = channels * 2
    subformat = struct.pack("<H", 1) + bytes.fromhex(
        "000000001000800000aa00389b71"
    )
    fmt = (
        struct.pack(
            "<HHIIHHH",
            0xFFFE,
            channels,
            sample_rate,
            sample_rate * align,
            align,
            16,
            22,
        )
        + struct.pack("<HI", 16, 3)
        + subformat
    )
    body = b"WAVE" + riff_chunk(b"fmt ", fmt) + riff_chunk(b"data", pcm)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return values


def make_float32_wav(path: Path, values: list[float]) -> bytes:
    channels = 1
    sample_rate = 44_100
    align = 4
    pcm = struct.pack("<" + "f" * len(values), *values)
    fmt = struct.pack(
        "<HHIIHH",
        3,
        channels,
        sample_rate,
        sample_rate * align,
        align,
        32,
    )
    body = b"WAVE" + riff_chunk(b"fmt ", fmt) + riff_chunk(b"data", pcm)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return pcm


def unpack_f32(raw: bytes) -> list[float]:
    values = array("f")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    return list(values)


class SourcePcmUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.wav = self.root / "source.wav"
        self.values = make_pcm16_wav(self.wav, split_data=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_direct_wav_reads_only_requested_logical_frames(self) -> None:
        reader = sp.WavPcmWindowReader(self.wav)
        window = reader.read_window(130, 20)
        self.assertEqual(window.first_frame, 130)
        self.assertEqual(window.frame_count, 20)
        self.assertEqual(window.byte_count, 20 * 2 * 4)
        expected = [value / 32768.0 for value in self.values[130 * 2 : 150 * 2]]
        self.assertEqual(unpack_f32(window.pcm_f32le), expected)

    def test_direct_wav_clamps_at_eof(self) -> None:
        reader = sp.WavPcmWindowReader(self.wav)
        window = reader.read_window(4997, 100)
        self.assertEqual(window.frame_count, 3)
        self.assertEqual(window.byte_count, 3 * 2 * 4)

    def test_direct_wav_supports_extensible_pcm16(self) -> None:
        path = self.root / "extensible.wav"
        expected_i16 = make_extensible_pcm16_wav(path)
        window = sp.WavPcmWindowReader(path).read_window(7, 11)
        expected = [value / 32768.0 for value in expected_i16[14:36]]
        self.assertEqual(unpack_f32(window.pcm_f32le), expected)

    def test_direct_float32_wav_preserves_source_values(self) -> None:
        path = self.root / "float.wav"
        values = [-1.25, -0.0, 0.125, 1.0, 2.5]
        pcm = make_float32_wav(path, values)
        reader = sp.WavPcmWindowReader(path)
        self.assertEqual(reader.info.codec, "pcm_f32le")
        self.assertEqual(reader.read_window(0, len(values)).pcm_f32le, pcm)

    def test_source_envelope_uses_exact_bucket_extrema(self) -> None:
        reader = sp.WavPcmWindowReader(self.wav)
        raw = reader.read_window(128, 16)
        display = sp.build_pcm_display_window(raw, 8)
        self.assertEqual(display.mode, "envelope")
        self.assertEqual(display.components, 2)
        self.assertEqual(display.record_count, 2)
        got = unpack_f32(display.data_f32le)
        samples = unpack_f32(raw.pcm_f32le)
        expected: list[float] = []
        for bucket in range(2):
            for channel in range(2):
                lane = [
                    samples[frame * 2 + channel]
                    for frame in range(bucket * 8, bucket * 8 + 8)
                ]
                expected.extend([max(lane), min(lane)])
        self.assertEqual(got, expected)

    def test_sample_display_is_zero_copy_bytes_and_exact(self) -> None:
        reader = sp.WavPcmWindowReader(self.wav)
        raw = reader.read_window(50, 7)
        display = sp.build_pcm_display_window(raw, 1)
        self.assertEqual(display.mode, "samples")
        self.assertEqual(display.components, 1)
        self.assertIs(display.data_f32le, raw.pcm_f32le)

    def test_draw_plan_exposes_line_point_and_interleaved_sample_geometry(self) -> None:
        raw = sp.WavPcmWindowReader(self.wav).read_window(50, 7)
        display = sp.build_pcm_display_window(raw, 1)
        draw = sp.plan_pcm_draw(display, 52, 55, 12)
        self.assertTrue(draw.draw_lines)
        self.assertTrue(draw.draw_points)
        self.assertEqual(draw.first_visible_record, 1)
        self.assertEqual(draw.visible_record_count, 6)
        self.assertEqual(draw.x_origin_px, -4.0)
        self.assertEqual(draw.x_step_px, 4.0)
        self.assertEqual(draw.x_for_local_record(2), 4.0)
        self.assertEqual(draw.sample_offset(0, 1), 3)
        with self.assertRaises(IndexError):
            draw.x_for_local_record(draw.visible_record_count)
        self.assertFalse(sp.plan_pcm_draw(display, 52, 55, 6).draw_points)
        self.assertEqual(
            sp.pcm_display_values(display),
            array("f", unpack_f32(display.data_f32le)),
        )

    def test_lod_switch_hysteresis_sample_mode_and_memory_cap(self) -> None:
        # fine division 160 at 100 frames/pixel => 1.6 px/fine peak: enter.
        entered = sp.plan_pcm_lod(
            0,
            100_000,
            1000,
            2_000_000,
            2,
            160,
            target_page_bytes=2 * 1024 * 1024,
            max_window_bytes=8 * 1024 * 1024,
        )
        self.assertTrue(entered.active)
        self.assertEqual(entered.mode, "envelope")
        self.assertEqual(entered.division, 128)

        # At 1.2 px/peak a cold viewport stays cached, while an already-active
        # viewport remains on source PCM until the 1.1 exit threshold.
        cold = sp.plan_pcm_lod(
            0, 160_000, 1200, 2_000_000, 2, 160, source_active=False
        )
        warm = sp.plan_pcm_lod(
            0, 160_000, 1200, 2_000_000, 2, 160, source_active=True
        )
        self.assertFalse(cold.active)
        self.assertTrue(warm.active)
        self.assertLessEqual(warm.division, 160)

        samples = sp.plan_pcm_lod(
            1000, 2000, 2000, 2_000_000, 2, 160, source_active=True
        )
        self.assertTrue(samples.active)
        self.assertEqual(samples.mode, "samples")
        self.assertEqual(samples.division, 1)
        self.assertLessEqual(
            (samples.frame_count + samples.division - 1) // samples.division,
            sp.DEFAULT_PCM_MAX_TEXTURE_RECORDS,
        )

        capped = sp.plan_pcm_lod(
            0,
            100_000,
            1000,
            2_000_000,
            8,
            160,
            max_window_bytes=1024,
        )
        self.assertFalse(capped.active)
        self.assertEqual(capped.reason, "source byte budget")

        # A 4k-wide, eight-channel viewport near a page boundary must fit one
        # sliding bounded window. Naively rounding both ends to fixed 2048-
        # record pages would require three pages and disable source LOD.
        adversarial = sp.plan_pcm_lod(
            256_000,
            665_600,
            4096,
            2_000_000,
            8,
            160,
            max_window_bytes=16 * 1024 * 1024,
            target_page_bytes=4 * 1024 * 1024,
            max_texture_records=4096,
        )
        self.assertTrue(adversarial.active)
        self.assertLessEqual(adversarial.first_frame, 256_000)
        self.assertGreaterEqual(
            adversarial.first_frame + adversarial.frame_count, 665_600
        )
        self.assertLessEqual(adversarial.frame_count * 8 * 4, 16 * 1024 * 1024)

    def test_service_enforces_window_limit_before_read(self) -> None:
        reader = sp.WavPcmWindowReader(self.wav)
        service = sp.SourcePcmService(
            reader,
            cache_bytes=4096,
            max_window_bytes=1024,
            target_page_bytes=512,
            expected_sample_rate=48_000,
            expected_channels=2,
        )
        with self.assertRaises(sp.PlayerCacheError):
            service.display_window(0, 1000, 1)
        self.assertEqual(service.cache.loads, 0)

    def test_callback_reader_adapts_a_host_playback_pcm_cache(self) -> None:
        calls: list[tuple[int, int]] = []

        def host_pcm(first: int, count: int) -> bytes:
            calls.append((first, count))
            values = array(
                "f",
                (
                    (first + frame) / 100.0 + channel
                    for frame in range(count)
                    for channel in range(2)
                ),
            )
            if sys.byteorder != "little":
                values.byteswap()
            return values.tobytes()

        reader = sp.CallbackPcmWindowReader(
            host_pcm,
            sample_rate=48_000,
            channels=2,
            total_frames=1000,
            backend="playback-block-cache",
        )
        service = sp.SourcePcmService(
            reader,
            cache_bytes=0,
            max_window_bytes=4096,
            target_page_bytes=128,
        )
        display = service.display_window(100, 8, 1)
        self.assertEqual(calls, [(96, 16)])
        self.assertEqual(display.backend, "playback-block-cache")
        self.assertEqual(display.frame_count, 8)
        expected = array(
            "f",
            (
                (100 + frame) / 100.0 + channel
                for frame in range(8)
                for channel in range(2)
            ),
        )
        self.assertEqual(
            unpack_f32(display.data_f32le),
            list(expected),
        )
        assert display.range_event is not None
        self.assertTrue(display.range_event.reader_ran)
        self.assertEqual(service.cache.item_count, 0)

    def test_callback_reader_preserves_a_host_cache_hit_notification(self) -> None:
        def host_pcm(first: int, count: int) -> sp.PcmWindowReadResult:
            values = array("f", (0.25 for _index in range(count)))
            if sys.byteorder != "little":
                values.byteswap()
            return sp.PcmWindowReadResult(
                sp.PcmWindow(
                    first,
                    count,
                    48_000,
                    1,
                    values.tobytes(),
                    "shared-playback-cache",
                ),
                cache_disposition="cache-hit",
                reader_ran=False,
            )

        service = sp.SourcePcmService(
            sp.CallbackPcmWindowReader(
                host_pcm,
                sample_rate=48_000,
                channels=1,
                total_frames=1000,
            ),
            cache_bytes=0,
            max_window_bytes=4096,
            target_page_bytes=128,
        )
        display = service.display_window(100, 8, 1)
        assert display.range_event is not None
        self.assertEqual(display.range_event.cache_disposition, "cache-hit")
        self.assertFalse(display.range_event.reader_ran)
        self.assertTrue(display.raw_cache_hit)

    def test_nonfinite_source_samples_are_safe_for_cpu_and_envelope_drawing(self) -> None:
        payload = struct.pack("<ffff", math.nan, math.inf, -math.inf, 0.5)
        raw = sp.PcmWindow(0, 4, 48_000, 1, payload, "test")

        samples = sp.build_pcm_display_window(raw, 1)
        unsanitized = sp.pcm_display_values(samples, sanitize_nonfinite=False)
        self.assertTrue(math.isnan(unsanitized[0]))
        self.assertTrue(math.isinf(unsanitized[1]))
        self.assertEqual(list(sp.pcm_display_values(samples)), [0.0, 0.0, 0.0, 0.5])

        envelope = sp.build_pcm_display_window(raw, 2)
        self.assertEqual(unpack_f32(envelope.data_f32le), [0.0, 0.0, 0.5, 0.0])

    def test_display_and_draw_helpers_reject_inconsistent_geometry(self) -> None:
        invalid_windows = [
            sp.PcmWindow(-1, 1, 48_000, 1, b"\0" * 4, "test"),
            sp.PcmWindow(0, -1, 48_000, 1, b"", "test"),
            sp.PcmWindow(0, 1, 0, 1, b"\0" * 4, "test"),
            sp.PcmWindow(0, 1, 48_000, 0, b"", "test"),
            sp.PcmWindow(0, 1, 48_000, 1, b"\0" * 3, "test"),
            sp.PcmWindow(0, 1, 48_000, 1, b"\0" * 4, "bad\nheader"),
        ]
        for window in invalid_windows:
            with self.subTest(window=window), self.assertRaises(sp.PlayerCacheError):
                sp.build_pcm_display_window(window, 1)

        good = sp.build_pcm_display_window(
            sp.PcmWindow(0, 4, 48_000, 1, b"\0" * 16, "test"), 1
        )
        for kwargs in (
            {"point_min_pixels_per_frame": math.nan},
            {"point_radius_px": math.inf},
            {"line_width_px": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(sp.PlayerCacheError):
                sp.plan_pcm_draw(good, 0, 4, 100, **kwargs)

    def test_service_rejects_empty_and_eof_requests_before_reader_io(self) -> None:
        reader = _CountingReader()
        service = sp.SourcePcmService(
            reader,
            cache_bytes=4096,
            max_window_bytes=4096,
            target_page_bytes=1024,
        )
        with self.assertRaises(sp.PlayerCacheError):
            service.display_window(0, 0, 1)
        with self.assertRaises(sp.PlayerCacheError):
            service.display_window(10_000, 1, 1)
        self.assertEqual(reader.calls, 0)

        with self.assertRaises(sp.PlayerCacheError):
            sp.SourcePcmService(
                reader,
                max_window_bytes=3,
                target_page_bytes=1,
            )

    def test_callback_and_service_reject_header_unsafe_diagnostics(self) -> None:
        with self.assertRaises(sp.PlayerCacheError):
            sp.CallbackPcmWindowReader(
                lambda _first, _count: b"",
                sample_rate=48_000,
                channels=1,
                total_frames=1,
                backend="unsafe\r\nX-Evil: yes",
            )

        reader = sp.CallbackPcmWindowReader(
            lambda first, count: sp.PcmWindow(
                first, count, 48_000, 1, b"\0" * (count * 4), "unsafe\nbackend"
            ),
            sample_rate=48_000,
            channels=1,
            total_frames=8,
        )
        service = sp.SourcePcmService(
            reader,
            cache_bytes=0,
            max_window_bytes=32,
            target_page_bytes=16,
        )
        with self.assertRaises(sp.PlayerCacheError):
            service.display_window(0, 1, 1)

    def test_wav_chunk_count_limit_and_random_garbage_corpus(self) -> None:
        bomb = self.root / "chunk-bomb.wav"
        chunks = b"".join(
            riff_chunk(b"JUNK", b"") for _ in range(sp._MAX_WAV_CHUNKS + 1)
        )
        body = b"WAVE" + chunks
        bomb.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
        with self.assertRaisesRegex(sp.PlayerCacheError, "chunk safety limit"):
            sp.WavPcmWindowReader(bomb)

        rng = random.Random(0xA11D10)
        garbage = self.root / "garbage.wav"
        for case in range(500):
            garbage.write_bytes(rng.randbytes(rng.randrange(0, 2049)))
            with self.subTest(case=case), self.assertRaises(sp.PlayerCacheError):
                sp.WavPcmWindowReader(garbage)

    def test_structured_malformed_wav_corpus_is_rejected(self) -> None:
        channels = 2
        sample_rate = 48_000
        align = channels * 2
        valid_fmt = struct.pack(
            "<HHIIHH",
            1,
            channels,
            sample_rate,
            sample_rate * align,
            align,
            16,
        )

        def riff_payload(chunks: bytes) -> bytes:
            body = b"WAVE" + chunks
            return b"RIFF" + struct.pack("<I", len(body)) + body

        bad_rate = struct.pack(
            "<HHIIHH",
            1,
            channels,
            sample_rate,
            1,
            align,
            16,
        )
        conflicting = bytearray(valid_fmt)
        conflicting[4:8] = struct.pack("<I", 44_100)
        cases = {
            "declared RIFF beyond EOF": (
                b"RIFF" + struct.pack("<I", 10_000) + b"WAVE"
            ),
            "truncated chunk header": riff_payload(b"JUNK"),
            "chunk size beyond RIFF": riff_payload(
                b"JUNK" + struct.pack("<I", 100) + b"x"
            ),
            "missing odd chunk padding": riff_payload(
                b"JUNK" + struct.pack("<I", 1) + b"x"
            ),
            "oversized fmt": riff_payload(
                riff_chunk(b"fmt ", b"\0" * (sp._MAX_WAV_FMT_BYTES + 1))
                + riff_chunk(b"data", b"")
            ),
            "conflicting fmt": riff_payload(
                riff_chunk(b"fmt ", valid_fmt)
                + riff_chunk(b"fmt ", bytes(conflicting))
                + riff_chunk(b"data", b"")
            ),
            "inconsistent byte rate": riff_payload(
                riff_chunk(b"fmt ", bad_rate) + riff_chunk(b"data", b"\0" * 4)
            ),
            "partial interleaved frame": riff_payload(
                riff_chunk(b"fmt ", valid_fmt) + riff_chunk(b"data", b"\0" * 3)
            ),
            "unsupported sample representation": riff_payload(
                riff_chunk(
                    b"fmt ",
                    struct.pack(
                        "<HHIIHH",
                        1,
                        channels,
                        sample_rate,
                        sample_rate * channels * 3,
                        channels * 3,
                        24,
                    ),
                )
                + riff_chunk(b"data", b"\0" * 6)
            ),
        }
        path = self.root / "structured-invalid.wav"
        for name, payload in cases.items():
            path.write_bytes(payload)
            with self.subTest(name=name), self.assertRaises(sp.PlayerCacheError):
                sp.WavPcmWindowReader(path)

    def test_randomized_envelopes_match_exact_bucket_extrema(self) -> None:
        rng = random.Random(0xE17E)
        for case in range(1000):
            channels = rng.randint(1, 8)
            frames = rng.randint(1, 96)
            division = rng.randint(2, 24)
            values = array(
                "f", (rng.uniform(-4.0, 4.0) for _ in range(frames * channels))
            )
            if sys.byteorder != "little":
                values.byteswap()
            payload = values.tobytes()
            decoded = unpack_f32(payload)
            first = rng.randint(0, 100) * division
            display = sp.build_pcm_display_window(
                sp.PcmWindow(first, frames, 48_000, channels, payload, "fuzz"),
                division,
            )
            expected: list[float] = []
            for bucket in range(0, frames, division):
                for channel in range(channels):
                    lane = [
                        decoded[frame * channels + channel]
                        for frame in range(bucket, min(frames, bucket + division))
                    ]
                    expected.extend((max(lane), min(lane)))
            with self.subTest(case=case):
                self.assertEqual(unpack_f32(display.data_f32le), expected)

    def test_randomized_service_windows_match_direct_source_across_page_edges(self) -> None:
        reader = sp.WavPcmWindowReader(self.wav)
        service = sp.SourcePcmService(
            reader,
            cache_bytes=8192,
            max_window_bytes=4096,
            target_page_bytes=2048,
        )
        rng = random.Random(0x51CED)
        for case in range(1000):
            division = rng.choice((1, 2, 3, 4, 8, 16, 31, 64))
            first = rng.randrange(0, 5000)
            first -= first % division
            count = rng.randint(1, min(400, 5000 - first))
            actual = service.display_window(first, count, division)
            expected = sp.build_pcm_display_window(
                reader.read_window(first, count), division
            )
            with self.subTest(case=case):
                self.assertEqual(actual.first_frame, expected.first_frame)
                self.assertEqual(actual.frame_count, expected.frame_count)
                self.assertEqual(actual.record_count, expected.record_count)
                self.assertEqual(actual.data_f32le, expected.data_f32le)
                self.assertIsNotNone(actual.range_event)
                self.assertLessEqual(service.cache.resident_bytes, 8192)

    def test_randomized_lod_plans_respect_all_hard_bounds(self) -> None:
        rng = random.Random(0x10D5AFE)
        for case in range(5000):
            total = rng.randint(1, 20_000_000)
            start_input = rng.randint(-total, total * 2)
            end_input = start_input + rng.randint(-1000, max(1, total // 2))
            width = rng.randint(1, 8192)
            channels = rng.randint(1, 32)
            fine = rng.choice((1, 2, 3, 16, 64, 160, 256, 1024))
            max_window = rng.randint(1, 32) * 1024 * 1024
            target_page = rng.randint(1, 48) * 1024 * 1024
            max_records = rng.randint(1, 8192)
            plan = sp.plan_pcm_lod(
                start_input,
                end_input,
                width,
                total,
                channels,
                fine,
                source_active=bool(rng.getrandbits(1)),
                max_window_bytes=max_window,
                target_page_bytes=target_page,
                max_texture_records=max_records,
            )
            self.assertTrue(math.isfinite(plan.frames_per_pixel))
            self.assertTrue(math.isfinite(plan.pixels_per_fine_peak))
            if not plan.active:
                self.assertIsNone(plan.key)
                continue
            start = min(max(0, start_input), total - 1)
            end = min(max(start + 1, end_input), total)
            with self.subTest(case=case):
                self.assertGreater(plan.frame_count, 0)
                self.assertGreater(plan.division, 0)
                self.assertLessEqual(plan.division, fine)
                self.assertEqual(plan.first_frame % plan.division, 0)
                self.assertLessEqual(plan.first_frame, start)
                self.assertGreaterEqual(plan.first_frame + plan.frame_count, end)
                self.assertLessEqual(plan.frame_count * channels * 4, max_window)
                self.assertLessEqual(
                    math.ceil(plan.frame_count / plan.division), max_records
                )
                self.assertEqual(
                    plan.mode, "samples" if plan.division == 1 else "envelope"
                )

    def test_lod_planner_rejects_nonfinite_and_impossible_configuration(self) -> None:
        valid = dict(
            view_start=0,
            view_end=100,
            width=100,
            total_frames=1000,
            channels=2,
            fine_division=160,
        )
        invalid = (
            {"total_frames": 0},
            {"width": 0},
            {"channels": 0},
            {"fine_division": 0},
            {"max_window_bytes": 0},
            {"target_page_bytes": 0},
            {"max_texture_records": 0},
            {"enter_pixels_per_peak": math.nan},
            {"exit_pixels_per_peak": math.inf},
            {"enter_pixels_per_peak": 1.0, "exit_pixels_per_peak": 1.1},
        )
        for override in invalid:
            arguments = {**valid, **override}
            with self.subTest(override=override), self.assertRaises(sp.PlayerCacheError):
                sp.plan_pcm_lod(**arguments)

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_python_and_javascript_lod_planners_match_randomized_corpus(self) -> None:
        rng = random.Random(0xBADC0DE)
        cases: list[dict[str, object]] = []
        for _case in range(1000):
            total = rng.randint(1, 20_000_000)
            start = rng.randint(-total, total * 2)
            cases.append(
                {
                    "viewStart": start,
                    "viewEnd": start
                    + rng.randint(-1000, max(1, total // 2)),
                    "width": rng.randint(1, 8192),
                    "totalFrames": total,
                    "channels": rng.randint(1, 32),
                    "fineDivision": rng.choice((1, 2, 3, 16, 64, 160, 256, 1024)),
                    "sourceActive": bool(rng.getrandbits(1)),
                    "maxWindowBytes": rng.randint(1, 32) * 1024 * 1024,
                    "targetPageBytes": rng.randint(1, 48) * 1024 * 1024,
                    "maxTextureRecords": rng.randint(1, 8192),
                }
            )
        module_url = (REPO / "examples/web_player/webgl2_renderer.mjs").as_uri()
        script = (
            "import fs from 'node:fs';"
            f"import {{planPcmLod}} from {json.dumps(module_url)};"
            "const cases=JSON.parse(fs.readFileSync(0,'utf8'));"
            "process.stdout.write(JSON.stringify(cases.map(planPcmLod)));"
        )
        completed = subprocess.run(
            [shutil.which("node") or "node", "--input-type=module", "-e", script],
            input=json.dumps(cases).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", "replace"),
        )
        javascript = json.loads(completed.stdout)
        for case, actual in zip(cases, javascript, strict=True):
            plan = sp.plan_pcm_lod(
                case["viewStart"],
                case["viewEnd"],
                case["width"],
                case["totalFrames"],
                case["channels"],
                case["fineDivision"],
                source_active=case["sourceActive"],
                max_window_bytes=case["maxWindowBytes"],
                target_page_bytes=case["targetPageBytes"],
                max_texture_records=case["maxTextureRecords"],
            )
            self.assertEqual(actual["active"], plan.active)
            self.assertEqual(actual["mode"], plan.mode)
            self.assertEqual(actual["division"], plan.division)
            self.assertEqual(actual["firstFrame"], plan.first_frame)
            self.assertEqual(actual["frameCount"], plan.frame_count)
            self.assertEqual(actual["reason"], plan.reason)
            self.assertAlmostEqual(
                actual["framesPerPixel"], plan.frames_per_pixel, places=12
            )
            self.assertAlmostEqual(
                actual["pixelsPerFinePeak"],
                plan.pixels_per_fine_peak,
                places=12,
            )


class _CountingReader(sp.PcmWindowReader):
    def __init__(self) -> None:
        self.info = sp.PcmSourceInfo(
            Path("fake"), 48_000, 1, 10_000, "fake", "fake"
        )
        self.calls = 0
        self.lock = threading.Lock()

    def read_window(self, first_frame: int, frame_count: int) -> sp.PcmWindow:
        with self.lock:
            self.calls += 1
        time.sleep(0.03)
        values = array("f", (float(first_frame + index) for index in range(frame_count)))
        if sys.byteorder != "little":
            values.byteswap()
        return sp.PcmWindow(
            first_frame,
            frame_count,
            48_000,
            1,
            values.tobytes(),
            "fake",
        )


class SourcePcmCacheTests(unittest.TestCase):
    def test_concurrent_identical_windows_share_one_decode(self) -> None:
        reader = _CountingReader()
        cache = sp.PcmWindowLru(reader, 4096)
        barrier = threading.Barrier(8)

        def get_window(_n):
            barrier.wait()
            return cache.get(100, 128)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(get_window, range(8)))
        self.assertEqual(reader.calls, 1)
        self.assertTrue(all(window.frame_count == 128 for window, _hit in results))
        accesses = [access for _window, access in results]
        self.assertEqual(sum(access.reader_ran for access in accesses), 1)
        self.assertEqual(sum(access.disposition == "coalesced" for access in accesses), 7)
        self.assertEqual(cache.loads, 1)
        self.assertEqual(cache.hits, 0)
        self.assertEqual(cache.coalesced, 7)

    def test_zero_capacity_still_coalesces_an_inflight_decode(self) -> None:
        reader = _CountingReader()
        cache = sp.PcmWindowLru(reader, 0)
        barrier = threading.Barrier(8)

        def get_window(_n):
            barrier.wait()
            return cache.get(100, 128)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(get_window, range(8)))
        self.assertEqual(reader.calls, 1)
        self.assertEqual(sum(access.reader_ran for _window, access in results), 1)
        self.assertEqual(cache.item_count, 0)

    def test_lru_is_byte_bounded(self) -> None:
        reader = _CountingReader()
        cache = sp.PcmWindowLru(reader, 1024)
        for first in (0, 256, 512):
            cache.get(first, 256)
        self.assertLessEqual(cache.resident_bytes, 1024)
        self.assertEqual(cache.item_count, 1)

        disabled = sp.PcmWindowLru(reader, 0)
        disabled.get(0, 0)
        self.assertEqual(disabled.item_count, 0)

    def test_service_reuses_a_larger_raw_page_for_adjacent_display_windows(self) -> None:
        reader = _CountingReader()
        service = sp.SourcePcmService(
            reader,
            cache_bytes=4096,
            max_window_bytes=4096,
            target_page_bytes=1024,
        )
        first = service.display_window(100, 16, 1)
        second = service.display_window(120, 16, 1)
        self.assertEqual(reader.calls, 1)
        self.assertFalse(first.raw_cache_hit)
        self.assertTrue(second.raw_cache_hit)
        self.assertIsNotNone(first.range_event)
        self.assertIsNotNone(second.range_event)
        assert first.range_event is not None and second.range_event is not None
        self.assertTrue(first.range_event.reader_ran)
        self.assertEqual(first.range_event.cache_disposition, "decoded")
        self.assertFalse(second.range_event.reader_ran)
        self.assertEqual(second.range_event.cache_disposition, "cache-hit")
        self.assertGreater(second.range_event.event_id, first.range_event.event_id)
        self.assertEqual(first.range_event.raw_first_frame, 0)
        self.assertEqual(first.range_event.raw_frame_count, 256)
        self.assertEqual(unpack_f32(second.data_f32le), [float(i) for i in range(120, 136)])

    def test_all_coalesced_waiters_are_released_after_decode_failure(self) -> None:
        class FlakyReader(_CountingReader):
            def read_window(self, first_frame: int, frame_count: int) -> sp.PcmWindow:
                with self.lock:
                    self.calls += 1
                    call = self.calls
                time.sleep(0.03)
                if call == 1:
                    raise sp.PlayerCacheError("intentional decode failure")
                return sp.PcmWindow(
                    first_frame,
                    frame_count,
                    48_000,
                    1,
                    b"\0" * (frame_count * 4),
                    "fake",
                )

        reader = FlakyReader()
        cache = sp.PcmWindowLru(reader, 4096)
        barrier = threading.Barrier(8)

        def request() -> tuple[sp.PcmWindow, sp.PcmCacheAccess]:
            barrier.wait()
            return cache.get(100, 128)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(request) for _ in range(8)]
            for future in futures:
                with self.assertRaisesRegex(sp.PlayerCacheError, "intentional"):
                    future.result(timeout=5)
        self.assertEqual(reader.calls, 1)
        self.assertEqual(cache.stats()["pending"], 0)
        self.assertEqual(cache.loads, 0)

        window, access = cache.get(100, 128)
        self.assertEqual(window.frame_count, 128)
        self.assertTrue(access.reader_ran)
        self.assertEqual(reader.calls, 2)

    def test_unique_load_concurrency_is_bounded(self) -> None:
        class ConcurrencyReader(_CountingReader):
            def __init__(self) -> None:
                super().__init__()
                self.active = 0
                self.maximum_active = 0

            def read_window(self, first_frame: int, frame_count: int) -> sp.PcmWindow:
                with self.lock:
                    self.calls += 1
                    self.active += 1
                    self.maximum_active = max(self.maximum_active, self.active)
                try:
                    time.sleep(0.04)
                    return sp.PcmWindow(
                        first_frame,
                        frame_count,
                        48_000,
                        1,
                        b"\0" * (frame_count * 4),
                        "fake",
                    )
                finally:
                    with self.lock:
                        self.active -= 1

        reader = ConcurrencyReader()
        cache = sp.PcmWindowLru(
            reader,
            0,
            max_pending_windows=16,
            max_concurrent_loads=2,
        )
        barrier = threading.Barrier(8)

        def request(index: int):
            barrier.wait()
            return cache.get(index * 32, 32)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(request, range(8)))
        self.assertEqual(len(results), 8)
        self.assertEqual(reader.calls, 8)
        self.assertEqual(reader.maximum_active, 2)
        self.assertEqual(cache.stats()["pending"], 0)

    def test_pending_window_flood_is_rejected_without_poisoning_cache(self) -> None:
        class BlockingReader(_CountingReader):
            def __init__(self) -> None:
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()

            def read_window(self, first_frame: int, frame_count: int) -> sp.PcmWindow:
                with self.lock:
                    self.calls += 1
                self.entered.set()
                if not self.release.wait(5):
                    raise sp.PlayerCacheError("test release timed out")
                return sp.PcmWindow(
                    first_frame,
                    frame_count,
                    48_000,
                    1,
                    b"\0" * (frame_count * 4),
                    "fake",
                )

        reader = BlockingReader()
        cache = sp.PcmWindowLru(
            reader,
            0,
            max_pending_windows=2,
            max_concurrent_loads=1,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cache.get, 0, 16)
            self.assertTrue(reader.entered.wait(5))
            second = pool.submit(cache.get, 32, 16)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and cache.stats()["pending"] != 2:
                time.sleep(0.005)
            self.assertEqual(cache.stats()["pending"], 2)
            with self.assertRaisesRegex(sp.PlayerCacheError, "too many pending"):
                cache.get(64, 16)
            reader.release.set()
            self.assertEqual(first.result(timeout=5)[0].first_frame, 0)
            self.assertEqual(second.result(timeout=5)[0].first_frame, 32)
        self.assertEqual(cache.rejected, 1)
        self.assertEqual(cache.stats()["pending"], 0)

    def test_zero_byte_entries_are_item_bounded(self) -> None:
        class EmptyReader(_CountingReader):
            def read_window(self, first_frame: int, frame_count: int) -> sp.PcmWindow:
                with self.lock:
                    self.calls += 1
                return sp.PcmWindow(first_frame, 0, 48_000, 1, b"", "fake")

        reader = EmptyReader()
        cache = sp.PcmWindowLru(reader, 1024, max_items=32)
        for first in range(200):
            cache.get(first, 1)
        self.assertEqual(cache.resident_bytes, 0)
        self.assertEqual(cache.item_count, 32)

    def test_reentrant_same_key_fails_fast_and_can_retry(self) -> None:
        class ReentrantReader(_CountingReader):
            cache: sp.PcmWindowLru

            def __init__(self) -> None:
                super().__init__()
                self.reenter = True

            def read_window(self, first_frame: int, frame_count: int) -> sp.PcmWindow:
                with self.lock:
                    self.calls += 1
                if self.reenter:
                    self.cache.get(first_frame, frame_count)
                return sp.PcmWindow(
                    first_frame,
                    frame_count,
                    48_000,
                    1,
                    b"\0" * (frame_count * 4),
                    "fake",
                )

        reader = ReentrantReader()
        cache = sp.PcmWindowLru(reader, 4096)
        reader.cache = cache
        with self.assertRaisesRegex(sp.PlayerCacheError, "reentrant"):
            cache.get(100, 16)
        self.assertEqual(cache.stats()["pending"], 0)
        reader.reenter = False
        self.assertEqual(cache.get(100, 16)[0].frame_count, 16)

    def test_malformed_reader_results_never_enter_the_lru(self) -> None:
        class ResultReader(sp.PcmWindowReader):
            def __init__(self, result) -> None:
                self.info = sp.PcmSourceInfo(
                    Path("fake"), 48_000, 1, 10_000, "fake", "fake"
                )
                self.result = result

            def read_window(self, first_frame: int, frame_count: int):
                return self.result(first_frame, frame_count)

        window = lambda first, count: sp.PcmWindow(  # noqa: E731
            first, count, 48_000, 1, b"\0" * (count * 4), "fake"
        )
        cases = {
            "wrong first": lambda first, count: window(first + 1, count),
            "negative count": lambda first, _count: sp.PcmWindow(
                first, -1, 48_000, 1, b"", "fake"
            ),
            "excess count": lambda first, count: window(first, count + 1),
            "sample rate": lambda first, count: sp.PcmWindow(
                first, count, 44_100, 1, b"\0" * (count * 4), "fake"
            ),
            "channels": lambda first, count: sp.PcmWindow(
                first, count, 48_000, 2, b"\0" * (count * 8), "fake"
            ),
            "payload length": lambda first, count: sp.PcmWindow(
                first, count, 48_000, 1, b"\0" * max(0, count * 4 - 1), "fake"
            ),
            "payload type": lambda first, count: sp.PcmWindow(
                first, count, 48_000, 1, object(), "fake"  # type: ignore[arg-type]
            ),
            "unsafe backend": lambda first, count: sp.PcmWindow(
                first, count, 48_000, 1, b"\0" * (count * 4), "bad\nheader"
            ),
            "unknown result": lambda _first, _count: object(),
            "unknown disposition": lambda first, count: sp.PcmWindowReadResult(
                window(first, count), "mystery", False  # type: ignore[arg-type]
            ),
            "non-boolean flag": lambda first, count: sp.PcmWindowReadResult(
                window(first, count), "decoded", 1  # type: ignore[arg-type]
            ),
            "inconsistent hit": lambda first, count: sp.PcmWindowReadResult(
                window(first, count), "cache-hit", True
            ),
            "inconsistent decode": lambda first, count: sp.PcmWindowReadResult(
                window(first, count), "decoded", False
            ),
        }
        for name, result in cases.items():
            cache = sp.PcmWindowLru(ResultReader(result), 4096)
            with self.subTest(name=name), self.assertRaises(sp.PlayerCacheError):
                cache.get(100, 4)
            self.assertEqual(cache.item_count, 0)
            self.assertEqual(cache.stats()["pending"], 0)

        mutable = bytearray(b"\0" * 16)
        cache = sp.PcmWindowLru(
            ResultReader(
                lambda first, count: sp.PcmWindow(
                    first, count, 48_000, 1, mutable, "fake"  # type: ignore[arg-type]
                )
            ),
            4096,
        )
        loaded, _access = cache.get(0, 4)
        mutable[0] = 255
        self.assertIsInstance(loaded.pcm_f32le, bytes)
        self.assertEqual(loaded.pcm_f32le[0], 0)

    def test_cache_limit_configuration_is_validated(self) -> None:
        reader = _CountingReader()
        invalid = (
            {"capacity_bytes": -1},
            {"capacity_bytes": 0, "max_items": 0},
            {"capacity_bytes": 0, "max_pending_windows": 0},
            {"capacity_bytes": 0, "max_concurrent_loads": 0},
        )
        for arguments in invalid:
            options = dict(arguments)
            capacity = options.pop("capacity_bytes")
            with self.subTest(arguments=options), self.assertRaises(sp.PlayerCacheError):
                sp.PcmWindowLru(reader, capacity, **options)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
class SourcePcmFfmpegTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.wav = self.root / "source.wav"
        make_pcm16_wav(self.wav, frames=12_000)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ffmpeg_accurate_seek_matches_direct_pcm_windows(self) -> None:
        direct = sp.WavPcmWindowReader(self.wav)
        ffmpeg = sp.FfmpegPcmWindowReader(self.wav, total_frames_hint=12_000)
        for first, count in ((0, 97), (1, 257), (1234, 511), (11_900, 100)):
            with self.subTest(first=first):
                expected = direct.read_window(first, count)
                actual = ffmpeg.read_window(first, count)
                self.assertEqual(actual.frame_count, expected.frame_count)
                self.assertEqual(actual.pcm_f32le, expected.pcm_f32le)

    def test_ffmpeg_serializes_decodes_and_supersedes_stale_waiters(self) -> None:
        reader = sp.FfmpegPcmWindowReader(self.wav, total_frames_hint=12_000)
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def fake_run(command, **_kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
                call = calls
            if call == 1:
                entered.set()
                self.assertTrue(release.wait(5.0))
            return subprocess.CompletedProcess(command, 0, b"\0" * (10 * 2 * 4), b"")

        with mock.patch.object(sp.subprocess, "run", side_effect=fake_run):
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(reader.read_window, 0, 10)
                self.assertTrue(entered.wait(5.0))
                stale = pool.submit(reader.read_window, 100, 10)
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    with reader._decode_condition:
                        if reader._decode_generation >= 2:
                            break
                    time.sleep(0.005)
                self.assertGreaterEqual(reader._decode_generation, 2)
                latest = pool.submit(reader.read_window, 200, 10)
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    with reader._decode_condition:
                        if reader._decode_generation >= 3:
                            break
                    time.sleep(0.005)
                self.assertGreaterEqual(reader._decode_generation, 3)
                release.set()
                self.assertEqual(first.result(timeout=5).frame_count, 10)
                with self.assertRaises(sp.PlayerCacheError):
                    stale.result(timeout=5)
                self.assertEqual(latest.result(timeout=5).first_frame, 200)
        self.assertEqual(calls, 2)

    def test_ffmpeg_command_has_independent_output_bound(self) -> None:
        reader = sp.FfmpegPcmWindowReader(self.wav, total_frames_hint=12_000)
        captured: list[str] = []

        def fake_run(command, **_kwargs):
            captured.extend(command)
            return subprocess.CompletedProcess(command, 0, b"\0" * (17 * 2 * 4), b"")

        with mock.patch.object(sp.subprocess, "run", side_effect=fake_run):
            window = reader.read_window(123, 17)
        self.assertEqual(window.frame_count, 17)
        self.assertIn(
            "atrim=start_sample=123:end_sample=140,asetpts=PTS-STARTPTS",
            captured,
        )
        fs_index = captured.index("-fs")
        self.assertEqual(captured[fs_index + 1], str(17 * 2 * 4))

    def test_ffmpeg_timeout_partial_frame_and_output_overrun_are_rejected(self) -> None:
        reader = sp.FfmpegPcmWindowReader(self.wav, total_frames_hint=12_000)
        failures = (
            subprocess.TimeoutExpired(["ffmpeg"], 1),
            subprocess.CompletedProcess(["ffmpeg"], 0, b"\0" * 7, b""),
            subprocess.CompletedProcess(["ffmpeg"], 0, b"\0" * (11 * 2 * 4), b""),
            subprocess.CompletedProcess(["ffmpeg"], 7, b"", b"x" * (70 * 1024)),
        )
        for failure in failures:
            with mock.patch.object(
                sp.subprocess,
                "run",
                side_effect=failure if isinstance(failure, BaseException) else None,
                return_value=None if isinstance(failure, BaseException) else failure,
            ):
                with self.subTest(failure=type(failure).__name__), self.assertRaises(
                    sp.PlayerCacheError
                ):
                    reader.read_window(100, 10)
            with reader._decode_condition:
                self.assertFalse(reader._decode_active)

    def test_lossless_compressed_window_matches_full_decoded_timeline(self) -> None:
        flac = self.root / "source.flac"
        completed = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(self.wav),
                "-c:a",
                "flac",
                str(flac),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if completed.returncode != 0:
            self.skipTest("FFmpeg FLAC encoder unavailable")
        direct = sp.WavPcmWindowReader(self.wav)
        reader = sp.FfmpegPcmWindowReader(flac, total_frames_hint=12_000)
        for first in (37, 4097, 10_321):
            self.assertEqual(
                reader.read_window(first, 333).pcm_f32le,
                direct.read_window(first, 333).pcm_f32le,
            )

    def test_lossy_window_matches_the_same_ffmpeg_decoded_timeline(self) -> None:
        mp3 = self.root / "source.mp3"
        encoder = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(self.wav),
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(mp3),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if encoder.returncode != 0:
            self.skipTest("FFmpeg MP3 encoder unavailable")
        decoded = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(mp3),
                "-map",
                "0:a:0",
                "-c:a",
                "pcm_f32le",
                "-f",
                "f32le",
                "pipe:1",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode(errors="replace"))
        frame_bytes = 2 * 4
        self.assertEqual(len(decoded.stdout) % frame_bytes, 0)
        total_frames = len(decoded.stdout) // frame_bytes
        reader = sp.FfmpegPcmWindowReader(mp3, total_frames_hint=total_frames)
        for first in (0, 1, 1000, total_frames // 2, total_frames - 257):
            count = min(257, total_frames - first)
            expected = decoded.stdout[
                first * frame_bytes : (first + count) * frame_bytes
            ]
            with self.subTest(first=first):
                self.assertEqual(reader.read_window(first, count).pcm_f32le, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
