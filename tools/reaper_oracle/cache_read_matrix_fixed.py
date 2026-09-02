#!/usr/bin/env python3
"""Corrected cache-read comparison using only valid PCM_Source_GetPeaks slots.

PCM_Source_GetPeaks returns the sample count in the low 20 bits. Buffer blocks
remain spaced by the requested samples-per-channel count, so slots beyond the
returned count are unspecified and must not participate in comparisons.
"""

from __future__ import annotations

import re
import cache_read_matrix as matrix

REQUESTED = 16
LINE_RE = re.compile(r"^PEAK rate=(\d+) extra=(\d+) ret=(\d+) values=(.*)$")


def signature(status: str) -> list[str]:
    normalized: list[str] = []
    for line in status.splitlines():
        match = LINE_RE.match(line)
        if match is None:
            continue
        rate, extra, retval_text, raw_values = match.groups()
        retval = int(retval_text)
        returned = retval & 0xFFFFF
        extra_available = bool(retval & 0x1000000)
        values = raw_values.split(",") if raw_values else []
        blocks = 3 if int(extra) != 0 and extra_available else 2
        kept: list[str] = []
        for block in range(blocks):
            start = block * REQUESTED
            kept.extend(values[start : start + returned])
        normalized.append(
            f"PEAK rate={rate} extra={extra} returned={returned} "
            f"output_mode={(retval & 0xF00000) >> 20} "
            f"extra_available={int(extra_available)} values={','.join(kept)}"
        )
    return normalized


matrix.signature = signature
raise SystemExit(matrix.main())
