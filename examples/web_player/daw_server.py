"""Enhanced local HTTP server for the PySide-parity libreapeaks web GUI.

This wraps the existing web player service rather than duplicating its packed
GPU and source-PCM endpoints. It adds:

* the DAW web page/assets;
* browser file open / drag-and-drop with background full-cache preparation;
* progress/session reporting; and
* an RPKX container inventory endpoint.

Run with an optional initial source:

    python examples/web_player/daw_server.py song.wav
    python examples/web_player/daw_server.py
"""
from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
import sys
import tempfile
import threading
import uuid
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent
if str(EXAMPLES) not in sys.path:
    sys.path.insert(0, str(EXAMPLES))

import server as base  # noqa: E402
from player_native_cache import ensure_reapeaks_native  # noqa: E402
from player_common import prepare_playback_audio  # noqa: E402
from rpkx_inventory import rpkx_inventory  # noqa: E402


MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024


class SessionState:
    def __init__(self, options: argparse.Namespace):
        self.options = options
        self.lock = threading.RLock()
        self.server = None
        self.revision = 0
        self.building = False
        self.progress = 0
        self.status = "Waiting for audio"
        self.error = ""
        self.audio_path: Path | None = None
        self.peaks_path: Path | None = None
        self.generated = False
        self.prepared = None
        self.tempdir = tempfile.TemporaryDirectory(prefix="libreapeaks-web-daw-")

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "ready": self.audio_path is not None and self.peaks_path is not None and self.prepared is not None,
                "building": self.building,
                "revision": self.revision,
                "progress": self.progress,
                "status": self.status,
                "error": self.error,
                "audio_name": self.audio_path.name if self.audio_path is not None else None,
                "peaks_name": self.peaks_path.name if self.peaks_path is not None else None,
                "generated_cache": self.generated,
            }

    def _progress(self, stage: str, value: int) -> None:
        with self.lock:
            self.status = str(stage)
            self.progress = max(0, min(100, int(value)))

    def _build_service(self, audio: Path, *, peaks_path: Path | None) -> tuple[object, Path, bool, object]:
        opts = self.options
        peaks, generated = ensure_reapeaks_native(
            audio,
            peaks_path,
            generation_mode="spectrogram",
            rebuild=bool(opts.rebuild_cache),
            decoder=opts.cache_decoder,
            cache_mode=opts.cache_mode,
            cache_directory=opts.cache_dir,
            reaper_cache_map=opts.reaper_cache_map,
            allow_stale_cache=bool(opts.allow_stale_cache),
            ffmpeg=opts.ffmpeg,
            ffprobe=opts.ffprobe,
            decode_timeout=opts.decode_timeout,
            max_decode_bytes=opts.max_decode_bytes,
            wave_encoding=opts.wave_encoding,
            divisions=opts.divisions,
            fine_peaks_per_second=opts.fine_peaks_per_second,
            lock_timeout=opts.lock_timeout,
            progress=self._progress,
        )
        self._progress("Preparing playback source", 100)
        prepared = prepare_playback_audio(
            audio,
            decoder=opts.playback_decoder,
            ffmpeg=opts.ffmpeg,
            ffprobe=opts.ffprobe,
            timeout=opts.decode_timeout,
            max_decode_bytes=opts.max_decode_bytes,
        )
        service = base.TileService(
            audio,
            prepared.path,
            peaks,
            generated,
            cache_decoder=opts.cache_decoder,
            playback_decoder=opts.playback_decoder,
            source_pcm_enabled=not opts.no_source_pcm,
            pcm_decoder=opts.pcm_decoder,
            ffmpeg=opts.ffmpeg,
            ffprobe=opts.ffprobe,
            decode_timeout=opts.decode_timeout,
            pcm_cache_bytes=opts.pcm_cache_bytes,
            pcm_max_window_bytes=opts.pcm_max_window_bytes,
            pcm_target_page_bytes=opts.pcm_target_page_bytes,
        )
        return service, peaks, generated, prepared

    def _publish(self, audio: Path, service, peaks: Path, generated: bool, prepared) -> None:
        old_prepared = None
        with self.lock:
            old_prepared = self.prepared
            self.audio_path = audio
            self.peaks_path = peaks
            self.generated = bool(generated)
            self.prepared = prepared
            self.revision += 1
            self.progress = 100
            self.status = f"Ready: {audio.name}"
            self.error = ""
            self.building = False
            if self.server is not None:
                self.server.service = service
        if old_prepared is not None:
            try:
                old_prepared.close()
            except Exception:
                pass

    def build_initial(self, audio: Path, *, peaks_path: Path | None = None) -> None:
        with self.lock:
            self.building = True
            self.progress = 0
            self.status = f"Preparing {audio.name}"
            self.error = ""
        prepared = None
        try:
            service, peaks, generated, prepared = self._build_service(audio, peaks_path=peaks_path)
            self._publish(audio, service, peaks, generated, prepared)
        except Exception:
            if prepared is not None:
                prepared.close()
            with self.lock:
                self.building = False
            raise

    def start(self, audio: Path) -> int:
        with self.lock:
            if self.building:
                raise RuntimeError("cache preparation is already running")
            self.building = True
            self.progress = 0
            self.status = f"Preparing {audio.name}"
            self.error = ""
            target_revision = self.revision + 1

        def worker() -> None:
            prepared = None
            try:
                service, peaks, generated, prepared = self._build_service(audio, peaks_path=None)
                self._publish(audio, service, peaks, generated, prepared)
            except Exception as exc:
                if prepared is not None:
                    try:
                        prepared.close()
                    except Exception:
                        pass
                with self.lock:
                    self.building = False
                    self.progress = 0
                    self.error = f"{type(exc).__name__}: {exc}"
                    self.status = "Preparation failed"

        threading.Thread(target=worker, name="libreapeaks-web-prepare", daemon=True).start()
        return target_revision

    def save_upload(self, name: str, stream, length: int) -> Path:
        safe_name = Path(name or "uploaded-audio").name
        suffix = Path(safe_name).suffix[:32]
        destination = Path(self.tempdir.name) / f"upload-{uuid.uuid4().hex}{suffix}"
        remaining = length
        with destination.open("wb") as handle:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("upload ended before Content-Length bytes were received")
                handle.write(chunk)
                remaining -= len(chunk)
        return destination

    def close(self) -> None:
        prepared = None
        with self.lock:
            prepared = self.prepared
            self.prepared = None
        if prepared is not None:
            try:
                prepared.close()
            except Exception:
                pass
        self.tempdir.cleanup()


