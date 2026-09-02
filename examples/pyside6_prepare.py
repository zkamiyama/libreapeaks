"""Responsive full-cache preparation dialog for the PySide6 demo player."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from player_native_cache import NativeGenerationMode, ensure_reapeaks_native


FULL_GENERATION_MODE: NativeGenerationMode = "spectrogram"


class CacheWorker(QObject):
    progress = Signal(str, int)
    completed = Signal(object, bool, str)
    failed = Signal(str)

    def __init__(self, audio: Path, options: dict[str, object]):
        super().__init__()
        self.audio = audio
        self.options = dict(options)

    @Slot()
    def run(self) -> None:
        try:
            peaks, generated = ensure_reapeaks_native(
                self.audio,
                generation_mode=FULL_GENERATION_MODE,
                progress=lambda stage, value: self.progress.emit(stage, value),
                **self.options,
            )
        except Exception as exc:  # PyO3 errors have no stable shared base class.
            self.failed.emit(str(exc))
            return
        self.completed.emit(peaks, generated, FULL_GENERATION_MODE)


class CachePreparationDialog(QDialog):
    """Build the complete REAPER-native analysis cache off the UI thread."""

    def __init__(
        self,
        audio: Path,
        *,
        options: dict[str, object],
        initial_mode: NativeGenerationMode = FULL_GENERATION_MODE,
        parent=None,
    ):
        super().__init__(parent)
        # Keep ``initial_mode`` in the public demo signature so older callers do
        # not break, but GUI cache preparation intentionally always generates
        # the complete native spectrogram shape.
        _ = initial_mode
        self.audio = audio
        self.options = dict(options)
        self.peaks_path: Path | None = None
        self.generated = False
        self.generation_mode: NativeGenerationMode = FULL_GENERATION_MODE
        self._thread: QThread | None = None
        self._worker: CacheWorker | None = None

        self.setWindowTitle("Prepare libreapeaks cache")
        self.setModal(True)
        self.resize(620, 190)

        self.summary = QLabel(
            "The player creates the complete REAPER-compatible analysis cache: "
            "waveform, spectral peaks, spectrogram, and loudness. "
            "Generation runs on a worker thread so the UI remains responsive."
        )
        self.summary.setWordWrap(True)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)

        self.status = QLabel("Ready")
        self.status.setWordWrap(True)

        self.start_button = QPushButton("Prepare and open")
        self.start_button.clicked.connect(self.start)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.start_button)

        layout = QVBoxLayout()
        layout.addWidget(self.summary)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        self.setLayout(layout)

    @Slot()
    def start(self) -> None:
        if self._thread is not None:
            return
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.progress.setValue(0)
        self.status.setText("Starting full cache preparation…")

        thread = QThread(self)
        worker = CacheWorker(self.audio, self.options)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._worker_finished)
        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot(str, int)
    def _on_progress(self, stage: str, value: int) -> None:
        self.progress.setValue(value)
        self.status.setText(stage)

    @Slot(object, bool, str)
    def _on_completed(self, peaks_path: object, generated: bool, mode: str) -> None:
        self.peaks_path = Path(peaks_path)
        self.generated = bool(generated)
        self.generation_mode = mode  # type: ignore[assignment]
        self.progress.setValue(100)
        self.status.setText(
            f"Ready: {self.peaks_path.name}"
            f" ({'generated' if self.generated else 'reused'})"
        )

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.status.setText(f"Cache preparation failed: {message}")
        self.progress.setValue(0)

    @Slot()
    def _worker_finished(self) -> None:
        completed = self.peaks_path is not None
        self._worker = None
        self._thread = None
        if completed:
            self.accept()
            return
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(True)

    def reject(self) -> None:
        # QThread termination would be unsafe while Rust/FFmpeg owns buffers.
        # Disable cancellation once work begins and let the worker finish cleanly.
        if self._thread is None:
            super().reject()
