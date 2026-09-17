"""issue #471 集成测试:worker 心跳线程侧停滞看门狗(活性防线第三层)。

覆盖验收标准:

* 挂死 job(不调 progress、execute 永不返回)→ 小阈值看门狗在有界时间内
  取消执行任务,job 具名收敛 ``retry_waiting``(``error_code=stall_watchdog``,
  摘要携带 #471 挂死签名),in-flight 不残留;
* 对照:持续上报 progress 的健康 job(阈值同样紧张)不被误杀;
* ``stall_timeout_seconds=0`` 关闭看门狗(挂死 job 保持 running,交 #306
  DB 侧兜底);
* PG 方言门控:真实 postgres 会话 ``SET statement_timeout`` 往返生效
  (P1.2 服务端界,sqlite 单元侧已验证跳过)。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。不连 broker / 不下实盘单。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs import JobExecutorRegistry
from finboard_backtest.background_jobs.contracts import (
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.background_jobs.executors.research_run import (
    DEFAULT_RESEARCH_STORE_OPERATION_TIMEOUT_SECONDS,
    _postgres_statement_timeout_ms,
)
from finboard_backtest.background_jobs.worker import BackgroundWorker, WorkerConfig
from finboard_persistence import (
    BackgroundJobRepository,
    create_async_engine,
    session_factory,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    from finboard_persistence import Base
    from tests.integration.conftest import TEST_DB_URL, ensure_test_db

    await ensure_test_db(TEST_DB_URL)
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM background_jobs"))


# ---- 执行器桩 ------------------------------------------------------------------


class _SilentStallExecutor:
    """#471 挂死签名:从不调 progress,execute 永不返回(await 卡死)。"""

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        del progress
        await asyncio.sleep(300)
        return JobResult(status="succeeded")  # pragma: no cover


class _ProgressingExecutor:
    """健康执行器:执行期持续上报 progress(模拟逐 stage / 逐决策帧)。"""

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        steps = 8
        for index in range(steps):
            await asyncio.sleep(0.1)
            await progress(index, steps, f"stepping:{index}")
        await progress(steps, steps, "stepping:done")
        return JobResult(status="succeeded", result_ref="stepped")


def _build_worker(
    engine: AsyncEngine,
    *,
    kind: str,
    executor: object,
    stall_timeout_seconds: float,
    worker_id: str,
) -> BackgroundWorker:
    registry = JobExecutorRegistry()
    registry.register(kind, executor)  # type: ignore[arg-type]
    return BackgroundWorker(
        engine=engine,
        session_maker=session_factory(engine),
        registry=registry,
        config=WorkerConfig(
            worker_id=worker_id,
            poll_interval_seconds=0.05,
            max_concurrent=2,
            lease_timeout_seconds=60.0,
            heartbeat_interval_seconds=0.5,
            stall_timeout_seconds=stall_timeout_seconds,
        ),
    )


async def _enqueue(engine: AsyncEngine, *, kind: str) -> str:
    payload: dict[str, object] = {"n": 1}
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        job_id = generate_background_job_id()
        checksum = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        await repo.create_or_get(
            job_id=job_id,
            idempotency_key=f"idem-{job_id}",
            kind=kind,
            queue="default",
            status=BackgroundJobStatus.QUEUED.value,
            priority=0,
            payload=payload,
            payload_checksum=checksum,
            max_attempts=3,
            requested_by="issue-471-test",
        )
        await repo.checkpoint()
        return job_id


async def _get_job(engine: AsyncEngine, job_id: str):
    async with session_factory(engine)() as session:
        return await BackgroundJobRepository(session).get(job_id)


async def _wait_until(predicate, *, timeout_seconds: float, message: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"等待超时: {message}")


async def _drain_inflight(worker: BackgroundWorker) -> None:
    for _ in range(300):
        if not worker._inflight:
            return
        await asyncio.sleep(0.02)


# ---- 看门狗全链路 --------------------------------------------------------------


