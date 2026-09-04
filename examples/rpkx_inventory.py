"""Small shared RPKX inventory helper for the demo GUIs.

The public Python bindings intentionally expose RPKX payloads as opaque bytes.
This module turns those bindings into a JSON/UI-friendly inventory without
assigning application semantics to any namespace or FourCC.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
import uuid

import reapeaks


def _namespace_text(raw: bytes) -> str:
    if len(raw) != 16:
        return raw.hex()
    try:
        return str(uuid.UUID(bytes=raw))
    except ValueError:
        return raw.hex()


def _kind_text(raw: bytes) -> str:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return raw.hex()
    return text if all(32 <= ord(ch) < 127 for ch in text) else raw.hex()


def _preview(payload: bytes, limit: int) -> dict[str, str]:
    prefix = payload[: max(0, int(limit))]
    ascii_preview = "".join(chr(b) if 32 <= b < 127 else "." for b in prefix)
    return {
        "hex": prefix.hex(" "),
        "ascii": ascii_preview,
    }


def rpkx_inventory_bytes(blob: bytes, *, preview_bytes: int = 48) -> dict[str, Any]:
    """Return a display-oriented inventory for one complete `.reapeaks` blob."""

    info = reapeaks.rpkx_container_info(blob)
    if info is None:
        return {
            "present": False,
            "container_flags": 0,
            "source_mtime_low32": 0,
            "source_size_low32": 0,
            "chunk_count": 0,
            "chunks": [],
        }

    container_flags, source_mtime, source_size, declared_count = info
    rows: list[dict[str, Any]] = []
    for index, chunk in enumerate(reapeaks.rpkx_chunks(blob)):
        namespace_raw = bytes(chunk.namespace)
        kind_raw = bytes(chunk.kind)
        payload = bytes(chunk.payload)
        rows.append(
            {
                "index": index,
                "namespace": _namespace_text(namespace_raw),
                "namespace_hex": namespace_raw.hex(),
                "kind": _kind_text(kind_raw),
                "kind_hex": kind_raw.hex(),
                "version": int(chunk.version),
                "flags": int(chunk.flags),
                "payload_bytes": len(payload),
                "preview": _preview(payload, preview_bytes),
            }
        )

    return {
        "present": True,
        "container_flags": int(container_flags),
        "source_mtime_low32": int(source_mtime),
        "source_size_low32": int(source_size),
        "chunk_count": int(declared_count),
        "chunks": rows,
    }


def rpkx_inventory(path: str | Path, *, preview_bytes: int = 48) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve(strict=True)
    payload = resolved.read_bytes()
    result = rpkx_inventory_bytes(payload, preview_bytes=preview_bytes)
    result["path"] = str(resolved)
    result["file_bytes"] = len(payload)
    return result
