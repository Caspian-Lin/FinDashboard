"""issue #383:所有 job kind 通用 timing 落库(background_jobs.timing 列)。

worker 在 ``_execute_with_heart`` 外统一测量 wall-clock + parquet 读取聚合,
成功与失败兜底路径都落 ``timing`` 列。真 PG 端到端:入队 → worker 消费 →
读行断言 timing 形态。前置与 ``test_background_job_persistence.py`` 相同
(本地 PostgreSQL,conftest 自动建 findashboard_test 库)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs import JobExecutorRegistry
from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.background_jobs.worker import BackgroundWorker, WorkerConfig
from finboard_data.cache import ParquetCache, make_symbol
from finboard_persistence import (
    BackgroundJobRepository,
    Base,
    create_async_engine,
    session_factory,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market


def _checksum(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def _enqueue(
    engine: AsyncEngine,
    *,
    kind: str,
    payload: dict[str, object],
) -> str:
    job_id = generate_background_job_id()
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        await repo.create_or_get(
            job_id=job_id,
            idempotency_key=f"idem-{job_id}",
            kind=kind,
            queue="default",
            status=BackgroundJobStatus.QUEUED.value,
            priority=0,
            payload=payload,
            payload_checksum=_checksum(payload),
            max_attempts=3,
            requested_by="tester",
        )
        await repo.checkpoint()
    return job_id


async def _drain_worker(worker: BackgroundWorker) -> None:
    await worker._fill_concurrency()
    for _ in range(400):
        if not worker._inflight:
            break
        await asyncio.sleep(0.02)
    assert not worker._inflight


def _make_worker(engine: AsyncEngine, registry: JobExecutorRegistry) -> BackgroundWorker:
    return BackgroundWorker(
        engine=engine,
        session_maker=session_factory(engine),
        registry=registry,
        config=WorkerConfig(
            worker_id="w-timing-test",
            poll_interval_seconds=0.05,
            max_concurrent=2,
            lease_timeout_seconds=60,
            heartbeat_interval_seconds=10.0,
        ),
    )


class _SlowEchoExecutor:
    """纯 sleep:验证 wall-clock 进 timing,零 parquet 读。"""

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        await progress(0, 1, "slow_echo:start")
        await asyncio.sleep(0.08)
        await progress(1, 1, "slow_echo:done")
        return JobResult(status="succeeded", result_ref="slow:1")


class _ParquetEchoExecutor:
    """写一个 tmp parquet 再读回:验证 job 级读取聚合计入 timing。"""

    def __init__(self, cache_root: Path) -> None:
        self._cache = ParquetCache(str(cache_root))

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        await progress(0, 2, "pq:write")
        symbol = make_symbol("000001.SZ")
        bar = Bar(
            symbol=Symbol(code="000001.SZ", market=Market.A_SHARE),
            period=BarPeriod.D1,
            timestamp=datetime.combine(date(2026, 1, 5), datetime.min.time(), tzinfo=UTC),
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
            volume=Decimal(1000),
            amount=Decimal("10000"),
            source="issue_383_fixture",
        )
        await self._cache.write(symbol, BarPeriod.D1, "qfq", [bar])
        await progress(1, 2, "pq:read")
        bars = await self._cache.read(symbol, BarPeriod.D1, "qfq")
        assert len(bars) == 1
        return JobResult(status="succeeded", result_ref="pq:1")


class _BoomExecutor:
    """中途崩溃:验证失败兜底路径也带 timing。"""

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        await progress(0, 1, "boom:start")
        raise ExecutorError(code="boom", summary="爆炸", retryable=False)


@pytest.fixture(scope="module")
async def engine(_db_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(_db_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.execute(text("delete from background_jobs"))
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("delete from background_jobs"))


class TestJobTimingPersistence:
    async def test_success_timing_has_wall_clock_and_zero_reads(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        job_id = await _enqueue(engine, kind="slow_echo", payload={})
        registry = JobExecutorRegistry()
        registry.register("slow_echo", _SlowEchoExecutor())
        await _drain_worker(_make_worker(engine, registry))

        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.SUCCEEDED.value
        assert row.timing is not None
        timing = cast("dict[str, Any]", row.timing)
        # sleep 0.08,给 Windows 定时器粒度留余量。
        assert timing["execute_elapsed_seconds"] >= 0.05
        reads = cast("dict[str, Any]", timing["parquet_reads"])
        assert reads["read_ops"] == 0
        assert reads["read_bytes"] == 0

    async def test_parquet_reads_counted_into_timing(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        job_id = await _enqueue(engine, kind="pq_echo", payload={})
        registry = JobExecutorRegistry()
        registry.register("pq_echo", _ParquetEchoExecutor(tmp_path / "cache"))
        await _drain_worker(_make_worker(engine, registry))

        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.SUCCEEDED.value
        assert row.timing is not None
        timing = cast("dict[str, Any]", row.timing)
        reads = cast("dict[str, Any]", timing["parquet_reads"])
        # #285 计数器只计读取入口(write 不计):write 后 read 恰一次。
        assert reads["read_ops"] >= 1
        assert reads["read_bytes"] > 0
        assert reads["read_elapsed_ms"] >= 0

    async def test_failure_path_also_carries_timing(
        self, engine: AsyncEngine
    ) -> None:
        job_id = await _enqueue(engine, kind="boom", payload={})
        registry = JobExecutorRegistry()
        registry.register("boom", _BoomExecutor())
        await _drain_worker(_make_worker(engine, registry))

        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.FAILED.value
        assert row.error_code == "boom"
        assert row.timing is not None
        timing = cast("dict[str, Any]", row.timing)
        assert timing["execute_elapsed_seconds"] >= 0
        reads = cast("dict[str, Any]", timing["parquet_reads"])
        assert reads["read_ops"] == 0

    async def test_timing_shape_matches_as_dict(self, engine: AsyncEngine) -> None:
        """timing 列的 JSON 键集合稳定(前端 / MCP 消费契约)。"""

        job_id = await _enqueue(engine, kind="slow_echo", payload={})
        registry = JobExecutorRegistry()
        registry.register("slow_echo", _SlowEchoExecutor())
        await _drain_worker(_make_worker(engine, registry))

        async with session_factory(engine)() as session:
            row = await BackgroundJobRepository(session).get(job_id)
        assert row is not None
        timing = cast("dict[str, Any]", row.timing or {})
        assert set(timing.keys()) == {"execute_elapsed_seconds", "parquet_reads"}
        reads = cast("dict[str, Any]", timing["parquet_reads"])
        assert set(reads.keys()) == {
            "read_ops",
            "read_elapsed_ms",
            "read_bytes",
            "ops_by_entry",
        }
