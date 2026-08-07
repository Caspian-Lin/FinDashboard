"""特征快照后台任务的进度与生命周期管理。

特征快照属于研究域计算,任务状态只保存在当前 API 进程内。管理器不持有
数据库 session,后台 runner 必须自行创建独立 session,避免把请求 session
跨越请求生命周期传入任务。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)

FeatureSnapshotJobStatus = Literal["queued", "running", "succeeded", "failed"]
FeatureSnapshotJobRunner = Callable[["FeatureSnapshotJob"], Awaitable[str]]


class FeatureSnapshotJobConflictError(RuntimeError):
    """当前已有特征快照任务在排队或运行。"""


@dataclass(slots=True)
class FeatureSnapshotJob:
    """可序列化为 API 状态的单个特征快照任务。"""

    job_id: str
    total_symbols: int
    completed_symbols: int = 0
    status: FeatureSnapshotJobStatus = "queued"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    snapshot_id: str | None = None
    error: str | None = None
    _started_monotonic: float | None = field(default=None, repr=False)
    _finished_monotonic: float | None = field(default=None, repr=False)

    def mark_running(self) -> None:
        if self.status != "queued":
            return
        self.status = "running"
        self.started_at = datetime.now(UTC)
        self._started_monotonic = time.monotonic()

    def update_progress(self, completed_symbols: int) -> None:
        """更新已完成标的数,只接受单调递增的进度。"""

        self.completed_symbols = min(
            self.total_symbols,
            max(self.completed_symbols, completed_symbols),
        )

    def mark_succeeded(self, snapshot_id: str) -> None:
        self.completed_symbols = self.total_symbols
        self.status = "succeeded"
        self.snapshot_id = snapshot_id
        self.finished_at = datetime.now(UTC)
        self._finished_monotonic = time.monotonic()

    def mark_failed(self, error: str) -> None:
        self.status = "failed"
        self.error = error or "特征快照任务失败"
        self.finished_at = datetime.now(UTC)
        self._finished_monotonic = time.monotonic()

    def _elapsed_seconds(self) -> float:
        if self._started_monotonic is None:
            return 0.0
        end = self._finished_monotonic or time.monotonic()
        return max(0.0, end - self._started_monotonic)

    def as_dict(self) -> dict[str, object]:
        elapsed = self._elapsed_seconds()
        progress_pct = (
            round(self.completed_symbols / self.total_symbols * 100, 2)
            if self.total_symbols
            else 100.0
        )
        estimated_remaining: float | None = None
        if (
            self.status == "running"
            and self.completed_symbols > 0
            and self.completed_symbols < self.total_symbols
        ):
            estimated_remaining = max(
                0.0,
                elapsed
                / self.completed_symbols
                * (self.total_symbols - self.completed_symbols),
            )
        return {
            "job_id": self.job_id,
            "status": self.status,
            "total_symbols": self.total_symbols,
            "completed_symbols": self.completed_symbols,
            "progress_pct": progress_pct,
            "elapsed_seconds": round(elapsed, 3),
            "estimated_remaining_seconds": (
                round(estimated_remaining, 3)
                if estimated_remaining is not None
                else None
            ),
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
            "snapshot_id": self.snapshot_id,
            "error": self.error,
        }


class FeatureSnapshotJobManager:
    """管理单进程内的特征快照任务。

    当前只允许一个任务处于 queued/running,避免同一个冻结发布被多个任务
    同时读取并发布重复快照。已完成任务保留有限历史,供前端轮询到终态。
    """

    def __init__(self, *, max_history: int = 32) -> None:
        if max_history < 1:
            raise ValueError("max_history 必须 >= 1")
        self._max_history = max_history
        self._jobs: dict[str, FeatureSnapshotJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(
        self,
        *,
        total_symbols: int,
        runner: FeatureSnapshotJobRunner,
    ) -> FeatureSnapshotJob:
        if total_symbols < 1:
            raise ValueError("total_symbols 必须 >= 1")
        if any(
            job.status in {"queued", "running"} for job in self._jobs.values()
        ):
            raise FeatureSnapshotJobConflictError("特征快照任务正在运行中")

        job = FeatureSnapshotJob(
            job_id=f"FSJ-{uuid.uuid4().hex[:16].upper()}",
            total_symbols=total_symbols,
        )
        self._jobs[job.job_id] = job
        task = asyncio.create_task(
            self._run(job, runner),
            name=f"feature-snapshot-{job.job_id}",
        )
        self._tasks[job.job_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(job.job_id, None))
        self._prune()
        return job

    def get(self, job_id: str) -> FeatureSnapshotJob | None:
        return self._jobs.get(job_id)

    async def close(self) -> None:
        """应用关闭时取消仍在运行的任务。"""

        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run(
        self,
        job: FeatureSnapshotJob,
        runner: FeatureSnapshotJobRunner,
    ) -> None:
        job.mark_running()
        try:
            snapshot_id = await runner(job)
        except asyncio.CancelledError:
            job.mark_failed("特征快照任务因应用关闭而取消")
            raise
        except Exception as exc:
            message = str(exc).strip() or "特征快照任务失败"
            job.mark_failed(message)
            logger.exception("research.feature_snapshot_job_failed", extra={"job_id": job.job_id})
        else:
            job.mark_succeeded(snapshot_id)

    def _prune(self) -> None:
        if len(self._jobs) <= self._max_history:
            return
        terminal = [
            job
            for job in self._jobs.values()
            if job.status in {"succeeded", "failed"}
        ]
        terminal.sort(key=lambda job: job.created_at)
        for job in terminal[: max(0, len(self._jobs) - self._max_history)]:
            self._jobs.pop(job.job_id, None)


__all__ = [
    "FeatureSnapshotJob",
    "FeatureSnapshotJobConflictError",
    "FeatureSnapshotJobManager",
    "FeatureSnapshotJobRunner",
    "FeatureSnapshotJobStatus",
]
