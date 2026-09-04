"""Reusable RPKX inventory dock for the PySide6 libreapeaks demos."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDockWidget,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rpkx_inventory import rpkx_inventory


class RpkxInventoryWidget(QWidget):
    def __init__(self, peaks_path: str | Path, parent=None):
        super().__init__(parent)
        self.peaks_path = Path(peaks_path)
        try:
            self.inventory = rpkx_inventory(self.peaks_path)
        except Exception as exc:  # RPKX is optional; never take down playback.
            self.inventory = {
                "present": False,
                "chunks": [],
                "chunk_count": 0,
                "file_bytes": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }

        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.table = QTableWidget(self)
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["#", "Namespace", "Kind", "Version", "Flags", "Payload bytes", "Preview"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)

        self.detail = QPlainTextEdit(self)
        self.detail.setReadOnly(True)
        self.detail.setMaximumBlockCount(256)
        self.detail.setPlaceholderText("Select an RPKX chunk to inspect its opaque payload preview.")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.detail)

        self.table.currentCellChanged.connect(self._selection_changed)
        self._populate()

    def _populate(self) -> None:
        info = self.inventory
        if not info["present"]:
            if info.get("error"):
                self.summary.setText(
                    f"{self.peaks_path.name}: RPKX inventory unavailable — {info['error']}"
                )
            else:
                self.summary.setText(
                    f"{self.peaks_path.name}: no RPKX container is attached. "
                    f"File size: {info.get('file_bytes', 0):,} bytes."
                )
            self.table.setRowCount(0)
            return

        self.summary.setText(
            f"RPKX v1 inventory — chunks={info['chunk_count']} "
            f"container_flags=0x{info['container_flags']:08x} | "
            f"source stamp mtime=0x{info['source_mtime_low32']:08x} "
            f"size=0x{info['source_size_low32']:08x} | "
            f"{self.peaks_path.name} ({info.get('file_bytes', 0):,} bytes)"
        )
        chunks = info["chunks"]
        self.table.setRowCount(len(chunks))
        for row, chunk in enumerate(chunks):
            values = (
                str(chunk["index"]),
                chunk["namespace"],
                chunk["kind"],
                str(chunk["version"]),
                f"0x{chunk['flags']:08x}",
                f"{chunk['payload_bytes']:,}",
                chunk["preview"]["ascii"],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (0, 3, 4, 5):
                    item.setTextAlignment(
                        int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    )
                self.table.setItem(row, column, item)
        if chunks:
            self.table.selectRow(0)
            self._show_detail(0)

    def _selection_changed(self, current_row: int, _current_col: int, *_args) -> None:
        if current_row >= 0:
            self._show_detail(current_row)

    def _show_detail(self, row: int) -> None:
        chunks = self.inventory.get("chunks", [])
        if not (0 <= row < len(chunks)):
            self.detail.clear()
            return
        chunk = chunks[row]
        preview = chunk["preview"]
        self.detail.setPlainText(
            "\n".join(
                (
                    f"namespace: {chunk['namespace']}",
                    f"namespace bytes: {chunk['namespace_hex']}",
                    f"kind: {chunk['kind']} ({chunk['kind_hex']})",
                    f"version: {chunk['version']}",
                    f"flags: 0x{chunk['flags']:08x}",
                    f"payload: {chunk['payload_bytes']:,} bytes",
                    f"preview hex: {preview['hex'] or '(empty)'}",
                    f"preview ascii: {preview['ascii'] or '(empty)'}",
                )
            )
        )


def install_rpkx_dock(window, peaks_path: str | Path) -> QDockWidget:
    """Attach the same RPKX inventory view to any QMainWindow demo."""

    dock = QDockWidget("RPKX container", window)
    dock.setObjectName("libreapeaksRpkxDock")
    dock.setAllowedAreas(
        Qt.DockWidgetArea.BottomDockWidgetArea
        | Qt.DockWidgetArea.LeftDockWidgetArea
        | Qt.DockWidgetArea.RightDockWidgetArea
    )
    dock.setWidget(RpkxInventoryWidget(peaks_path, dock))
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
    return dock
