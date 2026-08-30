"""Local HTTP tile server for the libreapeaks JavaScript player demo.

The browser never receives a giant waveform image. It asks this server for a
view plan and then fetches only visible RGBA8 waveform/spectral data textures.
The server is deliberately thin: planning, lazy derived levels, tile extraction,
overview rendering, and optional cache generation are all delegated to
libreapeaks.

Run from the repository root:
    python examples/web_player/server.py /path/to/audio.wav
Then open http://127.0.0.1:8765/
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
from player_common import ensure_reapeaks, exact_audio_frames  # noqa: E402


class TileService:
    def __init__(self, audio_path: Path, peaks_path: Path, generated: bool):
        self.audio_path = audio_path
        self.peaks_path = peaks_path
        self.generated = generated
        self.rp = reapeaks.ReaPeaks.open(str(peaks_path))
        self.levels = self.rp.levels()
        estimate = max(1, self.levels[0][0] * self.levels[0][1])
        self.total_frames = exact_audio_frames(audio_path, self.rp.sample_rate) or estimate
        self.native_levels = [
            {
                "layer_index": layer_index,
                "level_index": level_index,
                "division": division,
                "wave_peak_count": peak_count,
            }
            for layer_index, (level_index, (division, peak_count, native)) in enumerate(
                (entry for entry in enumerate(self.levels) if entry[1][2])
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

    def meta(self):
        return {
            "audio_name": self.audio_path.name,
            "peaks_name": self.peaks_path.name,
            "generated_cache": self.generated,
            "sample_rate": self.rp.sample_rate,
            "channels": self.rp.channels,
            "wave_encoding": self.rp.wave_encoding,
            "tile_peaks": self.rp.tile_peaks,
            "total_frames": self.total_frames,
            "duration_seconds": self.total_frames / self.rp.sample_rate,
            "default_divisions": reapeaks.default_divisions(self.rp.sample_rate),
            "levels": [
                {
                    "level_index": i,
                    "division": division,
                    "peak_count": peak_count,
                    "native": native,
                }
                for i, (division, peak_count, native) in enumerate(self.levels)
            ],
            "native_spectral_levels": self.native_levels,
            "coarsest_envelope_texture": self.coarsest_texture,
        }

    def choose_spectral(self, desired_division: int):
        if not self.native_levels:
            return None
        return min(
            self.native_levels,
            key=lambda item: abs(
                math.log(max(1, item["division"]) / max(1, desired_division))
            ),
        )

    def plan(self, start: int, end: int, width: int):
        level_index, division, first_peak, peak_count, ppp = self.rp.plan_view(
            start, end, width
        )
        tiles = self.rp.tiles_for_view(start, end, width)
        spectral = self.choose_spectral(division)
        return {
            "level_index": level_index,
            "division": division,
            "first_peak": first_peak,
            "peak_count": peak_count,
            "peaks_per_pixel": ppp,
            "tiles": [
                {"level_index": int(level), "tile_index": int(tile)}
                for level, tile in tiles
            ],
            "spectral": spectral,
        }


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "libreapeaks-demo/1"

    @property
    def service(self) -> TileService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        sys.stderr.write("[web-player] " + (fmt % args) + "\n")

    def send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data: bytes, *, headers: dict[str, str] | None = None):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_static(self, path: Path):
        if not path.is_file() or path.parent != HERE:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_audio(self):
        path = self.service.audio_path
        size = path.stat().st_size
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if range_header and range_header.startswith("bytes="):
            spec = range_header[6:].split(",", 1)[0]
            left, right = spec.split("-", 1)
            if left:
                start = int(left)
                end = int(right) if right else end
            elif right:
                count = int(right)
                start = max(0, size - count)
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            partial = True
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _int_arg(self, query, name: str, default: int | None = None):
        values = query.get(name)
        if not values:
            if default is None:
                raise ValueError(f"missing query parameter {name}")
            return default
        return int(values[0])

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path in ("/", "/index.html"):
                return self.send_static(HERE / "index.html")
            if parsed.path == "/app.js":
                return self.send_static(HERE / "app.js")
            if parsed.path == "/style.css":
                return self.send_static(HERE / "style.css")
            if parsed.path == "/audio":
                return self.send_audio()
            if parsed.path == "/api/meta":
                return self.send_json(self.service.meta())
            if parsed.path == "/api/plan":
                start = self._int_arg(query, "start")
                end = self._int_arg(query, "end")
                width = max(1, self._int_arg(query, "width"))
                return self.send_json(self.service.plan(start, end, width))
            if parsed.path == "/api/wave-tile":
                level = self._int_arg(query, "level")
                tile = self._int_arg(query, "tile")
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
                layer = self._int_arg(query, "layer")
                tile = self._int_arg(query, "tile")
                first, width, height, raw = self.service.rp.spectral_tile_texture(layer, tile)
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
                width = max(1, min(4096, self._int_arg(query, "width", 1200)))
                height = max(1, min(512, self._int_arg(query, "height", 90)))
                start = self._int_arg(query, "start", 0)
                end = self._int_arg(query, "end", self.service.total_frames)
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
        except (ValueError, IndexError) as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.send_error(HTTPStatus.NOT_FOUND)


def parse_args(argv: list[str]):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--peaks", type=Path)
    parser.add_argument("--rebuild-cache", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    audio = args.audio.resolve()
    peaks, generated = ensure_reapeaks(
        audio, args.peaks, rebuild=args.rebuild_cache, spectral=True
    )
    service = TileService(audio, peaks, generated)
    server = ThreadingHTTPServer((args.host, args.port), DemoHandler)
    server.service = service  # type: ignore[attr-defined]
    print(f"libreapeaks web player: http://{args.host}:{args.port}/")
    print(f"audio={audio}")
    print(f"peaks={peaks}{' (generated)' if generated else ''}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
