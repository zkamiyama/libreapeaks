"""Bounded, random-access source PCM for sample-accurate waveform LOD.

The persistent ``.reapeaks`` pyramid remains the low/medium-resolution data
source.  This module is deliberately transient: it reads only a page around an
extreme-zoom viewport, keeps a byte-limited LRU of decoded float32 windows, and
can reduce a window to viewport-scale min/max records without creating another
persistent peak cache.

Supported random-access paths:

* PCM16 and IEEE-float32 RIFF/WAVE are read directly from their ``data`` chunks;
* other seekable media is decoded by an accurate-seeking FFmpeg subprocess for
  the requested time window only.

The service checks every requested window against its byte limit before file
I/O or subprocess creation.
"""
from __future__ import annotations

from array import array
from collections.abc import Callable
from collections import OrderedDict
from dataclasses import dataclass, replace
import math
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
from typing import Literal

from player_common import (
    DEFAULT_DECODE_TIMEOUT,
    PlayerCacheError,
    UnsupportedWavError,
    _probe_with_ffprobe,
    _resolve_tool,
    _resolved,
)


DEFAULT_PCM_CACHE_BYTES = 64 * 1024 * 1024
DEFAULT_PCM_MAX_WINDOW_BYTES = 16 * 1024 * 1024
DEFAULT_PCM_TARGET_PAGE_BYTES = 1 * 1024 * 1024
DEFAULT_PCM_MAX_TEXTURE_RECORDS = 4096
DEFAULT_PCM_MAX_CACHE_ITEMS = 1024
DEFAULT_PCM_MAX_PENDING_WINDOWS = 64
DEFAULT_PCM_MAX_CONCURRENT_LOADS = 2
DEFAULT_SOURCE_ENTER_PIXELS_PER_PEAK = 1.5
DEFAULT_SOURCE_EXIT_PIXELS_PER_PEAK = 1.1
DEFAULT_FFMPEG_SEEK_PREROLL_SECONDS = 2.0
MIN_SAMPLE_VIEW_FRAMES = 4

PcmDecoder = Literal["auto", "wav", "ffmpeg"]
PcmDisplayMode = Literal["envelope", "samples"]
PcmCacheDisposition = Literal["decoded", "cache-hit", "coalesced"]

_MAX_WAV_FMT_BYTES = 1024 * 1024
_MAX_WAV_CHUNKS = 65_536
_MAX_DIAGNOSTIC_LABEL_BYTES = 256


