import math
import struct
import unittest

import reapeaks


def pcm16(frames: int, channels: int) -> bytes:
    values = []
    for frame in range(frames):
        for channel in range(channels):
            value = ((frame * (channel + 3) * 97) % 65535) - 32768
            values.append(max(-32768, min(32767, value)))
    return struct.pack("<" + "h" * len(values), *values)


def layer_tokens(data: bytes) -> list[int]:
    count = data[5]
    return [struct.unpack_from("<iI", data, 18 + index * 8)[0] for index in range(count)]


class ReaperGenerationModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sample_rate = 48_000
        self.channels = 2
        self.divisions = reapeaks.default_divisions(self.sample_rate, 300)
        self.pcm = pcm16(48_137, self.channels)

    def generate(self, mode: str) -> bytes:
        return bytes(
            reapeaks.generate_pcm16_reaper(
                self.pcm,
                self.sample_rate,
                self.channels,
                self.divisions,
                mode,
            )
        )

    def test_exported_mode_names_are_stable(self) -> None:
        self.assertEqual(reapeaks.REAPER_PEAK_MODE_WAVEFORM, "waveform")
        self.assertEqual(reapeaks.REAPER_PEAK_MODE_SPECTRAL, "spectral")
        self.assertEqual(reapeaks.REAPER_PEAK_MODE_SPECTROGRAM, "spectrogram")

    def test_pcm16_modes_match_reaper_native_layer_shapes(self) -> None:
        self.assertEqual(layer_tokens(self.generate("waveform")), self.divisions)
        self.assertEqual(
            layer_tokens(self.generate("spectral")),
            self.divisions + [-115, -115, -115, -114, -114],
        )
        self.assertEqual(
            layer_tokens(self.generate("spectrogram")),
            self.divisions + [-115, -115, -115, -103, -103, -114, -114],
        )

    def test_unknown_mode_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            self.generate("s-only")

    def test_float_spectrogram_emits_native_layer_shape(self) -> None:
        values = [math.sin(index * 0.031) for index in range(48_137)]
        pcm = struct.pack("<" + "f" * len(values), *values)
        data = bytes(
            reapeaks.generate_f32_reaper(
                pcm,
                48_000,
                1,
                self.divisions,
                True,
                "spectrogram",
            )
        )
        self.assertEqual(data[:4], b"RPKL")
        self.assertEqual(
            layer_tokens(data),
            self.divisions + [-115, -115, -115, -103, -103, -114, -114],
        )

    def test_float_spectrogram_special_values_are_safe(self) -> None:
        values = [float("nan"), float("inf"), float("-inf"), 0.0, 0.5, -0.5]
        pcm_values = [values[index % len(values)] for index in range(48_137)]
        pcm = struct.pack("<" + "f" * len(pcm_values), *pcm_values)
        data = bytes(
            reapeaks.generate_f32_reaper(
                pcm,
                48_000,
                1,
                self.divisions,
                True,
                "spectrogram",
            )
        )
        self.assertEqual(data[:4], b"RPKL")


if __name__ == "__main__":
    unittest.main()