class DawHandler(base.DemoHandler):
    @property
    def session(self) -> SessionState:
        return self.server.session  # type: ignore[attr-defined]

    def _service_ready(self) -> bool:
        return bool(self.session.snapshot()["ready"])

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/daw"):
            return self.send_static(HERE / "daw_index.html")
        if parsed.path == "/daw_bootstrap.mjs":
            return self.send_static(HERE / "daw_bootstrap.mjs")
        if parsed.path == "/daw_app.mjs":
            return self.send_static(HERE / "daw_app.mjs")
        if parsed.path == "/daw_render_math.mjs":
            return self.send_static(HERE / "daw_render_math.mjs")
        if parsed.path == "/daw_style.css":
            return self.send_static(HERE / "daw_style.css")
        if parsed.path == "/api/session":
            return self.send_json(self.session.snapshot())
        if parsed.path == "/api/rpkx":
            snapshot = self.session.snapshot()
            if not snapshot["ready"]:
                return self.send_json({"error": "no audio/cache is open"}, HTTPStatus.CONFLICT)
            assert self.session.peaks_path is not None
            try:
                inventory = rpkx_inventory(self.session.peaks_path)
                inventory["peaks_name"] = self.session.peaks_path.name
                return self.send_json(inventory)
            except Exception as exc:
                return self.send_json(
                    {"error": f"{type(exc).__name__}: {exc}"},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        if parsed.path == "/audio" or parsed.path.startswith("/api/"):
            if not self._service_ready():
                return self.send_json({"error": "no audio/cache is open"}, HTTPStatus.CONFLICT)
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/open":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            if self.session.snapshot()["building"]:
                return self.send_json(
                    {"error": "cache preparation is already running"},
                    HTTPStatus.CONFLICT,
                )
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
            name_values = query.get("name", ["uploaded-audio"])
            name = name_values[0] if name_values else "uploaded-audio"
            length_value = self.headers.get("Content-Length")
            if length_value is None:
                raise ValueError("Content-Length is required")
            length = int(length_value)
            if length <= 0:
                raise ValueError("empty uploads are not supported")
            if length > MAX_UPLOAD_BYTES:
                return self.send_json(
                    {"error": f"upload exceeds {MAX_UPLOAD_BYTES} byte limit"},
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
            uploaded = self.session.save_upload(name, self.rfile, length)
            target_revision = self.session.start(uploaded)
            return self.send_json(
                {
                    "accepted": True,
                    "target_revision": target_revision,
                    "audio_name": Path(name).name,
                },
                HTTPStatus.ACCEPTED,
            )
        except (ValueError, OSError) as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except Exception as exc:
            return self.send_json(
                {"error": f"{type(exc).__name__}: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def parse_divisions(value: str) -> list[int]:
    return base.parse_divisions(value)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, nargs="?", help="optional initial media file")
    parser.add_argument("--peaks", type=Path, help="existing/target cache for the initial audio")
    parser.add_argument("--rebuild-cache", action="store_true")
    parser.add_argument("--cache-decoder", choices=("auto", "wav", "ffmpeg"), default="auto")
    parser.add_argument("--playback-decoder", choices=("native", "ffmpeg"), default="native")
    parser.add_argument(
        "--cache-mode", choices=("auto", "sidecar", "subdir", "central", "reaper"), default="auto"
    )
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--reaper-cache-map", type=Path)
    parser.add_argument("--allow-stale-cache", action="store_true")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--decode-timeout", type=float, default=base.DEFAULT_DECODE_TIMEOUT)
    parser.add_argument("--max-decode-bytes", type=int, default=base.DEFAULT_MAX_DECODE_BYTES)
    parser.add_argument("--wave-encoding", choices=("auto", "rpkn", "rpkl"), default="auto")
    parser.add_argument("--divisions", type=parse_divisions)
    parser.add_argument("--fine-peaks-per-second", type=int, default=300)
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    parser.add_argument("--no-source-pcm", action="store_true")
    parser.add_argument("--pcm-decoder", choices=("auto", "wav", "ffmpeg"), default="auto")
    parser.add_argument(
        "--pcm-cache-mib",
        dest="pcm_cache_bytes",
        type=lambda value: base.parse_mib(value, allow_zero=True),
        default=base.DEFAULT_PCM_CACHE_BYTES,
        metavar="MIB",
    )
    parser.add_argument(
        "--pcm-window-mib",
        dest="pcm_max_window_bytes",
        type=lambda value: base.parse_mib(value, allow_zero=False),
        default=base.DEFAULT_PCM_MAX_WINDOW_BYTES,
        metavar="MIB",
    )
    parser.add_argument(
        "--pcm-page-mib",
        dest="pcm_target_page_bytes",
        type=lambda value: base.parse_mib(value, allow_zero=False),
        default=base.DEFAULT_PCM_TARGET_PAGE_BYTES,
        metavar="MIB",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    session = SessionState(args)
    server = base.DemoHTTPServer((args.host, args.port), DawHandler)
    server.session = session  # type: ignore[attr-defined]
    session.server = server
    try:
        if args.audio is not None:
            audio = args.audio.expanduser().resolve(strict=False)
            if not audio.is_file():
                raise base.PlayerCacheError(f"audio file not found: {audio}")
            peaks = args.peaks.expanduser().resolve(strict=False) if args.peaks else None
            session.build_initial(audio, peaks_path=peaks)
        print(f"libreapeaks DAW web player: http://{args.host}:{args.port}/")
        if args.audio is None:
            print("Open or drop an audio file in the browser to prepare the full cache.")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except base.PlayerCacheError as exc:
        print(f"daw_server: {exc}", file=sys.stderr)
        return 2
    finally:
        server.server_close()
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
