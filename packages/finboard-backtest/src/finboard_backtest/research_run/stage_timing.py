"""研究运行决策装载与协方差的非产物计时。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from time import perf_counter

from finboard_backtest.research_run.contracts import JsonValue


class _StageTimings:
    def __init__(self) -> None:
        self._data: dict[str, tuple[int, float]] = {}
        self._lock = Lock()

    def record(self, stage: str, elapsed_seconds: float) -> None:
        with self._lock:
            count, total = self._data.get(stage, (0, 0.0))
            self._data[stage] = (count + 1, total + elapsed_seconds)

    def as_dict(self) -> dict[str, JsonValue]:
        """汇总各调用的耗时,预取时多个调用可同时计时。"""
        with self._lock:
            return {
                name: {"calls": count, "cumulative_seconds": round(seconds, 6)}
                for name, (count, seconds) in self._data.items()
            }


_timings: ContextVar[_StageTimings | None] = ContextVar("research_stage_timings", default=None)


@contextmanager
def collect_stage_timings() -> Iterator[_StageTimings]:
    timings = _StageTimings()
    token = _timings.set(timings)
    try:
        yield timings
    finally:
        _timings.reset(token)


def record_stage_timing(stage: str, started: float) -> None:
    timings = _timings.get()
    if timings is not None:
        timings.record(stage, perf_counter() - started)
