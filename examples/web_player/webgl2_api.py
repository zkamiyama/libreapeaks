"""Zero-display-conversion API helper for the WebGL2 web player path."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import reapeaks

KINDS = ("waveform", "spectral", "spectrogram", "loudness")


@dataclass(frozen=True)
class RawGpuResponse:
    data: bytes
    headers: dict[str, str]


class RawGpuService:
    """Index `.reapeaks` once and return exact on-disk record windows."""

    def __init__(self, peaks_path: str):
        self.view = reapeaks.GpuCacheView.open(peaks_path)
        self.levels: dict[str, list[dict[str, int]]] = {}
        for kind in KINDS:
            rows = []
            for layer_index, (division, records, bytes_per_channel) in enumerate(
                self.view.levels(kind)
            ):
                rows.append(
                    {
                        "layer_index": int(layer_index),
                        "division": int(division),
                        "record_count": int(records),
                        "bytes_per_channel_record": int(bytes_per_channel),
                    }
                )
            self.levels[kind] = rows

    def meta(self) -> dict[str, Any]:
        return {
            "gpu_raw_bytes": int(self.view.raw_bytes),
            "gpu_layers": self.levels,
            "gpu_wave_encoding": self.view.wave_encoding,
        }

    def records(
        self,
        kind: str,
        layer_index: int,
        first_record: int,
        record_count: int,
    ) -> RawGpuResponse:
        if kind not in KINDS:
            raise ValueError(
                "kind must be waveform, spectral, spectrogram, or loudness"
            )
        if layer_index < 0 or first_record < 0 or record_count <= 0:
            raise ValueError("invalid GPU record range")
        try:
            layer = self.levels[kind][layer_index]
        except IndexError as exc:
            raise ValueError("GPU layer index out of range") from exc
        first, records, channels, bytes_per_channel, raw = self.view.records(
            kind,
            layer_index,
            first_record,
            record_count,
        )
        data = bytes(raw)
        return RawGpuResponse(
            data=data,
            headers={
                "X-Layer-Kind": kind,
                "X-Layer-Index": str(layer_index),
                "X-Division": str(layer["division"]),
                "X-First-Record": str(int(first)),
                "X-Record-Count": str(int(records)),
                "X-Channels": str(int(channels)),
                "X-Bytes-Per-Channel-Record": str(int(bytes_per_channel)),
                "X-Payload-Bytes": str(len(data)),
            },
        )