def _checked_int(value: object, what: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PlayerCacheError(f"PCM {what} must be an integer") from exc


def _checked_finite_float(value: object, what: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PlayerCacheError(f"PCM {what} must be numeric") from exc
    if not math.isfinite(result):
        raise PlayerCacheError(f"PCM {what} must be finite")
    return result


def _validated_diagnostic_label(value: str, what: str) -> str:
    label = str(value)
    if not label or len(label.encode("utf-8")) > _MAX_DIAGNOSTIC_LABEL_BYTES:
        raise PlayerCacheError(f"PCM {what} label is empty or too long")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in label):
        raise PlayerCacheError(f"PCM {what} label must be printable ASCII")
    return label


@dataclass(frozen=True)
class PcmSourceInfo:
    path: Path
    sample_rate: int
    channels: int
    total_frames: int | None
    backend: str
    codec: str


@dataclass(frozen=True)
class PcmWindow:
    """Interleaved little-endian float32 source frames."""

    first_frame: int
    frame_count: int
    sample_rate: int
    channels: int
    pcm_f32le: bytes
    backend: str

    @property
    def byte_count(self) -> int:
        return len(self.pcm_f32le)


@dataclass(frozen=True)
class PcmWindowReadResult:
    """A reader result that preserves a nested provider's cache outcome.

    Normal file readers can return :class:`PcmWindow` directly. A host cache
    adapter returns this wrapper when its callback itself hit/coalesced a block,
    preventing that inexpensive provider access from being reported as a new
    range decode by the GUI.
    """

    window: PcmWindow
    cache_disposition: PcmCacheDisposition = "decoded"
    reader_ran: bool = True


@dataclass(frozen=True)
class PcmRangeEvent:
    """Structured debug event for one source-window access.

    ``reader_ran`` is the unambiguous answer to “did a range read/decode run?”.
    ``cache_disposition`` distinguishes a retained/provider cache hit from a
    request that shared another thread's in-flight decode.
    """

    event_id: int
    occurred_unix_ms: int
    request_first_frame: int
    request_frame_count: int
    division: int
    raw_first_frame: int
    raw_frame_count: int
    display_first_frame: int
    display_frame_count: int
    display_record_count: int
    mode: PcmDisplayMode
    backend: str
    cache_disposition: PcmCacheDisposition
    reader_ran: bool
    reader_ms: float


@dataclass(frozen=True)
class PcmDisplayWindow:
    """GPU/QPainter-ready source records.

    ``components`` is one for exact samples and two for max/min envelopes.
    Records are time-major and channel-inner, matching a texture whose width is
    ``channels`` and whose height is ``record_count``.
    """

    first_frame: int
    frame_count: int
    division: int
    record_count: int
    sample_rate: int
    channels: int
    components: int
    data_f32le: bytes
    mode: PcmDisplayMode
    backend: str
    raw_cache_hit: bool
    range_event: PcmRangeEvent | None = None

    @property
    def byte_count(self) -> int:
        return len(self.data_f32le)


@dataclass(frozen=True)
class PcmLodPlan:
    active: bool
    mode: PcmDisplayMode | None
    division: int
    first_frame: int
    frame_count: int
    frames_per_pixel: float
    pixels_per_fine_peak: float
    reason: str

    @property
    def key(self) -> tuple[int, int, int] | None:
        if not self.active:
            return None
        return self.first_frame, self.frame_count, self.division


@dataclass(frozen=True)
class PcmDrawPlan:
    """Allocation-free geometry hints for a GUI's PCM draw pass.

    Texture renderers use ``record0`` and ``records_across`` directly. CPU
    renderers iterate ``first_visible_record``/``visible_record_count`` and
    compute x with ``x_origin_px + local_record * x_step_px``. Sample values
    remain interleaved in :class:`PcmDisplayWindow`; ``sample_offset`` maps a
    local visible record and channel to that flat float array.
    """

    mode: PcmDisplayMode
    record0: float
    records_across: float
    pixels_per_frame: float
    pixels_per_record: float
    first_visible_record: int
    visible_record_count: int
    x_origin_px: float
    x_step_px: float
    draw_lines: bool
    draw_points: bool
    point_radius_px: float
    line_width_px: float
    channels: int
    components: int

    def x_for_local_record(self, local_record: int) -> float:
        local = int(local_record)
        if not (0 <= local < self.visible_record_count):
            raise IndexError("PCM local record is outside the draw plan")
        return self.x_origin_px + local * self.x_step_px

    def value_offset(
        self, local_record: int, channel: int, component: int = 0
    ) -> int:
        record = self.first_visible_record + int(local_record)
        channel_index = int(channel)
        component_index = int(component)
        if not (0 <= int(local_record) < self.visible_record_count):
            raise IndexError("PCM local record is outside the draw plan")
        if not (0 <= channel_index < self.channels):
            raise IndexError("PCM channel is outside the draw plan")
        if not (0 <= component_index < self.components):
            raise IndexError("PCM component is outside the draw plan")
        return (
            (record * self.channels + channel_index) * self.components
            + component_index
        )

    def sample_offset(self, local_record: int, channel: int) -> int:
        if self.mode != "samples":
            raise ValueError("sample_offset is only valid for exact samples")
        return self.value_offset(local_record, channel)


@dataclass(frozen=True)
class _WavChunk:
    file_offset: int
    byte_count: int
    logical_offset: int


class PcmWindowReader:
    info: PcmSourceInfo

    def read_window(
        self, first_frame: int, frame_count: int
    ) -> PcmWindow | PcmWindowReadResult:
        raise NotImplementedError


PcmWindowCallback = Callable[
    [int, int],
    PcmWindow | PcmWindowReadResult | bytes | bytearray | memoryview,
]


class CallbackPcmWindowReader(PcmWindowReader):
    """Adapter for a DAW/player-owned decoded block cache.

    The callback is synchronous and runs on whichever thread calls
    :meth:`read_window`; the reference PySide loader calls it on a worker, not
    the Qt GUI thread. It should consult the host playback/source cache first
    and decode only a miss. Raw callback results must be interleaved
    little-endian f32; returning :class:`PcmWindow` lets the host identify its
    own backend, while :class:`PcmWindowReadResult` also preserves the host's
    internal hit/decode disposition for GUI diagnostics.
    """

    def __init__(
        self,
        callback: PcmWindowCallback,
        *,
        sample_rate: int,
        channels: int,
        total_frames: int | None,
        backend: str = "host-pcm-provider",
        codec: str = "host-decoded",
        source_name: str = "<host-pcm>",
    ):
        if not callable(callback):
            raise PlayerCacheError("PCM provider callback must be callable")
        if sample_rate <= 0 or not (1 <= channels <= 255):
            raise PlayerCacheError("PCM provider has invalid audio geometry")
        if total_frames is not None and total_frames < 0:
            raise PlayerCacheError("PCM provider total_frames must be non-negative")
        self.callback = callback
        self.info = PcmSourceInfo(
            path=Path(source_name),
            sample_rate=int(sample_rate),
            channels=int(channels),
            total_frames=(None if total_frames is None else int(total_frames)),
            backend=_validated_diagnostic_label(backend, "backend"),
            codec=_validated_diagnostic_label(codec, "codec"),
        )

    def read_window(
        self, first_frame: int, frame_count: int
    ) -> PcmWindow | PcmWindowReadResult:
        first = max(0, int(first_frame))
        count = max(0, int(frame_count))
        total = self.info.total_frames
        if total is not None:
            if first >= total:
                count = 0
            else:
                count = min(count, total - first)
        if count == 0:
            return PcmWindow(
                first,
                0,
                self.info.sample_rate,
                self.info.channels,
                b"",
                self.info.backend,
            )
        callback_result = self.callback(first, count)
        outcome: PcmWindowReadResult | None = None
        if isinstance(callback_result, PcmWindowReadResult):
            outcome = callback_result
            window = callback_result.window
        elif isinstance(callback_result, PcmWindow):
            window = callback_result
        elif isinstance(callback_result, (bytes, bytearray, memoryview)):
            payload = (
                callback_result
                if isinstance(callback_result, bytes)
                else bytes(callback_result)
            )
            frame_bytes = self.info.channels * 4
            if len(payload) % frame_bytes:
                raise PlayerCacheError(
                    "PCM provider returned a partial interleaved float32 frame"
                )
            window = PcmWindow(
                first,
                len(payload) // frame_bytes,
                self.info.sample_rate,
                self.info.channels,
                payload,
                self.info.backend,
            )
        else:
            raise PlayerCacheError("PCM provider returned an unsupported value")
        if window.first_frame != first:
            raise PlayerCacheError("PCM provider returned the wrong first frame")
        if window.frame_count < 0 or window.frame_count > count:
            raise PlayerCacheError("PCM provider exceeded its requested frame count")
        if (
            window.sample_rate != self.info.sample_rate
            or window.channels != self.info.channels
        ):
            raise PlayerCacheError("PCM provider returned mismatched audio geometry")
        try:
            payload = (
                window.pcm_f32le
                if isinstance(window.pcm_f32le, bytes)
                else bytes(window.pcm_f32le)
            )
        except (TypeError, ValueError) as exc:
            raise PlayerCacheError("PCM provider payload is not bytes-like") from exc
        expected = window.frame_count * window.channels * 4
        if len(payload) != expected:
            raise PlayerCacheError("PCM provider byte count does not match its geometry")
        backend = _validated_diagnostic_label(window.backend, "window backend")
        if payload is not window.pcm_f32le or backend != window.backend:
            window = replace(window, pcm_f32le=payload, backend=backend)
        if outcome is None:
            return window
        if outcome.cache_disposition not in ("decoded", "cache-hit", "coalesced"):
            raise PlayerCacheError("PCM provider returned an unknown cache disposition")
        if (outcome.cache_disposition == "decoded") != bool(outcome.reader_ran):
            raise PlayerCacheError(
                "PCM provider decode flag and cache disposition are inconsistent"
            )
        return replace(outcome, window=window)


def _read_exact(handle, count: int, what: str) -> bytes:
    data = handle.read(count)
    if len(data) != count:
        raise PlayerCacheError(f"truncated WAV {what}")
    return data


def _parse_wav_layout(path: Path) -> tuple[PcmSourceInfo, str, int, list[_WavChunk]]:
    try:
        file_size = path.stat().st_size
        handle = path.open("rb")
    except OSError as exc:
        raise PlayerCacheError(f"cannot open WAV {path}: {exc}") from exc

    with handle:
        header = _read_exact(handle, 12, "header")
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise UnsupportedWavError("not a RIFF/WAVE file")
        riff_end = struct.unpack_from("<I", header, 4)[0] + 8
        if riff_end > file_size or riff_end < 12:
            raise PlayerCacheError(
                f"invalid/truncated WAV RIFF payload: declared={riff_end}, actual={file_size}"
            )

        fmt: bytes | None = None
        raw_chunks: list[tuple[int, int]] = []
        pos = 12
        chunk_count = 0
        while pos < riff_end:
            chunk_count += 1
            if chunk_count > _MAX_WAV_CHUNKS:
                raise PlayerCacheError(
                    f"WAV exceeds the {_MAX_WAV_CHUNKS:,}-chunk safety limit"
                )
            if pos + 8 > riff_end:
                raise PlayerCacheError("truncated WAV chunk header")
            handle.seek(pos)
            chunk_header = _read_exact(handle, 8, "chunk header")
            chunk_id = chunk_header[:4]
            chunk_size = struct.unpack_from("<I", chunk_header, 4)[0]
            start = pos + 8
            end = start + chunk_size
            padded_end = end + (chunk_size & 1)
            if end > riff_end or padded_end > riff_end:
                raise PlayerCacheError(
                    f"truncated WAV chunk {chunk_id!r}: size={chunk_size}"
                )
            if chunk_id == b"fmt ":
                if chunk_size > _MAX_WAV_FMT_BYTES:
                    raise PlayerCacheError("unreasonably large WAV fmt chunk")
                handle.seek(start)
                candidate = _read_exact(handle, chunk_size, "fmt chunk")
                if fmt is None:
                    fmt = candidate
                elif fmt != candidate:
                    raise PlayerCacheError("conflicting WAV fmt chunks")
            elif chunk_id == b"data":
                raw_chunks.append((start, chunk_size))
            pos = padded_end

        if pos != riff_end:
            raise PlayerCacheError("WAV chunks do not exactly consume RIFF payload")
        if fmt is None or not raw_chunks or len(fmt) < 16:
            raise PlayerCacheError("WAV is missing a valid fmt/data chunk")

    format_tag, channels, sample_rate, byte_rate, block_align, bits = struct.unpack_from(
        "<HHIIHH", fmt, 0
    )
    if format_tag == 0xFFFE:
        if len(fmt) < 40:
            raise PlayerCacheError("truncated WAVE_FORMAT_EXTENSIBLE fmt chunk")
        cb_size = struct.unpack_from("<H", fmt, 16)[0]
        if cb_size < 22 or 18 + cb_size > len(fmt):
            raise PlayerCacheError("invalid WAVE_FORMAT_EXTENSIBLE cbSize")
        valid_bits = struct.unpack_from("<H", fmt, 18)[0]
        if valid_bits not in (0, bits) and not (0 < valid_bits <= bits):
            raise PlayerCacheError("invalid WAVE_FORMAT_EXTENSIBLE valid bits")
        if fmt[26:40] != bytes.fromhex("000000001000800000aa00389b71"):
            raise UnsupportedWavError(
                "unsupported WAVE_FORMAT_EXTENSIBLE subformat GUID"
            )
        format_tag = struct.unpack_from("<H", fmt, 24)[0]

    if not (1 <= channels <= 255) or sample_rate <= 0 or block_align <= 0:
        raise PlayerCacheError("invalid WAV audio geometry")
    if format_tag == 1 and bits == 16 and block_align == channels * 2:
        sample_type = "i16"
        codec = "pcm_s16le"
    elif format_tag == 3 and bits == 32 and block_align == channels * 4:
        sample_type = "f32"
        codec = "pcm_f32le"
    else:
        raise UnsupportedWavError(
            "source PCM's direct WAV path supports PCM16/float32 only; "
            f"got tag={format_tag}, bits={bits}, block_align={block_align}"
        )
    if byte_rate != sample_rate * block_align:
        raise PlayerCacheError("inconsistent WAV byte rate")

    total_bytes = sum(size for _offset, size in raw_chunks)
    if total_bytes % block_align:
        raise PlayerCacheError("WAV data does not contain whole interleaved frames")
    chunks: list[_WavChunk] = []
    logical = 0
    for file_offset, byte_count in raw_chunks:
        chunks.append(_WavChunk(file_offset, byte_count, logical))
        logical += byte_count
    info = PcmSourceInfo(
        path=path,
        sample_rate=sample_rate,
        channels=channels,
        total_frames=total_bytes // block_align,
        backend="wav-direct",
        codec=codec,
    )
    return info, sample_type, block_align, chunks


class WavPcmWindowReader(PcmWindowReader):
    """Cross-platform positional reads over logical WAV data chunks."""

    def __init__(self, path: str | Path):
        self.info, self.sample_type, self.block_align, self._chunks = _parse_wav_layout(
            _resolved(path)
        )

    def _read_logical(self, byte_offset: int, byte_count: int) -> bytes:
        if byte_count <= 0:
            return b""
        end = byte_offset + byte_count
        parts: list[bytes] = []
        try:
            with self.info.path.open("rb") as handle:
                for chunk in self._chunks:
                    chunk_end = chunk.logical_offset + chunk.byte_count
                    overlap_start = max(byte_offset, chunk.logical_offset)
                    overlap_end = min(end, chunk_end)
                    if overlap_end <= overlap_start:
                        continue
                    handle.seek(chunk.file_offset + overlap_start - chunk.logical_offset)
                    parts.append(
                        _read_exact(
                            handle,
                            overlap_end - overlap_start,
                            "data window",
                        )
                    )
        except OSError as exc:
            raise PlayerCacheError(
                f"cannot read WAV source window {self.info.path}: {exc}"
            ) from exc
        data = b"".join(parts)
        if len(data) != byte_count:
            raise PlayerCacheError("WAV data-window map is internally inconsistent")
        return data

    def read_window(self, first_frame: int, frame_count: int) -> PcmWindow:
        first = max(0, int(first_frame))
        count = max(0, int(frame_count))
        total = int(self.info.total_frames or 0)
        if first >= total:
            count = 0
        else:
            count = min(count, total - first)
        raw = self._read_logical(first * self.block_align, count * self.block_align)
        if self.sample_type == "f32":
            pcm = raw
        else:
            integers = array("h")
            integers.frombytes(raw)
            if sys.byteorder != "little":
                integers.byteswap()
            floats = array("f", (sample / 32768.0 for sample in integers))
            if sys.byteorder != "little":
                floats.byteswap()
            pcm = floats.tobytes()
        return PcmWindow(
            first_frame=first,
            frame_count=count,
            sample_rate=self.info.sample_rate,
            channels=self.info.channels,
            pcm_f32le=pcm,
            backend=self.info.backend,
        )


class FfmpegPcmWindowReader(PcmWindowReader):
    """Accurate-seeking, bounded FFmpeg decode of one requested window."""

    def __init__(
        self,
        path: str | Path,
        *,
        ffmpeg: str | Path = "ffmpeg",
        ffprobe: str | Path = "ffprobe",
        timeout: float = DEFAULT_DECODE_TIMEOUT,
        total_frames_hint: int | None = None,
    ):
        source = _resolved(path)
        if timeout <= 0:
            raise PlayerCacheError("PCM decode timeout must be positive")
        if not source.is_file():
            raise PlayerCacheError(f"audio file not found: {source}")
        probe = _probe_with_ffprobe(source, ffprobe=ffprobe, timeout=timeout)
        sample_rate = int(probe["sample_rate"])
        channels = int(probe["channels"])
        if sample_rate <= 0 or not (1 <= channels <= 255):
            raise PlayerCacheError("FFmpeg probe returned invalid audio geometry")
        self.info = PcmSourceInfo(
            path=source,
            sample_rate=sample_rate,
            channels=channels,
            total_frames=(
                max(0, int(total_frames_hint))
                if total_frames_hint is not None
                else None
            ),
            backend="ffmpeg-window",
            codec=_validated_diagnostic_label(str(probe["codec"]), "codec"),
        )
        self.ffmpeg = _resolve_tool(ffmpeg, "ffmpeg")
        self.timeout = float(timeout)
        # The web demo is threaded. Bound decoder subprocess concurrency per
        # source and let only the newest waiter proceed, so stale HTTP requests
        # cannot multiply PCM memory/CPU or create a decoder backlog. The
        # browser also debounces viewport requests.
        self._decode_condition = threading.Condition()
        self._decode_generation = 0
        self._decode_active = False

    def read_window(self, first_frame: int, frame_count: int) -> PcmWindow:
        first = max(0, int(first_frame))
        count = max(0, int(frame_count))
        if self.info.total_frames is not None:
            if first >= self.info.total_frames:
                count = 0
            else:
                count = min(count, self.info.total_frames - first)
        if count == 0:
            return PcmWindow(
                first,
                0,
                self.info.sample_rate,
                self.info.channels,
                b"",
                self.info.backend,
            )

        # Input-side -ss uses the demuxer's nearest seek point, then FFmpeg's
        # default accurate-seek path decodes and discards the codec preroll. We
        # intentionally seek two seconds before the requested frame and trim by
        # decoded sample count. Besides making the final boundary explicitly
        # sample-based, this avoids known short-FLAC demuxer failures when -ss
        # lands inside the file's first seek block. The extra decode is bounded
        # CPU work and never appears in stdout or the PCM cache.
        seek_preroll = min(
            first,
            int(round(self.info.sample_rate * DEFAULT_FFMPEG_SEEK_PREROLL_SECONDS)),
        )
        seek_first = first - seek_preroll
        start_seconds = seek_first / self.info.sample_rate
        trim_end = seek_preroll + count
        command = [
            self.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-accurate_seek",
            "-ss",
            f"{start_seconds:.17f}",
            "-i",
            str(self.info.path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            # -frames:a counts encoded audio frames/packets, not PCM sample
            # frames. atrim's end_sample is defined in decoded samples and is
            # therefore the correct hard bound for a waveform window.
            "-af",
            (
                f"atrim=start_sample={seek_preroll}:end_sample={trim_end},"
                "asetpts=PTS-STARTPTS"
            ),
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            # atrim is the semantic sample bound. -fs is a second, independent
            # process-output bound so a decoder/filter regression cannot make
            # subprocess.run accumulate unbounded stdout before validation.
            "-fs",
            str(count * self.info.channels * 4),
            "pipe:1",
        ]
        with self._decode_condition:
            self._decode_generation += 1
            generation = self._decode_generation
            while self._decode_active:
                if generation != self._decode_generation:
                    raise PlayerCacheError(
                        "FFmpeg PCM request superseded by a newer window"
                    )
                self._decode_condition.wait()
            if generation != self._decode_generation:
                raise PlayerCacheError(
                    "FFmpeg PCM request superseded by a newer window"
                )
            self._decode_active = True
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PlayerCacheError(
                f"FFmpeg PCM window timed out after {self.timeout:g}s"
            ) from exc
        finally:
            with self._decode_condition:
                self._decode_active = False
                self._decode_condition.notify_all()
        diagnostic = completed.stderr[-64 * 1024 :].decode("utf-8", "replace").strip()
        if completed.returncode != 0:
            raise PlayerCacheError(
                f"FFmpeg PCM window failed with exit {completed.returncode}: {diagnostic}"
            )
        frame_bytes = self.info.channels * 4
        if len(completed.stdout) % frame_bytes:
            raise PlayerCacheError("FFmpeg returned a partial interleaved PCM frame")
        actual = len(completed.stdout) // frame_bytes
        if actual > count:
            raise PlayerCacheError("FFmpeg exceeded the requested PCM frame count")
        return PcmWindow(
            first_frame=first,
            frame_count=actual,
            sample_rate=self.info.sample_rate,
            channels=self.info.channels,
            pcm_f32le=completed.stdout,
            backend=self.info.backend,
        )


def open_pcm_window_reader(
    path: str | Path,
    *,
    decoder: PcmDecoder = "auto",
    ffmpeg: str | Path = "ffmpeg",
    ffprobe: str | Path = "ffprobe",
    timeout: float = DEFAULT_DECODE_TIMEOUT,
    total_frames_hint: int | None = None,
) -> PcmWindowReader:
    if decoder == "wav":
        return WavPcmWindowReader(path)
    if decoder == "ffmpeg":
        return FfmpegPcmWindowReader(
            path,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            timeout=timeout,
            total_frames_hint=total_frames_hint,
        )
    if decoder != "auto":
        raise PlayerCacheError(f"unknown PCM decoder: {decoder}")
    try:
        return WavPcmWindowReader(path)
    except UnsupportedWavError:
        return FfmpegPcmWindowReader(
            path,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            timeout=timeout,
            total_frames_hint=total_frames_hint,
        )


@dataclass
class _PendingWindow:
    event: threading.Event
    owner_thread_id: int
    value: PcmWindow | None = None
    error: BaseException | None = None
    reader_ms: float = 0.0


@dataclass(frozen=True)
class PcmCacheAccess:
    disposition: PcmCacheDisposition
    reader_ran: bool
    reader_ms: float


class PcmWindowLru:
    """Thread-safe byte LRU with in-flight request coalescing."""

    def __init__(
        self,
        reader: PcmWindowReader,
        capacity_bytes: int,
        *,
        max_items: int = DEFAULT_PCM_MAX_CACHE_ITEMS,
        max_pending_windows: int = DEFAULT_PCM_MAX_PENDING_WINDOWS,
        max_concurrent_loads: int = DEFAULT_PCM_MAX_CONCURRENT_LOADS,
    ):
        if not hasattr(reader, "info"):
            raise PlayerCacheError("PCM reader has no source information")
        sample_rate = _checked_int(reader.info.sample_rate, "reader sample rate")
        channels = _checked_int(reader.info.channels, "reader channel count")
        if sample_rate <= 0 or not (1 <= channels <= 255):
            raise PlayerCacheError("PCM reader has invalid audio geometry")
        capacity_bytes = _checked_int(capacity_bytes, "cache capacity")
        max_items = _checked_int(max_items, "cache item limit")
        max_pending_windows = _checked_int(
            max_pending_windows, "pending-window limit"
        )
        max_concurrent_loads = _checked_int(
            max_concurrent_loads, "concurrent-load limit"
        )
        if capacity_bytes < 0:
            raise PlayerCacheError("PCM cache capacity must be non-negative")
        if max_items <= 0 or max_pending_windows <= 0 or max_concurrent_loads <= 0:
            raise PlayerCacheError("PCM cache concurrency/item limits must be positive")
        self.reader = reader
        self.capacity_bytes = int(capacity_bytes)
        self.max_items = int(max_items)
        self.max_pending_windows = int(max_pending_windows)
        self.max_concurrent_loads = int(max_concurrent_loads)
        self._items: OrderedDict[tuple[int, int], PcmWindow] = OrderedDict()
        self._resident_bytes = 0
        self._pending: dict[tuple[int, int], _PendingWindow] = {}
        self._lock = threading.Lock()
        self._load_slots = threading.BoundedSemaphore(self.max_concurrent_loads)
        self.hits = 0
        self.misses = 0
        self.loads = 0
        self.coalesced = 0
        self.rejected = 0

    def _validated_result(
        self,
        result: PcmWindow | PcmWindowReadResult,
        key: tuple[int, int],
    ) -> tuple[PcmWindow, PcmCacheDisposition, bool]:
        if isinstance(result, PcmWindowReadResult):
            loaded = result.window
            disposition = result.cache_disposition
            reader_ran = result.reader_ran
            if disposition not in ("decoded", "cache-hit", "coalesced"):
                raise PlayerCacheError("PCM reader returned an unknown cache disposition")
            if not isinstance(reader_ran, bool):
                raise PlayerCacheError("PCM reader result flag must be boolean")
            if (disposition == "decoded") != reader_ran:
                raise PlayerCacheError(
                    "PCM reader decode flag and cache disposition are inconsistent"
                )
        elif isinstance(result, PcmWindow):
            loaded = result
            disposition = "decoded"
            reader_ran = True
        else:
            raise PlayerCacheError("PCM reader returned an unsupported result")

        first, count = key
        loaded_first = _checked_int(loaded.first_frame, "reader first frame")
        loaded_count = _checked_int(loaded.frame_count, "reader frame count")
        loaded_rate = _checked_int(loaded.sample_rate, "reader sample rate")
        loaded_channels = _checked_int(loaded.channels, "reader channel count")
        if loaded_first != first:
            raise PlayerCacheError("PCM reader returned the wrong first frame")
        if loaded_count < 0 or loaded_count > count:
            raise PlayerCacheError("PCM reader exceeded its requested frame count")
        if (
            loaded_rate != int(self.reader.info.sample_rate)
            or loaded_channels != int(self.reader.info.channels)
        ):
            raise PlayerCacheError("PCM reader returned mismatched audio geometry")
        try:
            payload = (
                loaded.pcm_f32le
                if isinstance(loaded.pcm_f32le, bytes)
                else bytes(loaded.pcm_f32le)
            )
        except (TypeError, ValueError) as exc:
            raise PlayerCacheError("PCM reader payload is not bytes-like") from exc
        expected = loaded_count * loaded_channels * 4
        if len(payload) != expected:
            raise PlayerCacheError("PCM reader byte count does not match its geometry")
        backend = _validated_diagnostic_label(loaded.backend, "window backend")
        if (
            payload is not loaded.pcm_f32le
            or backend != loaded.backend
            or loaded_first is not loaded.first_frame
            or loaded_count is not loaded.frame_count
            or loaded_rate is not loaded.sample_rate
            or loaded_channels is not loaded.channels
        ):
            loaded = replace(
                loaded,
                first_frame=loaded_first,
                frame_count=loaded_count,
                sample_rate=loaded_rate,
                channels=loaded_channels,
                pcm_f32le=payload,
                backend=backend,
            )
        return loaded, disposition, reader_ran

    @property
    def resident_bytes(self) -> int:
        with self._lock:
            return self._resident_bytes

    @property
    def item_count(self) -> int:
        with self._lock:
            return len(self._items)

    def get(
        self, first_frame: int, frame_count: int
    ) -> tuple[PcmWindow, PcmCacheAccess]:
        key = (
            max(0, _checked_int(first_frame, "first frame")),
            max(0, _checked_int(frame_count, "frame count")),
        )
        with self._lock:
            cached = self._items.pop(key, None)
            if cached is not None:
                self._items[key] = cached
                self.hits += 1
                return cached, PcmCacheAccess("cache-hit", False, 0.0)
            pending = self._pending.get(key)
            if pending is None:
                if len(self._pending) >= self.max_pending_windows:
                    self.rejected += 1
                    raise PlayerCacheError(
                        "too many pending PCM windows; discard stale viewport requests"
                    )
                pending = _PendingWindow(
                    threading.Event(),
                    threading.get_ident(),
                )
                self._pending[key] = pending
                self.misses += 1
                owner = True
            else:
                if pending.owner_thread_id == threading.get_ident():
                    raise PlayerCacheError(
                        "reentrant PCM request for the same in-flight window"
                    )
                owner = False
        if not owner:
            pending.event.wait()
            if pending.error is not None:
                raise pending.error
            if pending.value is None:
                raise PlayerCacheError("coalesced PCM decode produced no value")
            with self._lock:
                self.coalesced += 1
            # Return the in-flight result even when cache capacity is zero or
            # the decoded window is larger than the LRU. Coalescing should not
            # depend on retaining the result after this group of waiters exits.
            return pending.value, PcmCacheAccess(
                "coalesced", False, pending.reader_ms
            )

        try:
            with self._load_slots:
                started = time.perf_counter_ns()
                read_result = self.reader.read_window(*key)
                loaded, load_disposition, load_reader_ran = self._validated_result(
                    read_result,
                    key,
                )
                reader_ms = (time.perf_counter_ns() - started) / 1e6
        except BaseException as exc:
            with self._lock:
                pending.error = exc
                self._pending.pop(key, None)
                pending.event.set()
            raise

        with self._lock:
            self.loads += 1
            if self.capacity_bytes > 0 and loaded.byte_count <= self.capacity_bytes:
                self._items[key] = loaded
                self._resident_bytes += loaded.byte_count
                while self._items and (
                    self._resident_bytes > self.capacity_bytes
                    or len(self._items) > self.max_items
                ):
                    _old_key, old = self._items.popitem(last=False)
                    self._resident_bytes -= old.byte_count
            pending.value = loaded
            pending.reader_ms = reader_ms
            self._pending.pop(key, None)
            pending.event.set()
        return loaded, PcmCacheAccess(
            load_disposition,
            load_reader_ran,
            reader_ms,
        )

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "capacity_bytes": self.capacity_bytes,
                "max_items": self.max_items,
                "max_pending_windows": self.max_pending_windows,
                "max_concurrent_loads": self.max_concurrent_loads,
                "resident_bytes": self._resident_bytes,
                "items": len(self._items),
                "pending": len(self._pending),
                "hits": self.hits,
                "misses": self.misses,
                "loads": self.loads,
                "coalesced": self.coalesced,
                "rejected": self.rejected,
            }


def _little_endian_f32_view(raw: bytes) -> tuple[object, array | None]:
    if len(raw) % 4:
        raise PlayerCacheError("PCM float payload is not 32-bit aligned")
    if sys.byteorder == "little":
        return memoryview(raw).cast("f"), None
    copied = array("f")
    copied.frombytes(raw)
    copied.byteswap()
    return copied, copied


def build_pcm_display_window(window: PcmWindow, division: int) -> PcmDisplayWindow:
    div = max(1, _checked_int(division, "display division"))
    first_frame = _checked_int(window.first_frame, "window first frame")
    frames = _checked_int(window.frame_count, "window frame count")
    sample_rate = _checked_int(window.sample_rate, "window sample rate")
    channels = _checked_int(window.channels, "window channel count")
    if first_frame < 0 or frames < 0 or sample_rate <= 0 or not (1 <= channels <= 255):
        raise PlayerCacheError("PCM display window has invalid audio geometry")
    try:
        payload = (
            window.pcm_f32le
            if isinstance(window.pcm_f32le, bytes)
            else bytes(window.pcm_f32le)
        )
    except (TypeError, ValueError) as exc:
        raise PlayerCacheError("PCM display payload is not bytes-like") from exc
    backend = _validated_diagnostic_label(window.backend, "window backend")
    expected = window.frame_count * channels * 4
    if len(payload) != expected:
        raise PlayerCacheError("PCM window byte count does not match its geometry")
    if div == 1:
        return PcmDisplayWindow(
            first_frame=first_frame,
            frame_count=frames,
            division=1,
            record_count=frames,
            sample_rate=sample_rate,
            channels=channels,
            components=1,
            data_f32le=payload,
            mode="samples",
            backend=backend,
            raw_cache_hit=False,
        )
    if first_frame % div:
        raise PlayerCacheError("PCM envelope window must start on a division boundary")

    values, _owned = _little_endian_f32_view(payload)
    output = array("f")
    for bucket_start in range(0, frames, div):
        bucket_end = min(frames, bucket_start + div)
        for channel in range(channels):
            first = float(values[bucket_start * channels + channel])  # type: ignore[index]
            if not math.isfinite(first):
                first = 0.0
            maximum = first
            minimum = first
            for frame in range(bucket_start + 1, bucket_end):
                sample = float(values[frame * channels + channel])  # type: ignore[index]
                if not math.isfinite(sample):
                    sample = 0.0
                if sample > maximum:
                    maximum = sample
                if sample < minimum:
                    minimum = sample
            output.append(maximum)
            output.append(minimum)
    if sys.byteorder != "little":
        output.byteswap()
    return PcmDisplayWindow(
        first_frame=first_frame,
        frame_count=frames,
        division=div,
        record_count=(frames + div - 1) // div,
        sample_rate=sample_rate,
        channels=channels,
        components=2,
        data_f32le=output.tobytes(),
        mode="envelope",
        backend=backend,
        raw_cache_hit=False,
    )


def pcm_display_values(
    window: PcmDisplayWindow,
    *,
    sanitize_nonfinite: bool = True,
) -> array:
    """Return native-endian float values after validating display geometry.

    This convenience API is intended for CPU GUI toolkits. By default invalid
    NaN/infinite float samples become silence so coordinate conversion cannot
    fail. GPU clients should upload ``data_f32le`` directly (or endian-convert
    once on a big-endian host) to avoid the additional array allocation.
    """

    expected = window.record_count * window.channels * window.components * 4
    if len(window.data_f32le) != expected:
        raise PlayerCacheError(
            f"PCM display payload {len(window.data_f32le)} bytes != expected {expected}"
        )
    values = array("f")
    values.frombytes(window.data_f32le)
    if sys.byteorder != "little":
        values.byteswap()
    if sanitize_nonfinite:
        for index, value in enumerate(values):
            if not math.isfinite(value):
                values[index] = 0.0
    return values


def plan_pcm_draw(
    window: PcmDisplayWindow,
    view_start: float,
    view_end: float,
    width_px: int,
    *,
    point_min_pixels_per_frame: float = 3.0,
    point_radius_px: float = 2.7,
    line_width_px: float = 1.35,
) -> PcmDrawPlan:
    """Map a PCM display window to reusable CPU/GPU drawing geometry."""

    start = float(view_start)
    end = float(view_end)
    try:
        width = max(1, int(width_px))
        point_threshold = float(point_min_pixels_per_frame)
        point_radius = float(point_radius_px)
        line_width = float(line_width_px)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PlayerCacheError("PCM draw parameters must be numeric") from exc
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise PlayerCacheError("PCM draw view must be finite and non-empty")
    if window.division <= 0 or window.record_count <= 0:
        raise PlayerCacheError("PCM draw window has no records")
    if (
        window.first_frame < 0
        or window.frame_count <= 0
        or window.sample_rate <= 0
        or not (1 <= window.channels <= 255)
        or window.record_count
        != (window.frame_count + window.division - 1) // window.division
    ):
        raise PlayerCacheError("PCM draw window has invalid audio geometry")
    if window.mode not in ("samples", "envelope"):
        raise PlayerCacheError("PCM draw window has an unknown mode")
    expected_components = 1 if window.mode == "samples" else 2
    if window.components != expected_components:
        raise PlayerCacheError("PCM draw mode/component geometry is inconsistent")
    if window.mode == "samples" and window.division != 1:
        raise PlayerCacheError("exact sample draw mode requires division=1")
    if not math.isfinite(point_threshold) or point_threshold < 0:
        raise PlayerCacheError("sample-point threshold must be non-negative")
    if (
        not math.isfinite(point_radius)
        or not math.isfinite(line_width)
        or point_radius < 0
        or line_width <= 0
    ):
        raise PlayerCacheError("PCM point/line sizes are invalid")

    span = end - start
    record0 = (start - window.first_frame) / window.division
    records_across = span / window.division
    end_record = record0 + records_across
    if window.mode == "samples":
        # Keep one neighbor outside either view edge so a line strip reaches
        # the boundary without allocating a separate segment list.
        first_visible = math.ceil(record0) - 1
        last_visible = math.floor(end_record) + 2
    else:
        first_visible = math.floor(record0)
        last_visible = math.floor(end_record) + 1
    first_visible = max(0, min(window.record_count, first_visible))
    last_visible = max(first_visible, min(window.record_count, last_visible))
    pixels_per_frame = width / span
    x_step = pixels_per_frame * window.division
    first_frame = window.first_frame + first_visible * window.division
    return PcmDrawPlan(
        mode=window.mode,
        record0=record0,
        records_across=records_across,
        pixels_per_frame=pixels_per_frame,
        pixels_per_record=x_step,
        first_visible_record=first_visible,
        visible_record_count=last_visible - first_visible,
        x_origin_px=(first_frame - start) * pixels_per_frame,
        x_step_px=x_step,
        draw_lines=window.mode == "samples" and last_visible - first_visible >= 2,
        draw_points=(
            window.mode == "samples"
            and last_visible - first_visible > 0
            and pixels_per_frame >= point_threshold
        ),
        point_radius_px=point_radius,
        line_width_px=line_width,
        channels=window.channels,
        components=window.components,
    )


class SourcePcmService:
    """Validated source reader plus byte-bounded decoded-window cache."""

    def __init__(
        self,
        reader: PcmWindowReader,
        *,
        cache_bytes: int = DEFAULT_PCM_CACHE_BYTES,
        max_window_bytes: int = DEFAULT_PCM_MAX_WINDOW_BYTES,
        target_page_bytes: int = DEFAULT_PCM_TARGET_PAGE_BYTES,
        cache_max_items: int = DEFAULT_PCM_MAX_CACHE_ITEMS,
        max_pending_windows: int = DEFAULT_PCM_MAX_PENDING_WINDOWS,
        max_concurrent_loads: int = DEFAULT_PCM_MAX_CONCURRENT_LOADS,
        expected_sample_rate: int | None = None,
        expected_channels: int | None = None,
    ):
        sample_rate = _checked_int(reader.info.sample_rate, "source sample rate")
        channels = _checked_int(reader.info.channels, "source channel count")
        total_frames = (
            None
            if reader.info.total_frames is None
            else _checked_int(reader.info.total_frames, "source total frame count")
        )
        backend = _validated_diagnostic_label(reader.info.backend, "backend")
        codec = _validated_diagnostic_label(reader.info.codec, "codec")
        max_window_bytes = _checked_int(
            max_window_bytes, "maximum window byte limit"
        )
        target_page_bytes = _checked_int(target_page_bytes, "target page byte limit")
        if sample_rate <= 0 or not (1 <= channels <= 255):
            raise PlayerCacheError("source PCM reader has invalid audio geometry")
        if total_frames is not None and total_frames < 0:
            raise PlayerCacheError("source PCM reader has a negative frame count")
        if max_window_bytes <= 0 or target_page_bytes <= 0:
            raise PlayerCacheError("PCM window/page byte limits must be positive")
        if target_page_bytes > max_window_bytes:
            raise PlayerCacheError("PCM target page cannot exceed maximum window bytes")
        if max_window_bytes < channels * 4:
            raise PlayerCacheError("PCM maximum window cannot hold one source frame")
        if expected_sample_rate is not None and sample_rate != _checked_int(
            expected_sample_rate, "expected sample rate"
        ):
            raise PlayerCacheError(
                "source PCM/cache sample-rate mismatch: "
                f"source={sample_rate}, cache={expected_sample_rate}"
            )
        if expected_channels is not None and channels != _checked_int(
            expected_channels, "expected channel count"
        ):
            raise PlayerCacheError(
                "source PCM/cache channel mismatch: "
                f"source={channels}, cache={expected_channels}"
            )
        self.reader = reader
        self.info = PcmSourceInfo(
            path=Path(reader.info.path),
            sample_rate=sample_rate,
            channels=channels,
            total_frames=total_frames,
            backend=backend,
            codec=codec,
        )
        self.cache = PcmWindowLru(
            reader,
            cache_bytes,
            max_items=cache_max_items,
            max_pending_windows=max_pending_windows,
            max_concurrent_loads=max_concurrent_loads,
        )
        self.max_window_bytes = max_window_bytes
        self.target_page_bytes = target_page_bytes
        self._event_lock = threading.Lock()
        self._event_id = 0
        self._last_range_event: PcmRangeEvent | None = None

    @property
    def last_range_event(self) -> PcmRangeEvent | None:
        with self._event_lock:
            return self._last_range_event

    def _raw_page(self, first: int, count: int) -> tuple[int, int]:
        """Choose a half-overlapping decoded page that contains the request."""

        frame_bytes = self.info.channels * 4
        max_frames = self.max_window_bytes // frame_bytes
        target_frames = max(
            count,
            max(1, self.target_page_bytes // frame_bytes),
        )
        page_count = min(max_frames, target_frames)
        if page_count < count:
            raise PlayerCacheError(
                f"PCM request exceeds max_window_bytes={self.max_window_bytes}"
            )
        stride = max(1, page_count // 2)
        page_first = (first // stride) * stride
        requested_end = first + count
        if page_first + page_count < requested_end:
            page_first = requested_end - page_count
        total = self.info.total_frames
        if total is not None:
            total = max(0, int(total))
            if total >= page_count and page_first + page_count > total:
                page_first = total - page_count
            page_first = min(page_first, total)
            page_count = min(page_count, max(0, total - page_first))
        return max(0, page_first), max(0, page_count)

    def display_window(
        self,
        first_frame: int,
        frame_count: int,
        division: int,
    ) -> PcmDisplayWindow:
        first = max(0, int(first_frame))
        count = max(0, int(frame_count))
        div = max(1, int(division))
        if count <= 0:
            raise PlayerCacheError("PCM request must contain at least one frame")
        if self.info.total_frames is not None and first >= self.info.total_frames:
            raise PlayerCacheError("PCM request starts at or beyond source EOF")
        if first % div:
            raise PlayerCacheError("PCM request start must align to division")
        requested_bytes = count * self.info.channels * 4
        if requested_bytes > self.max_window_bytes:
            raise PlayerCacheError(
                f"PCM request exceeds max_window_bytes={self.max_window_bytes}"
            )
        page_first, page_count = self._raw_page(first, count)
        raw, access = self.cache.get(page_first, page_count)
        offset_frames = first - raw.first_frame
        if offset_frames < 0:
            raise PlayerCacheError("PCM cache page does not cover request start")
        actual_count = min(count, max(0, raw.frame_count - offset_frames))
        if actual_count <= 0:
            raise PlayerCacheError("source PCM returned no frames for the requested window")
        frame_bytes = self.info.channels * 4
        byte_start = offset_frames * frame_bytes
        byte_end = byte_start + actual_count * frame_bytes
        requested = PcmWindow(
            first_frame=first,
            frame_count=actual_count,
            sample_rate=raw.sample_rate,
            channels=raw.channels,
            pcm_f32le=raw.pcm_f32le[byte_start:byte_end],
            backend=raw.backend,
        )
        display = build_pcm_display_window(requested, div)
        with self._event_lock:
            self._event_id += 1
            event = PcmRangeEvent(
                event_id=self._event_id,
                occurred_unix_ms=time.time_ns() // 1_000_000,
                request_first_frame=first,
                request_frame_count=count,
                division=div,
                raw_first_frame=raw.first_frame,
                raw_frame_count=raw.frame_count,
                display_first_frame=display.first_frame,
                display_frame_count=display.frame_count,
                display_record_count=display.record_count,
                mode=display.mode,
                backend=display.backend,
                cache_disposition=access.disposition,
                reader_ran=access.reader_ran,
                reader_ms=access.reader_ms,
            )
            self._last_range_event = event
        return replace(
            display,
            raw_cache_hit=access.disposition == "cache-hit",
            range_event=event,
        )

    def meta(self) -> dict[str, object]:
        return {
            "available": True,
            "backend": self.info.backend,
            "codec": self.info.codec,
            "sample_rate": self.info.sample_rate,
            "channels": self.info.channels,
            "total_frames": self.info.total_frames,
            "cache_bytes": self.cache.capacity_bytes,
            "cache_max_items": self.cache.max_items,
            "max_pending_windows": self.cache.max_pending_windows,
            "max_concurrent_loads": self.cache.max_concurrent_loads,
            "max_window_bytes": self.max_window_bytes,
            "target_page_bytes": self.target_page_bytes,
            "range_event_version": 1,
        }


def _next_power_of_two(value: int) -> int:
    value = max(1, int(value))
    return 1 << (value - 1).bit_length()


def _inactive_plan(
    frames_per_pixel: float,
    pixels_per_peak: float,
    reason: str,
) -> PcmLodPlan:
    return PcmLodPlan(
        False,
        None,
        0,
        0,
        0,
        frames_per_pixel,
        pixels_per_peak,
        reason,
    )


def plan_pcm_lod(
    view_start: float,
    view_end: float,
    width: int,
    total_frames: int,
    channels: int,
    fine_division: int,
    *,
    source_active: bool = False,
    enter_pixels_per_peak: float = DEFAULT_SOURCE_ENTER_PIXELS_PER_PEAK,
    exit_pixels_per_peak: float = DEFAULT_SOURCE_EXIT_PIXELS_PER_PEAK,
    max_window_bytes: int = DEFAULT_PCM_MAX_WINDOW_BYTES,
    target_page_bytes: int = DEFAULT_PCM_TARGET_PAGE_BYTES,
    max_texture_records: int = DEFAULT_PCM_MAX_TEXTURE_RECORDS,
) -> PcmLodPlan:
    """Choose cache/source/sample LOD and a reusable aligned source page."""

    total = _checked_int(total_frames, "LOD total frame count")
    # UI zoom/pan coordinates may be fractional.  Source PCM reads remain
    # integer-indexed, so cover the continuous viewport by rounding its start
    # down and end up after clamping to the source timeline.
    start_input = _checked_finite_float(view_start, "LOD view start")
    end_input = _checked_finite_float(view_end, "LOD view end")
    width_px = _checked_int(width, "LOD viewport width")
    channel_count = _checked_int(channels, "LOD channel count")
    fine = _checked_int(fine_division, "LOD fine division")
    max_window = _checked_int(max_window_bytes, "LOD maximum window bytes")
    target_page = _checked_int(target_page_bytes, "LOD target page bytes")
    max_records = _checked_int(max_texture_records, "LOD texture record limit")
    try:
        enter_threshold = float(enter_pixels_per_peak)
        exit_threshold = float(exit_pixels_per_peak)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PlayerCacheError("PCM LOD thresholds must be numeric") from exc
    if total <= 0 or width_px <= 0 or channel_count <= 0 or fine <= 0:
        raise PlayerCacheError("PCM LOD audio/view geometry must be positive")
    if channel_count > 255:
        raise PlayerCacheError("PCM LOD channel count exceeds 255")
    if max_window <= 0 or target_page <= 0 or max_records <= 0:
        raise PlayerCacheError("PCM LOD byte/texture limits must be positive")
    target_page = min(target_page, max_window)
    if (
        not math.isfinite(enter_threshold)
        or not math.isfinite(exit_threshold)
        or exit_threshold <= 0
        or enter_threshold <= 0
        or exit_threshold > enter_threshold
    ):
        raise PlayerCacheError("PCM LOD thresholds must satisfy 0 < exit <= enter")
    start = math.floor(min(max(0.0, start_input), total - 1))
    end = math.ceil(min(max(start + 1, end_input), total))
    span = max(1, end - start)
    frames_per_pixel = span / width_px
    pixels_per_peak = fine / frames_per_pixel
    threshold = exit_threshold if source_active else enter_threshold
    if pixels_per_peak < threshold:
        return _inactive_plan(frames_per_pixel, pixels_per_peak, "peak-cache density")

    division = 1 if frames_per_pixel <= 1.0 else min(
        fine,
        _next_power_of_two(math.ceil(frames_per_pixel)),
    )
    mode: PcmDisplayMode = "samples" if division == 1 else "envelope"
    frame_bytes = channel_count * 4
    guard_frames = max(2, division * 2)
    needed_first = max(0, start - guard_frames)
    needed_first = (needed_first // division) * division
    needed_last = min(total, end + guard_frames)
    needed_last = min(
        total,
        ((needed_last + division - 1) // division) * division,
    )
    needed_records = (needed_last - needed_first + division - 1) // division
    byte_limited_records = max_window // max(
        1, frame_bytes * division
    )
    capacity_records = min(max_records, byte_limited_records)
    if needed_records > capacity_records:
        reason = (
            "source byte budget"
            if byte_limited_records < needed_records
            else "texture record limit"
        )
        return _inactive_plan(frames_per_pixel, pixels_per_peak, reason)

    target_records = max(
        needed_records,
        min(
            capacity_records,
            max(
                1,
                target_page // max(1, frame_bytes * division),
            ),
        ),
    )
    page_frames = target_records * division
    # A half-page grid reuses a decoded window through ordinary panning while
    # allowing one bounded page to slide at an adversarial alignment instead
    # of spuriously requiring two or three full pages.
    stride_frames = max(division, (target_records // 2) * division)
    grid_first = (needed_first // stride_frames) * stride_frames
    # An aligned start must be no later than needed_first and no earlier than
    # needed_last-page_frames.  Ceil the lower bound: flooring it is subtly
    # wrong when EOF is not a division multiple and can leave the last partial
    # bucket uncovered.
    minimum_first = max(
        0,
        ((needed_last - page_frames + division - 1) // division) * division,
    )
    maximum_first = needed_first
    first = min(max(grid_first, minimum_first), maximum_first)
    last = min(total, first + page_frames)
    count = max(0, last - first)
    records = (count + division - 1) // division
    byte_count = count * frame_bytes
    if count <= 0:
        return _inactive_plan(frames_per_pixel, pixels_per_peak, "empty source window")
    if byte_count > max_window:
        return _inactive_plan(frames_per_pixel, pixels_per_peak, "source byte budget")
    if records > max_records:
        return _inactive_plan(frames_per_pixel, pixels_per_peak, "texture record limit")
    return PcmLodPlan(
        True,
        mode,
        division,
        first,
        count,
        frames_per_pixel,
        pixels_per_peak,
        "source PCM",
    )
