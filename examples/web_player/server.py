"""Local HTTP tile server for the libreapeaks JavaScript player demo.

The Canvas2D fallback uses decoded waveform/spectral display textures. The
WebGL2 path instead serves exact on-disk `.reapeaks` record windows so the
browser can upload waveform, `-'s'`, packed `-'g'`, and `-'r'` directly to GPU
textures and perform display-domain decoding in shaders.
"""
from __future__ import annotations

import argparse
import json
import math
import mimetypes
from pathlib import Path
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent
if str(EXAMPLES) not in sys.path:
    sys.path.insert(0, str(EXAMPLES))

import reapeaks  # noqa: E402
from player_common import (  # noqa: E402
    DEFAULT_DECODE_TIMEOUT,
    DEFAULT_MAX_DECODE_BYTES,
    PlayerCacheError,
    available_spectral_levels,
    exact_audio_frames,
    prepare_playback_audio,
)
from player_native_cache import ensure_reapeaks_native  # noqa: E402
from source_pcm import (  # noqa: E402
    DEFAULT_PCM_CACHE_BYTES,
    DEFAULT_PCM_MAX_WINDOW_BYTES,
    DEFAULT_PCM_TARGET_PAGE_BYTES,
    DEFAULT_SOURCE_ENTER_PIXELS_PER_PEAK,
    DEFAULT_SOURCE_EXIT_PIXELS_PER_PEAK,
    MIN_SAMPLE_VIEW_FRAMES,
    SourcePcmService,
    open_pcm_window_reader,
)
from webgl2_api import RawGpuService  # noqa: E402


class TileService:
    def __init__(
        self,
        audio_path: Path,
        playback_path: Path,
        peaks_path: Path,
        generated: bool,
        *,
        cache_decoder: str,
        playback_decoder: str,
        source_pcm_enabled: bool,
        pcm_decoder: str,
        ffmpeg: str,
        ffprobe: str,
        decode_timeout: float,
        pcm_cache_bytes: int,
        pcm_max_window_bytes: int,
        pcm_target_page_bytes: int,
    ):
        self.audio_path = audio_path
        self.playback_path = playback_path
        self.peaks_path = peaks_path
        self.generated = generated
        self.cache_decoder = cache_decoder
        self.playback_decoder = playback_decoder
        self.rp = reapeaks.ReaPeaks.open(str(peaks_path))
        self.gpu_api = RawGpuService(str(peaks_path))
        self.levels = self.rp.levels()
        if not self.levels:
            raise PlayerCacheError(
                f"cache has no decodable RPKN/RPKL waveform layers: {peaks_path}"
            )
        estimate = max(1, self.levels[0][0] * self.levels[0][1])
        self.total_frames = (
            exact_audio_frames(playback_path, self.rp.sample_rate)
            or exact_audio_frames(audio_path, self.rp.sample_rate)
            or estimate
        )
        self.native_levels = [
            {
                "layer_index": layer_index,
                "level_index": level_index,
                "division": division,
                "wave_peak_count": peak_count,
            }
            for layer_index, level_index, division, peak_count in available_spectral_levels(
                self.rp, self.levels
            )
        ]
        coarsest = len(self.levels) - 1
        env_w, env_h, env_raw = self.rp.envelope_texture(coarsest)
        self.coarsest_texture = {
            "level_index": coarsest,
            "width": env_w,
            "height": env_h,
            "bytes": len(bytes(env_raw)),
        }
        self.source_pcm: SourcePcmService | None = None
        self.source_pcm_error = "disabled by --no-source-pcm"
        if source_pcm_enabled:
            # If playback was explicitly decoded to a temporary float WAV,
            # reuse that seekable disk-backed file. Otherwise read/decode the
            # original source in bounded windows.
            pcm_path = playback_path if playback_decoder == "ffmpeg" else audio_path
            try:
                reader = open_pcm_window_reader(
                    pcm_path,
                    decoder=pcm_decoder,  # type: ignore[arg-type]
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    timeout=decode_timeout,
                    total_frames_hint=self.total_frames,
                )
                self.source_pcm = SourcePcmService(
                    reader,
                    cache_bytes=pcm_cache_bytes,
                    max_window_bytes=pcm_max_window_bytes,
                    target_page_bytes=pcm_target_page_bytes,
                    expected_sample_rate=int(self.rp.sample_rate),
                    expected_channels=int(self.rp.channels),
                )
                self.source_pcm_error = ""
            except Exception as exc:
                # Source PCM is an extreme-zoom enhancement. A decoder that is
                # unavailable for this media must not take down the peak-cache
                # player; the client receives the reason and stays on reapeaks.
                self.source_pcm_error = f"{type(exc).__name__}: {exc}"

    def meta(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "audio_name": self.audio_path.name,
            "playback_name": self.playback_path.name,
            "peaks_name": self.peaks_path.name,
            "generated_cache": self.generated,
            "cache_decoder": self.cache_decoder,
            "playback_decoder": self.playback_decoder,
            "sample_rate": self.rp.sample_rate,
            "channels": self.rp.channels,
            "wave_encoding": self.rp.wave_encoding,
            "tile_peaks": self.rp.tile_peaks,
            "total_frames": self.total_frames,
            "duration_seconds": self.total_frames / self.rp.sample_rate,
            "default_divisions": reapeaks.default_divisions(self.rp.sample_rate),
            "levels": [
                {
                    "level_index": index,
                    "division": division,
                    "peak_count": peak_count,
                    "native": native,
                }
                for index, (division, peak_count, native) in enumerate(self.levels)
            ],
            "native_spectral_levels": self.native_levels,
            "coarsest_envelope_texture": self.coarsest_texture,
            "source_pcm": (
                self.source_pcm.meta()
                if self.source_pcm is not None
                else {"available": False, "error": self.source_pcm_error}
            ),
            "source_lod": {
                "enter_pixels_per_fine_peak": DEFAULT_SOURCE_ENTER_PIXELS_PER_PEAK,
                "exit_pixels_per_fine_peak": DEFAULT_SOURCE_EXIT_PIXELS_PER_PEAK,
                "min_view_frames": MIN_SAMPLE_VIEW_FRAMES,
            },
        }
        payload.update(self.gpu_api.meta())
        return payload

    def choose_spectral(self, desired_division: int):
        if not self.native_levels:
            return None
        return min(
            self.native_levels,
            key=lambda item: abs(
                math.log(
                    max(1, int(item["division"])) / max(1, desired_division)
                )
            ),
        )

    def plan(self, start: int, end: int, width: int) -> dict[str, object]:
        level_index, division, first_peak, peak_count, peaks_per_pixel = (
            self.rp.plan_view(start, end, width)
        )
        tiles = self.rp.tiles_for_view(start, end, width)
        return {
            "level_index": level_index,
            "division": division,
            "first_peak": first_peak,
            "peak_count": peak_count,
            "peaks_per_pixel": peaks_per_pixel,
            "tiles": [
                {"level_index": int(level), "tile_index": int(tile)}
                for level, tile in tiles
            ],
            "spectral": self.choose_spectral(division),
        }


class DemoHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "libreapeaks-demo/4"

    @property
    def service(self) -> TileService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write("[web-player] " + (fmt % args) + "\n")

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, payload, status=HTTPStatus.OK) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(data)

    def send_bytes(
        self, data: bytes, *, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self._write(data)

    def send_static(self, path: Path) -> None:
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if resolved.parent != HERE.resolve() or not resolved.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = resolved.read_bytes()
        content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self._write(data)

    @staticmethod
    def _parse_byte_range(value: str, size: int) -> tuple[int, int]:
        if size <= 0 or not value.startswith("bytes="):
            raise ValueError("unsatisfiable byte range")
        spec = value[6:].strip()
        if not spec or "," in spec or "-" not in spec:
            raise ValueError("only one bytes range is supported")
        left, right = spec.split("-", 1)
        try:
            if left:
                start = int(left)
                if start < 0 or start >= size:
                    raise ValueError
                if right:
                    end = int(right)
                    if end < start:
                        raise ValueError
                    end = min(end, size - 1)
                else:
                    end = size - 1
            else:
                suffix = int(right)
                if suffix <= 0:
                    raise ValueError
                suffix = min(suffix, size)
                start = size - suffix
                end = size - 1
        except ValueError as exc:
            raise ValueError("unsatisfiable byte range") from exc
        return start, end

    def _range_not_satisfiable(self, size: int) -> None:
        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_audio(self) -> None:
        path = self.service.playback_path
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        range_header = self.headers.get("Range")
        if range_header:
            try:
                start, end = self._parse_byte_range(range_header, size)
            except ValueError:
                self._range_not_satisfiable(size)
                return
            status = HTTPStatus.PARTIAL_CONTENT
        else:
            start, end = 0, size - 1
            status = HTTPStatus.OK
        length = max(0, end - start + 1)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if length == 0:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self._write(chunk)
                    remaining -= len(chunk)
        except OSError:
            return

    @staticmethod
    def _int_arg(
        query: dict[str, list[str]],
        name: str,
        default: int | None = None,
        *,
        minimum: int | None = None,
    ) -> int:
        values = query.get(name)
        if not values:
            if default is None:
                raise ValueError(f"missing query parameter {name}")
            value = default
        else:
            if len(values) != 1:
                raise ValueError(f"query parameter {name} must appear exactly once")
            value = int(values[0])
        if minimum is not None and value < minimum:
            raise ValueError(f"query parameter {name} must be >= {minimum}")
        return value

    @staticmethod
    def _str_arg(query: dict[str, list[str]], name: str) -> str:
        values = query.get(name)
        if not values or not values[0]:
            raise ValueError(f"missing query parameter {name}")
        if len(values) != 1:
            raise ValueError(f"query parameter {name} must appear exactly once")
        return values[0]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urlparse(self.path)
            # Keep blank values so ``count=&count=1`` remains a duplicate and
            # cannot evade the exactly-once checks.  Parsing itself belongs in
            # this error boundary because max_num_fields intentionally raises.
            query = parse_qs(
                parsed.query,
                keep_blank_values=True,
                max_num_fields=32,
            )
            if parsed.path in ("/", "/index.html"):
                return self.send_static(HERE / "index.html")
            if parsed.path == "/app.js":
                return self.send_static(HERE / "app.js")
            if parsed.path == "/webgl2_renderer.mjs":
                return self.send_static(HERE / "webgl2_renderer.mjs")
            if parsed.path == "/style.css":
                return self.send_static(HERE / "style.css")
            if parsed.path == "/audio":
                return self.send_audio()
            if parsed.path == "/api/meta":
                return self.send_json(self.service.meta())
            if parsed.path == "/api/plan":
                start = self._int_arg(query, "start", minimum=0)
                end = self._int_arg(query, "end", minimum=0)
                width = self._int_arg(query, "width", minimum=1)
                if end <= start:
                    raise ValueError("end must be greater than start")
                return self.send_json(self.service.plan(start, end, width))
            if parsed.path == "/api/gpu-records":
                kind = self._str_arg(query, "kind")
                layer = self._int_arg(query, "layer", minimum=0)
                first = self._int_arg(query, "first", minimum=0)
                count = self._int_arg(query, "count", minimum=1)
                response = self.service.gpu_api.records(kind, layer, first, count)
                return self.send_bytes(response.data, headers=response.headers)
            if parsed.path == "/api/pcm-window":
                if self.service.source_pcm is None:
                    return self.send_json(
                        {
                            "error": self.service.source_pcm_error
                            or "source PCM is unavailable"
                        },
                        HTTPStatus.NOT_FOUND,
                    )
                first = self._int_arg(query, "first", minimum=0)
                count = self._int_arg(query, "count", minimum=1)
                division = self._int_arg(query, "division", minimum=1)
                if first >= self.service.total_frames:
                    raise ValueError("PCM first frame must be before source EOF")
                if first % division:
                    raise ValueError("PCM first frame must align to division")
                frame_bytes = self.service.source_pcm.info.channels * 4
                if count > self.service.source_pcm.max_window_bytes // frame_bytes:
                    raise ValueError("PCM frame count exceeds the source window limit")
                response = self.service.source_pcm.display_window(
                    first, count, division
                )
                event = response.range_event
                if event is None:
                    raise PlayerCacheError("source PCM response has no range event")
                if event.reader_ran:
                    self.log_message(
                        "PCM_RANGE_DECODE id=%d backend=%s raw=%d+%d reader_ms=%.3f",
                        event.event_id,
                        event.backend,
                        event.raw_first_frame,
                        event.raw_frame_count,
                        event.reader_ms,
                    )
                stats = self.service.source_pcm.cache.stats()
                return self.send_bytes(
                    response.data_f32le,
                    headers={
                        "X-Pcm-First-Frame": str(response.first_frame),
                        "X-Pcm-Frame-Count": str(response.frame_count),
                        "X-Pcm-Division": str(response.division),
                        "X-Pcm-Record-Count": str(response.record_count),
                        "X-Pcm-Channels": str(response.channels),
                        "X-Pcm-Components": str(response.components),
                        "X-Pcm-Mode": response.mode,
                        "X-Pcm-Backend": response.backend,
                        "X-Pcm-Raw-Cache-Hit": "1" if response.raw_cache_hit else "0",
                        "X-Pcm-Cache-Disposition": event.cache_disposition,
                        "X-Pcm-Range-Reader-Ran": "1" if event.reader_ran else "0",
                        "X-Pcm-Range-Decode-Ran": "1" if event.reader_ran else "0",
                        "X-Pcm-Range-Reader-Ms": f"{event.reader_ms:.3f}",
                        "X-Pcm-Raw-First-Frame": str(event.raw_first_frame),
                        "X-Pcm-Raw-Frame-Count": str(event.raw_frame_count),
                        "X-Pcm-Range-Event-Id": str(event.event_id),
                        "X-Pcm-Range-Event-Unix-Ms": str(event.occurred_unix_ms),
                        "X-Pcm-Cache-Bytes": str(stats["resident_bytes"]),
                        "X-Pcm-Payload-Bytes": str(response.byte_count),
                    },
                )
            if parsed.path == "/api/wave-tile":
                level = self._int_arg(query, "level", minimum=0)
                tile = self._int_arg(query, "tile", minimum=0)
                first, width, height, raw = self.service.rp.tile_texture(level, tile)
                division = self.service.levels[level][0]
                return self.send_bytes(
                    bytes(raw),
                    headers={
                        "X-First-Peak": str(first),
                        "X-Texture-Width": str(width),
                        "X-Texture-Height": str(height),
                        "X-Division": str(division),
                        "X-Tile-Key": f"L{level}/T{tile}",
                    },
                )
            if parsed.path == "/api/spectral-tile":
                layer = self._int_arg(query, "layer", minimum=0)
                tile = self._int_arg(query, "tile", minimum=0)
                first, width, height, raw = self.service.rp.spectral_tile_texture(
                    layer, tile
                )
                spectral = self.service.native_levels[layer]
                return self.send_bytes(
                    bytes(raw),
                    headers={
                        "X-First-Peak": str(first),
                        "X-Texture-Width": str(width),
                        "X-Texture-Height": str(height),
                        "X-Division": str(spectral["division"]),
                        "X-Tile-Key": f"S{layer}/T{tile}",
                    },
                )
            if parsed.path == "/api/overview":
                width = min(4096, self._int_arg(query, "width", 1200, minimum=1))
                height = min(512, self._int_arg(query, "height", 90, minimum=1))
                start = self._int_arg(query, "start", 0, minimum=0)
                end = self._int_arg(
                    query, "end", self.service.total_frames, minimum=0
                )
                if end <= start:
                    raise ValueError("end must be greater than start")
                raw = self.service.rp.render_rgba(
                    width,
                    height,
                    start,
                    end,
                    background=(17, 20, 26, 255),
                    waveform=(110, 218, 164, 255),
                )
                return self.send_bytes(
                    bytes(raw),
                    headers={
                        "X-Texture-Width": str(width),
                        "X-Texture-Height": str(height),
                    },
                )
        except (ValueError, IndexError, OverflowError) as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            return self.send_json(
                {"error": f"{type(exc).__name__}: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        self.send_error(HTTPStatus.NOT_FOUND)


def parse_divisions(value: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "divisions must be comma-separated integers"
        ) from exc
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("divisions must contain positive integers")
    return values


def parse_mib(value: str, *, allow_zero: bool) -> int:
    try:
        mib = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("MiB value must be numeric") from exc
    if not math.isfinite(mib) or mib < 0 or (mib == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise argparse.ArgumentTypeError(f"MiB value must be {qualifier}")
    result = int(mib * 1024 * 1024)
    if result == 0 and not allow_zero:
        raise argparse.ArgumentTypeError("MiB value rounds to zero bytes")
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--peaks", type=Path)
    parser.add_argument("--rebuild-cache", action="store_true")
    parser.add_argument(
        "--cache-decoder", choices=("auto", "wav", "ffmpeg"), default="auto"
    )
    parser.add_argument(
        "--playback-decoder", choices=("native", "ffmpeg"), default="native"
    )
    parser.add_argument(
        "--cache-mode",
        choices=("auto", "sidecar", "subdir", "central", "reaper"),
        default="auto",
    )
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--reaper-cache-map", type=Path)
    parser.add_argument("--allow-stale-cache", action="store_true")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--decode-timeout", type=float, default=DEFAULT_DECODE_TIMEOUT)
    parser.add_argument(
        "--max-decode-bytes", type=int, default=DEFAULT_MAX_DECODE_BYTES
    )
    parser.add_argument(
        "--wave-encoding", choices=("auto", "rpkn", "rpkl"), default="auto"
    )
    parser.add_argument("--divisions", type=parse_divisions)
    parser.add_argument("--fine-peaks-per-second", type=int, default=300)
    parser.add_argument(
        "--generation-mode",
        choices=("waveform", "spectral", "spectrogram"),
        default="spectral",
        help="REAPER-native cache richness; spectrogram gives wave+s+g+r",
    )
    parser.add_argument(
        "--no-spectral",
        action="store_true",
        help="deprecated compatibility alias for --generation-mode waveform",
    )
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    parser.add_argument(
        "--no-source-pcm",
        action="store_true",
        help="disable automatic high-zoom source PCM windows",
    )
    parser.add_argument(
        "--pcm-decoder",
        choices=("auto", "wav", "ffmpeg"),
        default="auto",
        help="decoder used only for high-zoom source windows",
    )
    parser.add_argument(
        "--pcm-cache-mib",
        dest="pcm_cache_bytes",
        type=lambda value: parse_mib(value, allow_zero=True),
        default=DEFAULT_PCM_CACHE_BYTES,
        metavar="MIB",
        help="byte-bounded decoded-window LRU (default: 64 MiB)",
    )
    parser.add_argument(
        "--pcm-window-mib",
        dest="pcm_max_window_bytes",
        type=lambda value: parse_mib(value, allow_zero=False),
        default=DEFAULT_PCM_MAX_WINDOW_BYTES,
        metavar="MIB",
        help="hard limit for one decoded source window (default: 16 MiB)",
    )
    parser.add_argument(
        "--pcm-page-mib",
        dest="pcm_target_page_bytes",
        type=lambda value: parse_mib(value, allow_zero=False),
        default=DEFAULT_PCM_TARGET_PAGE_BYTES,
        metavar="MIB",
        help="target prefetch-page size before texture limits (default: 1 MiB)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    audio = args.audio.expanduser().resolve(strict=False)
    prepared = None
    server = None
    generation_mode = "waveform" if args.no_spectral else args.generation_mode
    try:
        peaks, generated = ensure_reapeaks_native(
            audio,
            args.peaks,
            generation_mode=generation_mode,
            rebuild=args.rebuild_cache,
            decoder=args.cache_decoder,
            cache_mode=args.cache_mode,
            cache_directory=args.cache_dir,
            reaper_cache_map=args.reaper_cache_map,
            allow_stale_cache=args.allow_stale_cache,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            decode_timeout=args.decode_timeout,
            max_decode_bytes=args.max_decode_bytes,
            wave_encoding=args.wave_encoding,
            divisions=args.divisions,
            fine_peaks_per_second=args.fine_peaks_per_second,
            lock_timeout=args.lock_timeout,
        )
        prepared = prepare_playback_audio(
            audio,
            decoder=args.playback_decoder,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            timeout=args.decode_timeout,
            max_decode_bytes=args.max_decode_bytes,
        )
        service = TileService(
            audio,
            prepared.path,
            peaks,
            generated,
            cache_decoder=args.cache_decoder,
            playback_decoder=args.playback_decoder,
            source_pcm_enabled=not args.no_source_pcm,
            pcm_decoder=args.pcm_decoder,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            decode_timeout=args.decode_timeout,
            pcm_cache_bytes=args.pcm_cache_bytes,
            pcm_max_window_bytes=args.pcm_max_window_bytes,
            pcm_target_page_bytes=args.pcm_target_page_bytes,
        )
        server = DemoHTTPServer((args.host, args.port), DemoHandler)
        server.service = service  # type: ignore[attr-defined]
        print(f"libreapeaks web player: http://{args.host}:{args.port}/")
        print(f"audio={audio}")
        print(f"playback={prepared.path} decoder={prepared.decoder}")
        print(
            f"peaks={peaks}{' (generated)' if generated else ' (reused)'} "
            f"mode={generation_mode}"
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except PlayerCacheError as exc:
        print(f"web_player: {exc}", file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()
        if prepared is not None:
            prepared.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
