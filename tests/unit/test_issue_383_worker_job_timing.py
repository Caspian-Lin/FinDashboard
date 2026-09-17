"""issue #383:worker 统一 job timing —— ``build_job_timing`` 纯函数与
``_finalize`` 对 ``repo.finish`` 的 timing 转传。

``_run_one`` 的端到端计时注入(成功 / 失败路径落 timing 列)由
``tests/integration/test_issue_383_job_timing.py``(真 PG)覆盖。
"""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import MagicMock

from finboard_backtest.background_jobs.contracts import JobResult
from finboard_backtest.background_jobs.worker import (
    BackgroundWorker,
    WorkerConfig,
    build_job_timing,
)
from finboard_data.cache import ParquetReadJobStats
from finboard_shared.background_jobs import generate_background_job_id


def _make_worker() -> BackgroundWorker:
    return BackgroundWorker(
        engine=MagicMock(),
        session_maker=MagicMock(),
        registry=MagicMock(),
        config=WorkerConfig(
            worker_id="w-test",
            poll_interval_seconds=0.05,
            max_concurrent=1,
            lease_timeout_seconds=60,
            heartbeat_interval_seconds=10.0,
        ),
    )


class _FakeSessionCM:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSessionMaker:
    def __call__(self) -> _FakeSessionCM:
        return _FakeSessionCM()


def _attach_fake_session_maker(worker: BackgroundWorker) -> None:
    worker._session_maker = _FakeSessionMaker()  # type: ignore[assignment]


class TestBuildJobTiming:
    def test_shape_and_elapsed(self) -> None:
        started = time.monotonic()
        time.sleep(0.05)
        stats = ParquetReadJobStats()
        stats.record("read", elapsed_ms=12.5, size_bytes=4096)
        timing = build_job_timing(started, stats)

        # round(_, 3) + Windows 定时器粒度:给足余量防抖动。
        assert cast("float", timing["execute_elapsed_seconds"]) >= 0.04
        reads = cast("dict[str, Any]", timing["parquet_reads"])
        assert reads == stats.as_dict()
        assert reads["read_ops"] == 1
        assert reads["read_elapsed_ms"] == 12.5

    def test_zero_reads(self) -> None:
        timing = build_job_timing(time.monotonic(), ParquetReadJobStats())
        reads = cast("dict[str, Any]", timing["parquet_reads"])
        assert reads["read_ops"] == 0
        assert reads["read_bytes"] == 0


class TestFinalizePassesTiming:
    async def test_timing_flows_to_repo_finish(self, monkeypatch) -> None:
        worker = _make_worker()
        captured: dict[str, object] = {}

        class _FakeRepo:
            def __init__(self, session: object) -> None:
                pass

            async def finish(self, job_id: str, **kwargs: object) -> None:
                captured.update(kwargs)

            async def checkpoint(self) -> None:
                return None

        monkeypatch.setattr(
            "finboard_backtest.background_jobs.worker.BackgroundJobRepository",
            _FakeRepo,
        )
        _attach_fake_session_maker(worker)

        timing = {"execute_elapsed_seconds": 0.5, "parquet_reads": {"read_ops": 3}}
        await worker._finalize(
            generate_background_job_id(),
            JobResult(status="succeeded", timing=timing),
        )
        assert captured["timing"] == timing

    async def test_none_timing_passes_none(self, monkeypatch) -> None:
        worker = _make_worker()
        captured: dict[str, object] = {}

        class _FakeRepo:
            def __init__(self, session: object) -> None:
                pass

            async def finish(self, job_id: str, **kwargs: object) -> None:
                captured.update(kwargs)

            async def checkpoint(self) -> None:
                return None

        monkeypatch.setattr(
            "finboard_backtest.background_jobs.worker.BackgroundJobRepository",
            _FakeRepo,
        )
        _attach_fake_session_maker(worker)

        await worker._finalize(
            generate_background_job_id(),
            JobResult(status="succeeded", timing=None),
        )
        assert captured["timing"] is None

    async def test_conflict_keeps_silent(self, monkeypatch) -> None:
        """状态已被并发流转改变时,finalize 静默让位(既有语义不回归)。"""

        from finboard_persistence import BackgroundJobPersistenceConflictError

        worker = _make_worker()

        class _FakeRepo:
            def __init__(self, session: object) -> None:
                pass

            async def finish(self, job_id: str, **kwargs: object) -> None:
                raise BackgroundJobPersistenceConflictError("conflict")

            async def checkpoint(self) -> None:
                return None

        monkeypatch.setattr(
            "finboard_backtest.background_jobs.worker.BackgroundJobRepository",
            _FakeRepo,
        )
        _attach_fake_session_maker(worker)

        await worker._finalize(
            generate_background_job_id(),
            JobResult(status="succeeded", timing={"a": 1}),
        )
