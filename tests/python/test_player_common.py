from __future__ import annotations

import concurrent.futures
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "examples"))

import player_common as pc  # noqa: E402
import reapeaks  # noqa: E402


def riff_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    assert len(chunk_id) == 4
    return chunk_id + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")


def pcm16_bytes(sample_rate: int, channels: int, frames: int) -> bytes:
    values: list[int] = []
    for frame in range(frames):
        for channel in range(channels):
            phase = 2.0 * math.pi * (127.0 + channel * 73.0) * frame / sample_rate
            value = int(round(30000.0 * math.sin(phase)))
            # Exercise the exact signed endpoints too.
            if frame == 0 and channel == 0:
                value = -32768
            elif frame == 1 and channel == 0:
                value = 32767
            values.append(value)
    return struct.pack("<" + "h" * len(values), *values)


def make_pcm16_wav(
    path: Path,
    *,
    sample_rate: int = 44_100,
    channels: int = 2,
    frames: int = 5000,
    odd_junk: bool = False,
    split_data: bool = False,
    extensible: bool = False,
) -> bytes:
    pcm = pcm16_bytes(sample_rate, channels, frames)
    block_align = channels * 2
    if extensible:
        # KSDATAFORMAT_SUBTYPE_PCM, little endian.
        guid = struct.pack("<IHH", 1, 0, 0x0010) + bytes.fromhex("800000aa00389b71")
        fmt = struct.pack(
            "<HHIIHHHHI16s",
            0xFFFE,
            channels,
            sample_rate,
            sample_rate * block_align,
            block_align,
            16,
            22,
            16,
            0,
            guid,
        )
    else:
        fmt = struct.pack(
            "<HHIIHH", 1, channels, sample_rate, sample_rate * block_align, block_align, 16
        )
    chunks = [riff_chunk(b"fmt ", fmt)]
    if odd_junk:
        chunks.append(riff_chunk(b"JUNK", b"abc"))
    if split_data:
        midpoint = (len(pcm) // (2 * block_align)) * block_align
        chunks.append(riff_chunk(b"data", pcm[:midpoint]))
        chunks.append(riff_chunk(b"data", pcm[midpoint:]))
    else:
        chunks.append(riff_chunk(b"data", pcm))
    body = b"WAVE" + b"".join(chunks)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return pcm


def run_ffmpeg(input_path: Path, output_path: Path, codec_args: list[str]) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    completed = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            *codec_args,
            str(output_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )
    return completed.returncode == 0 and output_path.is_file()


class PlayerCommonUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.audio = self.root / "tone.wav"
        make_pcm16_wav(self.audio)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_wav_reader_handles_odd_padding_and_multiple_data_chunks(self) -> None:
        path = self.root / "chunks.wav"
        expected = make_pcm16_wav(path, odd_junk=True, split_data=True, frames=333)
        decoded = pc.read_wav_cache_input(path)
        self.assertEqual(decoded.pcm_bytes, expected)
        self.assertEqual(decoded.frames, 333)
        self.assertEqual(decoded.sample_type, "i16")

    def test_wav_reader_handles_extensible_pcm(self) -> None:
        path = self.root / "extensible.wav"
        expected = make_pcm16_wav(path, extensible=True, channels=6, frames=40)
        decoded = pc.read_wav_cache_input(path)
        self.assertEqual(decoded.pcm_bytes, expected)
        self.assertEqual(decoded.channels, 6)

    def test_wav_reader_rejects_truncation_and_bad_alignment(self) -> None:
        truncated = self.root / "truncated.wav"
        data = self.audio.read_bytes()
        truncated.write_bytes(data[:-1])
        with self.assertRaises(pc.PlayerCacheError):
            pc.read_wav_cache_input(truncated)

        malformed = self.root / "misaligned.wav"
        body = b"WAVE" + riff_chunk(
            b"fmt ", struct.pack("<HHIIHH", 1, 2, 8000, 32000, 4, 16)
        ) + riff_chunk(b"data", b"\0\0")
        malformed.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
        with self.assertRaises(pc.PlayerCacheError):
            pc.read_wav_cache_input(malformed)

    def test_cache_path_policies_do_not_collide(self) -> None:
        self.assertEqual(pc.sidecar_peak_path(self.audio), self.root / "tone.wav.reapeaks")
        self.assertEqual(pc.subdir_peak_path(self.audio), self.root / "peaks/tone.wav.reapeaks")
        shared = self.root / "cache"
        first = pc.central_peak_path(self.audio, shared)
        other_dir = self.root / "other"
        other_dir.mkdir()
        other = other_dir / self.audio.name
        other.write_bytes(self.audio.read_bytes())
        second = pc.central_peak_path(other, shared)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, shared)
        self.assertTrue(first.name.endswith(".reapeaks"))

    def test_reaper_cache_map_json_and_tsv(self) -> None:
        mapped = self.root / "reaper-cache" / "mapped.REAPEAKS"
        key = str(self.audio.resolve())
        json_map = self.root / "map.json"
        json_map.write_text(
            json.dumps({"version": 1, "entries": {key: {"read": "", "write": str(mapped)}}}),
            encoding="utf-8",
        )
        self.assertEqual(pc.reaper_mapped_peak_path(self.audio, json_map, for_write=True), mapped)
        self.assertEqual(pc.reaper_mapped_peak_path(self.audio, json_map, for_write=False), mapped)

        tsv_map = self.root / "map.tsv"
        tsv_map.write_text(f"{key}\t{mapped}\t{mapped}\n", encoding="utf-8")
        self.assertEqual(pc.reaper_mapped_peak_path(self.audio, tsv_map, for_write=False), mapped)

    def test_generate_reuse_and_stale_rebuild(self) -> None:
        peaks, generated = pc.ensure_reapeaks(self.audio, decoder="wav", spectral=False)
        self.assertTrue(generated)
        self.assertTrue(peaks.is_file())
        parsed = reapeaks.ReaPeaks.open(str(peaks))
        self.assertIsNotNone(parsed)
        header = pc.read_reapeaks_header(peaks)
        self.assertEqual(header.magic, b"RPKN")
        self.assertEqual(header.sample_rate, 44_100)
        self.assertEqual(header.channels, 2)
        self.assertTrue(pc.inspect_reapeaks_cache(peaks, self.audio).fresh)

        reused, generated_again = pc.ensure_reapeaks(self.audio, decoder="wav", spectral=False)
        self.assertEqual(reused, peaks)
        self.assertFalse(generated_again)

        old_payload = peaks.read_bytes()
        stat = self.audio.stat()
        os.utime(self.audio, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        self.assertFalse(pc.inspect_reapeaks_cache(peaks, self.audio).fresh)
        rebuilt, did_rebuild = pc.ensure_reapeaks(self.audio, decoder="wav", spectral=False)
        self.assertEqual(rebuilt, peaks)
        self.assertTrue(did_rebuild)
        self.assertNotEqual(rebuilt.read_bytes(), old_payload)  # header mtime changed
        self.assertTrue(pc.inspect_reapeaks_cache(peaks, self.audio).fresh)

    def test_invalid_cache_is_never_reused(self) -> None:
        peaks = pc.sidecar_peak_path(self.audio)
        peaks.write_bytes(b"RPKN")
        result, generated = pc.ensure_reapeaks(self.audio, decoder="wav", spectral=False)
        self.assertEqual(result, peaks)
        self.assertTrue(generated)
        reapeaks.ReaPeaks.open(str(peaks))

    def test_central_mode_writes_only_to_shared_directory(self) -> None:
        shared = self.root / "shared-peaks"
        result, generated = pc.ensure_reapeaks(
            self.audio,
            decoder="wav",
            spectral=False,
            cache_mode="central",
            cache_directory=shared,
        )
        self.assertTrue(generated)
        self.assertEqual(result.parent, shared)
        self.assertFalse(pc.sidecar_peak_path(self.audio).exists())

    def test_reaper_mode_writes_exact_mapped_path(self) -> None:
        mapped = self.root / "alternate" / "REAPER-choice.reapeaks"
        mapping = self.root / "map.json"
        mapping.write_text(
            json.dumps({str(self.audio.resolve()): str(mapped)}), encoding="utf-8"
        )
        result, generated = pc.ensure_reapeaks(
            self.audio,
            decoder="wav",
            spectral=False,
            cache_mode="reaper",
            reaper_cache_map=mapping,
        )
        self.assertTrue(generated)
        self.assertEqual(result, mapped)
        self.assertTrue(mapped.is_file())

    def test_concurrent_generation_never_publishes_partial_file(self) -> None:
        target = self.root / "concurrent.reapeaks"

        def build() -> tuple[Path, bool]:
            return pc.ensure_reapeaks(
                self.audio,
                target,
                decoder="wav",
                spectral=False,
                lock_timeout=20.0,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _n: build(), range(4)))
        self.assertTrue(all(path == target for path, _generated in results))
        self.assertEqual(sum(1 for _path, generated in results if generated), 1)
        self.assertFalse(target.with_name(target.name + ".lock").exists())
        reapeaks.ReaPeaks.open(str(target))

    def test_source_change_during_decode_aborts_without_publication(self) -> None:
        target = self.root / "changed.reapeaks"
        real_decode = pc.decode_audio_for_cache

        def changing_decode(*args: object, **kwargs: object) -> pc.AudioCacheInput:
            result = real_decode(*args, **kwargs)
            with self.audio.open("ab") as handle:
                handle.write(b"x")
            return result

        with mock.patch.object(pc, "decode_audio_for_cache", side_effect=changing_decode):
            with self.assertRaises(pc.PlayerCacheError):
                pc.ensure_reapeaks(self.audio, target, decoder="wav", spectral=False)
        self.assertFalse(target.exists())

    def test_max_decode_limit_and_stale_lock(self) -> None:
        with self.assertRaises(pc.PlayerCacheError):
            pc.read_wav_cache_input(self.audio, max_decode_bytes=16)

        target = self.root / "stale-lock.reapeaks"
        lock = target.with_name(target.name + ".lock")
        lock.write_text("abandoned", encoding="ascii")
        stale_time = time.time() - 3600
        os.utime(lock, (stale_time, stale_time))
        result, generated = pc.ensure_reapeaks(
            self.audio,
            target,
            decoder="wav",
            spectral=False,
            lock_timeout=2.0,
        )
        self.assertTrue(generated)
        self.assertEqual(result, target)
        self.assertFalse(lock.exists())


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
class PlayerCommonFFmpegTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.wav = self.root / "source.wav"
        make_pcm16_wav(self.wav, frames=9000)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pcm16_wav_native_and_ffmpeg_waveform_bytes_are_identical(self) -> None:
        native = self.root / "native.reapeaks"
        via_ffmpeg = self.root / "ffmpeg.reapeaks"
        pc.ensure_reapeaks(
            self.wav,
            native,
            decoder="wav",
            spectral=False,
            wave_encoding="rpkn",
        )
        pc.ensure_reapeaks(
            self.wav,
            via_ffmpeg,
            decoder="ffmpeg",
            spectral=False,
            wave_encoding="rpkn",
        )
        self.assertEqual(native.read_bytes(), via_ffmpeg.read_bytes())

    def test_ffmpeg_decode_is_deterministic(self) -> None:
        first = pc.decode_with_ffmpeg(self.wav)
        second = pc.decode_with_ffmpeg(self.wav)
        self.assertEqual(first.sample_rate, second.sample_rate)
        self.assertEqual(first.channels, second.channels)
        self.assertEqual(first.pcm_bytes, second.pcm_bytes)

    def test_compressed_sources_generate_rpkl_and_reopen(self) -> None:
        candidates = [
            (self.root / "source.flac", ["-c:a", "flac"], b"RPKN"),
            (self.root / "source.mp3", ["-c:a", "libmp3lame", "-q:a", "2"], b"RPKL"),
            (self.root / "source.ogg", ["-c:a", "libvorbis", "-q:a", "5"], b"RPKL"),
            (self.root / "source.opus", ["-c:a", "libopus", "-b:a", "96k"], b"RPKL"),
        ]
        made = 0
        for compressed, codec_args, expected_magic in candidates:
            if not run_ffmpeg(self.wav, compressed, codec_args):
                continue
            made += 1
            peaks = compressed.with_name(compressed.name + ".reapeaks")
            result, generated = pc.ensure_reapeaks(
                compressed,
                peaks,
                decoder="ffmpeg",
                spectral=False,
                wave_encoding="auto",
            )
            self.assertTrue(generated)
            self.assertEqual(pc.read_reapeaks_header(result).magic, expected_magic)
            reapeaks.ReaPeaks.open(str(result))
            first = result.read_bytes()
            _result, rebuilt = pc.ensure_reapeaks(
                compressed,
                peaks,
                decoder="ffmpeg",
                spectral=False,
                wave_encoding="auto",
                rebuild=True,
            )
            self.assertTrue(rebuilt)
            self.assertEqual(first, result.read_bytes())
        self.assertGreaterEqual(made, 1, "no FFmpeg compressed encoder was available")

    def test_ffmpeg_playback_option_produces_valid_temporary_float_wav(self) -> None:
        compressed = self.root / "source.flac"
        self.assertTrue(run_ffmpeg(self.wav, compressed, ["-c:a", "flac"]))
        prepared = pc.prepare_playback_audio(compressed, decoder="ffmpeg")
        path = prepared.path
        try:
            self.assertTrue(path.is_file())
            decoded = pc.read_wav_cache_input(path)
            self.assertEqual(decoded.sample_type, "f32")
            self.assertGreater(decoded.frames, 0)
        finally:
            prepared.close()
        self.assertFalse(path.exists())

    def test_ffmpeg_output_limit_is_enforced(self) -> None:
        with self.assertRaises(pc.PlayerCacheError):
            pc.decode_with_ffmpeg(self.wav, max_decode_bytes=64)


class PlayerCommonPathHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.audio = self.root / (("界" * 70) + ".wav")
        make_pcm16_wav(self.audio, frames=32)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_long_utf8_central_name_stays_under_name_max(self) -> None:
        directory = self.root / "all-peaks"
        target = pc.central_peak_path(self.audio, directory)
        directory.mkdir()
        if hasattr(os, "pathconf"):
            limit = os.pathconf(directory, "PC_NAME_MAX")
            self.assertLessEqual(len(os.fsencode(target.name)), limit)
        self.assertEqual(target.parent, directory)
        result, generated = pc.ensure_reapeaks(
            self.audio,
            decoder="wav",
            spectral=False,
            cache_mode="central",
            cache_directory=directory,
        )
        self.assertTrue(generated)
        self.assertEqual(result, target)

    def test_cache_target_cannot_overwrite_source(self) -> None:
        with self.assertRaises(pc.PlayerCacheError):
            pc.ensure_reapeaks(
                self.audio,
                self.audio,
                decoder="wav",
                spectral=False,
            )

    def test_stale_escape_hatch_never_accepts_malformed_cache(self) -> None:
        target = pc.sidecar_peak_path(self.audio)
        target.write_bytes(b"RPKN")
        result, generated = pc.ensure_reapeaks(
            self.audio,
            decoder="wav",
            spectral=False,
            allow_stale_cache=True,
        )
        self.assertTrue(generated)
        self.assertEqual(result, target)
        reapeaks.ReaPeaks.open(str(target))


class WebRangeParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        web_player = REPO / "examples" / "web_player"
        sys.path.insert(0, str(web_player))
        import server  # noqa: PLC0415

        cls.handler = server.DemoHandler

    def test_closed_open_and_suffix_ranges(self) -> None:
        self.assertEqual(self.handler._parse_byte_range("bytes=2-4", 10), (2, 4))
        self.assertEqual(self.handler._parse_byte_range("bytes=7-", 10), (7, 9))
        self.assertEqual(self.handler._parse_byte_range("bytes=-3", 10), (7, 9))
        self.assertEqual(self.handler._parse_byte_range("bytes=-99", 10), (0, 9))

    def test_invalid_ranges_are_rejected(self) -> None:
        for value in (
            "items=0-1",
            "bytes=",
            "bytes=0-1,4-5",
            "bytes=10-",
            "bytes=5-4",
            "bytes=-0",
            "bytes=abc-def",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.handler._parse_byte_range(value, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
