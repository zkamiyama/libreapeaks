#!/usr/bin/env python3
"""Temporary corrected entrypoint for cache_extension_oracle.

REAPER's -'r' header count is a count of 32-bit values (two per loudness
record), so the payload occupies count * channels * 4 bytes.
"""

from __future__ import annotations

import cache_extension_oracle as oracle


def payload_size(magic: bytes, channels: int, token: int, count: int) -> int:
    if token > 0:
        bytes_per_channel_peak = 2 if magic == b"RPKM" else 4
        return count * channels * bytes_per_channel_peak
    if token == oracle.TOKEN_SPECTRAL:
        return count * channels * 4
    if token == oracle.TOKEN_SPECTROGRAM:
        return count * channels * 192
    if token == oracle.TOKEN_LOUDNESS:
        return count * channels * 4
    if token == oracle.TOKEN_LOUDNESS_LEGACY:
        raise ValueError("legacy loudness size is deliberately not inferred")
    raise ValueError(f"unknown layer token {token}")


oracle.payload_size = payload_size
raise SystemExit(oracle.main())
