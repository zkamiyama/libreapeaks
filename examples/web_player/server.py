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
    server_version = "libreapeaks-demo/3"

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
            value = int(values[0])
        if minimum is not None and value < minimum:
            raise ValueError(f"query parameter {name} must be >= {minimum}")
        return value

    @staticmethod
    def _str_arg(query: dict[str, list[str]], name: str) -> str:
        values = query.get(name)
        if not values or not values[0]:
            raise ValueError(f"missing query parameter {name}")
        return values[0]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
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
