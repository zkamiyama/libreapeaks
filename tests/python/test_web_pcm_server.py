from __future__ import annotations

from array import array
import http.client
from pathlib import Path
import sys
import threading
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "examples"))
sys.path.insert(0, str(REPO / "examples" / "web_player"))

import server as web_server  # noqa: E402
import source_pcm as sp  # noqa: E402


class _Reader(sp.PcmWindowReader):
    def __init__(self) -> None:
        self.info = sp.PcmSourceInfo(
            Path("fake.wav"), 48_000, 1, 64, "test-reader", "pcm_f32le"
        )
        self.calls = 0

    def read_window(self, first_frame: int, frame_count: int) -> sp.PcmWindow:
        self.calls += 1
        values = array(
            "f", (float(first_frame + offset) for offset in range(frame_count))
        )
        if sys.byteorder != "little":
            values.byteswap()
        return sp.PcmWindow(
            first_frame,
            frame_count,
            48_000,
            1,
            values.tobytes(),
            "test-reader",
        )


class _Service:
    def __init__(self) -> None:
        self.reader = _Reader()
        self.source_pcm = sp.SourcePcmService(
            self.reader,
            cache_bytes=1024,
            max_window_bytes=256,
            target_page_bytes=64,
        )
        self.source_pcm_error = ""
        self.total_frames = 64


class _QuietHandler(web_server.DemoHandler):
    def log_message(self, _fmt, *_args) -> None:
        pass


class WebPcmServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _Service()
        self.server = web_server.DemoHTTPServer(("127.0.0.1", 0), _QuietHandler)
        self.server.service = self.service
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path: str) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_pcm_response_reports_real_decode_then_cache_hit(self) -> None:
        path = "/api/pcm-window?first=0&count=4&division=1"
        status, headers, body = self.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 4 * 4)
        self.assertEqual(headers["X-Pcm-Cache-Disposition"], "decoded")
        self.assertEqual(headers["X-Pcm-Range-Reader-Ran"], "1")
        self.assertEqual(headers["X-Pcm-Range-Decode-Ran"], "1")
        self.assertEqual(headers["X-Pcm-Payload-Bytes"], str(len(body)))
        first_event = int(headers["X-Pcm-Range-Event-Id"])

        status, headers, body = self.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Pcm-Cache-Disposition"], "cache-hit")
        self.assertEqual(headers["X-Pcm-Range-Reader-Ran"], "0")
        self.assertEqual(headers["X-Pcm-Raw-Cache-Hit"], "1")
        self.assertGreater(int(headers["X-Pcm-Range-Event-Id"]), first_event)
        self.assertEqual(self.service.reader.calls, 1)

    def test_pcm_query_validation_rejects_ambiguous_and_oversized_requests(self) -> None:
        invalid_paths = (
            "/api/pcm-window?first=0&first=1&count=1&division=1",
            "/api/pcm-window?first=&first=0&count=1&division=1",
            "/api/pcm-window?first=0&count=0&division=1",
            "/api/pcm-window?first=64&count=1&division=1",
            "/api/pcm-window?first=1&count=1&division=2",
            "/api/pcm-window?first=0&count=65&division=1",
        )
        for path in invalid_paths:
            with self.subTest(path=path):
                status, _headers, _body = self.request(path)
                self.assertEqual(status, 400)
        self.assertEqual(self.service.reader.calls, 0)

    def test_query_field_flood_returns_400_and_server_remains_healthy(self) -> None:
        query = "&".join(f"field{index}=1" for index in range(33))
        status, _headers, _body = self.request(f"/api/meta?{query}")
        self.assertEqual(status, 400)

        status, _headers, body = self.request(
            "/api/pcm-window?first=0&count=1&division=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
