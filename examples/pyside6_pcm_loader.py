"""Non-blocking source-PCM window coordinator for the PySide6 demos."""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from source_pcm import (
    DEFAULT_PCM_MAX_TEXTURE_RECORDS,
    PcmDisplayWindow,
    PcmLodPlan,
    PcmRangeEvent,
    SourcePcmService,
    plan_pcm_lod,
)


class _TaskSignals(QObject):
    finished = Signal(object, object, float)


class _WindowTask(QRunnable):
    def __init__(self, service: SourcePcmService, plan: PcmLodPlan):
        super().__init__()
        self.service = service
        self.plan = plan
        self.signals = _TaskSignals()

    def run(self) -> None:
        started = time.perf_counter_ns()
        try:
            value: object = self.service.display_window(
                self.plan.first_frame,
                self.plan.frame_count,
                self.plan.division,
            )
        except Exception as exc:  # delivered to the GUI thread
            value = exc
        elapsed_ms = (time.perf_counter_ns() - started) / 1e6
        self.signals.finished.emit(self.plan, value, elapsed_ms)


class PcmWindowLoader(QObject):
    """Keep at most one decoder task active and discard stale GUI results.

    The underlying service still coalesces identical requests and owns the
    byte-bounded LRU. This UI layer additionally prevents wheel/pan gestures
    from spawning an unbounded queue of FFmpeg processes.
    """

    changed = Signal()
    rangeAccess = Signal(object)
    rangeDecoded = Signal(object)

    def __init__(self, service: SourcePcmService, parent: QObject | None = None):
        super().__init__(parent)
        self.service = service
        self.source_active = False
        self.requested_plan: PcmLodPlan | None = None
        self.ready_plan: PcmLodPlan | None = None
        self.ready_window: PcmDisplayWindow | None = None
        self._task: _WindowTask | None = None
        self._failed_key: tuple[int, int, int] | None = None
        self.last_error = ""
        self.last_load_ms = 0.0
        self.last_range_event: PcmRangeEvent | None = None

    def plan(
        self,
        view_start: int,
        view_end: int,
        width: int,
        total_frames: int,
        fine_division: int,
        *,
        max_texture_records: int = DEFAULT_PCM_MAX_TEXTURE_RECORDS,
    ) -> PcmLodPlan:
        plan = plan_pcm_lod(
            view_start,
            view_end,
            width,
            total_frames,
            self.service.info.channels,
            fine_division,
            source_active=self.source_active,
            max_window_bytes=self.service.max_window_bytes,
            target_page_bytes=self.service.target_page_bytes,
            max_texture_records=max_texture_records,
        )
        self.source_active = plan.active
        return plan

    def ensure(self, plan: PcmLodPlan) -> PcmDisplayWindow | None:
        if not plan.active or plan.key is None:
            self.requested_plan = None
            return None
        self.requested_plan = plan
        if self.ready_plan is not None and self.ready_plan.key == plan.key:
            return self.ready_window
        if plan.key != self._failed_key and self._task is None:
            self._start(plan)
        return None

    def _start(self, plan: PcmLodPlan) -> None:
        task = _WindowTask(self.service, plan)
        task.signals.finished.connect(self._finished)
        self._task = task
        QThreadPool.globalInstance().start(task)

    def _finished(self, plan: PcmLodPlan, value: object, elapsed_ms: float) -> None:
        self._task = None
        self.last_load_ms = float(elapsed_ms)
        if isinstance(value, Exception):
            if plan.key is not None:
                self._failed_key = plan.key
            if self.requested_plan is not None and self.requested_plan.key == plan.key:
                self.last_error = f"{type(value).__name__}: {value}"
        elif isinstance(value, PcmDisplayWindow):
            self._failed_key = None
            if value.range_event is not None:
                self.last_range_event = value.range_event
                self.rangeAccess.emit(value.range_event)
                if value.range_event.reader_ran:
                    self.rangeDecoded.emit(value.range_event)
            if self.requested_plan is not None and self.requested_plan.key == plan.key:
                self.ready_plan = plan
                self.ready_window = value
                self.last_error = ""

        requested = self.requested_plan
        if (
            requested is not None
            and requested.active
            and requested.key != plan.key
            and requested.key != self._failed_key
        ):
            self._start(requested)
        self.changed.emit()

    def diagnostics(self) -> str:
        stats = self.service.cache.stats()
        state = "pending" if self._task is not None else "idle"
        if (
            self.ready_window is not None
            and self.ready_plan is not None
            and self.requested_plan is not None
            and self.ready_plan.key == self.requested_plan.key
            and self.source_active
        ):
            state = (
                f"{self.ready_window.mode}@{self.ready_window.first_frame}"
                f"+{self.ready_window.frame_count}/d{self.ready_window.division}"
            )
        if self.last_error:
            state += f" fallback={self.last_error}"
        event = self.last_range_event
        range_state = (
            f"last-range=#{event.event_id}/{event.cache_disposition} "
            f"raw={event.raw_first_frame}+{event.raw_frame_count} "
            f"reader={event.reader_ms:.2f}ms"
            if event is not None
            else "last-range=none"
        )
        return (
            f"PCM {state} load={self.last_load_ms:.2f}ms "
            f"LRU={stats['resident_bytes'] / 1048576:.2f}/"
            f"{stats['capacity_bytes'] / 1048576:.2f}MiB "
            f"hit={stats['hits']} miss={stats['misses']} "
            f"coalesced={stats['coalesced']} {range_state}"
        )
