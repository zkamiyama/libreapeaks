from __future__ import annotations

from pathlib import Path
import struct
import sys
import unittest
import uuid

import reapeaks

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "examples"))

from rpkx_inventory import rpkx_inventory_bytes  # noqa: E402


class RpkxInventoryTests(unittest.TestCase):
    def base_cache(self) -> bytes:
        frames = 2_048
        values = [((index * 97) % 65535) - 32768 for index in range(frames)]
        pcm = struct.pack("<" + "h" * len(values), *values)
        divisions = reapeaks.default_divisions(48_000, 300)
        return bytes(
            reapeaks.generate_pcm16_reaper(
                pcm,
                48_000,
                1,
                divisions,
                "waveform",
            )
        )

    def test_plain_cache_reports_no_container(self) -> None:
        inventory = rpkx_inventory_bytes(self.base_cache())
        self.assertFalse(inventory["present"])
        self.assertEqual(inventory["chunk_count"], 0)
        self.assertEqual(inventory["chunks"], [])

    def test_chunk_metadata_and_preview_are_listed(self) -> None:
        namespace = uuid.UUID("12345678-1234-5678-90ab-cdef01234567")
        payload = b"hello\x00RPKX\xffworld"
        extended = bytes(
            reapeaks.rpkx_set_chunk(
                self.base_cache(),
                namespace.bytes,
                b"TEST",
                7,
                payload,
                flags=0x12,
            )
        )

        inventory = rpkx_inventory_bytes(extended, preview_bytes=12)
        self.assertTrue(inventory["present"])
        self.assertEqual(inventory["chunk_count"], 1)
        chunk = inventory["chunks"][0]
        self.assertEqual(chunk["index"], 0)
        self.assertEqual(chunk["namespace"], str(namespace))
        self.assertEqual(chunk["namespace_hex"], namespace.bytes.hex())
        self.assertEqual(chunk["kind"], "TEST")
        self.assertEqual(chunk["kind_hex"], b"TEST".hex())
        self.assertEqual(chunk["version"], 7)
        self.assertEqual(chunk["flags"], 0x12)
        self.assertEqual(chunk["payload_bytes"], len(payload))
        self.assertEqual(chunk["preview"]["hex"], payload[:12].hex(" "))
        self.assertEqual(chunk["preview"]["ascii"], "hello.RPKX.w")


if __name__ == "__main__":
    unittest.main()