class TestStallWatchdogWorkerPath:
    async def test_silent_job_killed_named_and_no_residue(self, engine: AsyncEngine) -> None:
        """挂死 job:看门狗秒级取消 → retry_waiting + stall_watchdog 具名摘要。"""

        job_id = await _enqueue(engine, kind="stall_kind_471")
        worker = _build_worker(
            engine,
            kind="stall_kind_471",
            executor=_SilentStallExecutor(),
            stall_timeout_seconds=0.5,
            worker_id="w-471-stall",
        )
        await worker._fill_concurrency()
        await _wait_until(
            lambda: _job_is(engine, job_id, BackgroundJobStatus.RUNNING.value),
            timeout_seconds=5.0,
            message="任务未被领取",
        )

        # 看门狗在 ~0.5s + 轮询窗口内击杀并收口(retry_waiting = interrupted
        # 家族的重试态,requeue_due 与 #306 僵尸击杀同收敛路径)。
        await _wait_until(
            lambda: _job_is(engine, job_id, BackgroundJobStatus.RETRY_WAITING.value),
            timeout_seconds=10.0,
            message="挂死任务未被停滞看门狗收敛",
        )
        row = await _get_job(engine, job_id)
        assert row is not None
        assert row.error_code == "stall_watchdog"
        assert row.error_summary is not None
        assert "progress" in row.error_summary
        assert "471" in row.error_summary

        # in-flight 不残留:执行任务已收口,worker 可继续领下一个 job。
        await _drain_inflight(worker)
        assert not worker._inflight

        # 原属主心跳停止续租(与 #306 击杀后同语义):lease 不再被推远。
        lease_frozen = row.lease_until
        assert lease_frozen is not None
        await asyncio.sleep(0.8)
        row = await _get_job(engine, job_id)
        assert row is not None
        assert row.lease_until == lease_frozen

    async def test_progressing_job_not_killed(self, engine: AsyncEngine) -> None:
        """对照:持续上报 progress 的健康 job(阈值同样紧张)不误杀。"""

        job_id = await _enqueue(engine, kind="stepping_kind_471")
        worker = _build_worker(
            engine,
            kind="stepping_kind_471",
            executor=_ProgressingExecutor(),
            stall_timeout_seconds=0.5,
            worker_id="w-471-step",
        )
        await worker._fill_concurrency()
        await _wait_until(
            lambda: _job_is(engine, job_id, BackgroundJobStatus.SUCCEEDED.value),
            timeout_seconds=10.0,
            message="健康任务被停滞看门狗误杀",
        )
        row = await _get_job(engine, job_id)
        assert row is not None
        assert row.error_code is None
        assert not worker._inflight

    async def test_disabled_when_threshold_zero(self, engine: AsyncEngine) -> None:
        """stall_timeout_seconds=0:看门狗关闭,挂死 job 保持 running(#306 兜底)。"""

        job_id = await _enqueue(engine, kind="stall_kind_471_off")
        worker = _build_worker(
            engine,
            kind="stall_kind_471_off",
            executor=_SilentStallExecutor(),
            stall_timeout_seconds=0.0,
            worker_id="w-471-off",
        )
        await worker._fill_concurrency()
        await _wait_until(
            lambda: _job_is(engine, job_id, BackgroundJobStatus.RUNNING.value),
            timeout_seconds=5.0,
            message="任务未被领取",
        )
        await asyncio.sleep(1.2)  # 远超本用例若开启时的阈值
        row = await _get_job(engine, job_id)
        assert row is not None
        assert row.status == BackgroundJobStatus.RUNNING.value

        # 收尾:取消 in-flight(不覆盖状态),避免悬挂到测试进程结束。
        for task in list(worker._inflight):
            task.cancel()
        await asyncio.gather(*worker._inflight, return_exceptions=True)

    async def test_sequential_jobs_no_watchdog_state_pollution(self, engine: AsyncEngine) -> None:
        """同一 worker_id 顺序执行「挂死 → 健康」两 job:看门狗状态不互相污染。"""

        worker = _build_worker(
            engine,
            kind="stall_kind_471",
            executor=_SilentStallExecutor(),
            stall_timeout_seconds=0.5,
            worker_id="w-471-seq",
        )
        first = await _enqueue(engine, kind="stall_kind_471")
        await worker._fill_concurrency()
        await _wait_until(
            lambda: _job_is(engine, first, BackgroundJobStatus.RETRY_WAITING.value),
            timeout_seconds=10.0,
            message="第一个(挂死)任务未被收敛",
        )
        await _drain_inflight(worker)

        # 第二个 job 同 kind 换健康执行器:重试用同一 worker_id 顺序执行,
        # 前一看门狗线程已随 job 终态停表,不影响后一 job 的活性判定。
        healthy = _build_worker(
            engine,
            kind="stall_kind_471",
            executor=_ProgressingExecutor(),
            stall_timeout_seconds=0.5,
            worker_id="w-471-seq",
        )
        second = await _enqueue(engine, kind="stall_kind_471")
        await healthy._fill_concurrency()
        await _wait_until(
            lambda: _job_is(engine, second, BackgroundJobStatus.SUCCEEDED.value),
            timeout_seconds=10.0,
            message="第二个(健康)任务被前序看门狗残留误杀",
        )


async def _job_is(engine: AsyncEngine, job_id: str, status: str) -> bool:
    row = await _get_job(engine, job_id)
    return row is not None and row.status == status


# ---- P1.2 服务端界:PG 方言门控集成侧 -------------------------------------------


class TestPostgresStatementTimeout:
    async def test_pg_session_gets_ms_value_and_set_roundtrip(self, engine: AsyncEngine) -> None:
        """真实 postgres 会话:门控返回毫秒值,SET 后 pg_settings 读回一致。"""

        async with session_factory(engine)() as session:
            timeout_ms = _postgres_statement_timeout_ms(
                session, DEFAULT_RESEARCH_STORE_OPERATION_TIMEOUT_SECONDS
            )
            assert timeout_ms == 120_000
            await session.execute(text(f"SET statement_timeout = {timeout_ms}"))
            # pg_settings 恒以毫秒字符串报告(SHOW 会渲染成 "2min" 等人读格式)。
            shown = (
                await session.execute(
                    text("SELECT setting FROM pg_settings WHERE name = 'statement_timeout'")
                )
            ).scalar_one()
            assert cast("str", shown) == "120000"
