"""统一后台任务队列:5k 标的批量任务期间 API 响应性基准(issue #117)。

验证目标(issue #117 验收标准"5k 标的批量任务期间 API 健康接口响应性基准测试"):

Worker 在执行一个 5000 标的的 bulk_download 任务(每个标的 yield 一次事件循环,
模拟真实 I/O)期间,API 进程的 ``GET /api/health`` 与 ``GET /api/jobs/{job_id}``
两个只读端点的响应延迟必须保持在低基线(p95 < 阈值),证明:

* API 与 Worker 解耦 —— Worker 执行耗时任务不会饿死 API 事件循环;
* 任务状态查询走持久化表,不依赖 Python 内存字典。

实现要点:
* Worker 与 FastAPI app 共用同一 asyncio 事件循环(同进程),Worker 把每个任务
  作为独立 ``asyncio.Task`` 运行(``asyncio.create_task``),执行器内部用
  ``asyncio.sleep`` 让出循环 —— 这是能复现"Worker 占用循环 vs API 探针排队"
  的最严格耦合条件。
* 用 ``httpx.AsyncClient`` + ``ASGITransport`` 直接驱动 ``create_app()`` 产出的
  app,不监听真实端口。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_api.app import create_app
from finboard_app.config import Settings
from finboard_backtest.background_jobs import JobExecutorRegistry
from finboard_backtest.background_jobs.contracts import (
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.background_jobs.worker import BackgroundWorker, WorkerConfig
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
from finboard_shared.types import BrokerKind
from tests.integration.conftest import TEST_DB_URL as DB_URL

#: 模拟批量拉取的标的数量(issue 要求的 5k 量级)。
SYMBOL_COUNT = 5000
#: 每标的让出事件循环的时间(秒);5000 * 0.001 = ~5s 批量任务,足以采样 API 延迟。
PER_SYMBOL_SLEEP = 0.001
#: API 响应延迟 p95 阈值(秒)。Worker 在跑 5k 任务,API 只读 DB,应远低于此。
P95_LATENCY_BUDGET_SECONDS = 1.0
#: 采样次数。
SAMPLES = 20


def _checksum(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class _SlowBulkDownloadExecutor:
    """模拟 5000 标的的 bulk_download 执行器。

    每标的 ``await asyncio.sleep`` 让出循环一次,模拟真实行情 I/O 的协程化等待
    (而非阻塞 sleep)。这正是 #117 想验证的:Worker 在占用事件循环执行长任务时,
    API 的只读探针能否保持低延迟。
    """

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        raw_total = job.payload.get("symbol_count", SYMBOL_COUNT)
        total = int(raw_total) if isinstance(raw_total, int) else SYMBOL_COUNT
        await progress(0, total, "fetching")
        for i in range(total):
            await asyncio.sleep(PER_SYMBOL_SLEEP)
            # 每 500 标的上报一次进度(回调内部会续约心跳 + 检测取消)。
            if (i + 1) % 500 == 0:
                await progress(i + 1, total, "fetching")
        await progress(total, total, "done")
        return JobResult(status="succeeded", result_ref=None, progress_total=total)


@pytest_asyncio.fixture(scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    from tests.integration.conftest import ensure_test_db

    await ensure_test_db(DB_URL)
    eng = create_async_engine(DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.execute(text("delete from background_jobs"))
    await eng.dispose()


def _session_maker(engine: AsyncEngine) -> object:
    return session_factory(engine)


async def _enqueue(engine: AsyncEngine, *, kind: str, payload: dict[str, object]) -> str:
    job_id = generate_background_job_id()
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        await repo.create_or_get(
            job_id=job_id,
            idempotency_key=f"idem-{job_id}",
            kind=kind,
            queue="data",
            status=BackgroundJobStatus.QUEUED.value,
            priority=0,
            payload=payload,
            payload_checksum=_checksum(payload),
            max_attempts=1,
            requested_by="benchmark",
        )
        await repo.checkpoint()
    return job_id


def _build_worker(engine: AsyncEngine) -> BackgroundWorker:
    registry = JobExecutorRegistry()
    # 用 _SlowBulkDownloadExecutor 替换真实 bulk_download 执行器(不联网)。
    registry.register("bulk_download", _SlowBulkDownloadExecutor())
    config = WorkerConfig(
        worker_id="bench-worker",
        poll_interval_seconds=0.05,
        max_concurrent=1,
        lease_timeout_seconds=60,
        heartbeat_interval_seconds=1,
        queues=None,
        kind_concurrency={"bulk_download": 1},
    )
    return BackgroundWorker(
        engine=engine,
        session_maker=_session_maker(engine),  # type: ignore[arg-type]
        registry=registry,
        config=config,
    )


@pytest.mark.integration
async def test_api_stays_responsive_during_5k_bulk_download(engine: AsyncEngine) -> None:
    """5k 标的批量任务执行期间,/api/health 与 /api/jobs/{id} 的 p95 延迟低于阈值。"""

    # 1) 起 FastAPI app(MockBroker + 同一 PG),ASGITransport 进程内驱动。
    settings = Settings(
        broker=BrokerKind.MOCK,
        account_id="bench-account",
        db_url=DB_URL,
        risk_allow_market_order=True,
    )
    app = create_app(settings)

    # 2) 入队一个 5k 标的的 bulk_download 任务。
    job_id = await _enqueue(
        engine,
        kind="bulk_download",
        payload={"market": "a_share", "symbol_count": SYMBOL_COUNT},
    )

    worker = _build_worker(engine)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 3) 后台启动 Worker 主循环(它会 claim 任务并跑 _SlowBulkDownloadExecutor)。
            worker_task = asyncio.create_task(worker.run())
            try:
                # 等 Worker 把任务领走置为 running。每轮开新 session,避免
                # 同一 session 的 identity map 缓存旧 queued 行导致看不到状态推进。
                running_started = False
                for _ in range(200):
                    async with session_factory(engine)() as session:
                        repo = BackgroundJobRepository(session)
                        row = await repo.get(job_id)
                    if row and row.status == BackgroundJobStatus.RUNNING.value:
                        running_started = True
                        break
                    await asyncio.sleep(0.02)
                assert running_started, "Worker 未能在限定时间内领取并启动任务"

                # 4) Worker 正在跑 5k 任务期间,采样 API 只读端点延迟。
                health_latencies: list[float] = []
                job_latencies: list[float] = []
                for _ in range(SAMPLES):
                    t0 = time.perf_counter()
                    resp = await client.get("/api/health")
                    health_latencies.append(time.perf_counter() - t0)
                    assert resp.status_code == 200

                    t0 = time.perf_counter()
                    resp = await client.get(f"/api/jobs/{job_id}")
                    job_latencies.append(time.perf_counter() - t0)
                    assert resp.status_code == 200
                    # 任务查询应反映 running,不应被 Worker 阻塞到终态才返回。
                    assert resp.json()["status"] in {
                        BackgroundJobStatus.RUNNING.value,
                        BackgroundJobStatus.SUCCEEDED.value,
                    }

                # 5) 断言 p95 延迟低于预算。
                health_p95 = _percentile(health_latencies, 95)
                job_p95 = _percentile(job_latencies, 95)
                assert health_p95 < P95_LATENCY_BUDGET_SECONDS, (
                    f"/api/health p95={health_p95:.3f}s 超预算 "
                    f"{P95_LATENCY_BUDGET_SECONDS}s(Worker 跑 {SYMBOL_COUNT} 标的时)"
                )
                assert job_p95 < P95_LATENCY_BUDGET_SECONDS, (
                    f"/api/jobs/{{id}} p95={job_p95:.3f}s 超预算 "
                    f"{P95_LATENCY_BUDGET_SECONDS}s(Worker 跑 {SYMBOL_COUNT} 标的时)"
                )
            finally:
                # 6) 收尾:停 Worker,等 5k 任务跑完或超时取消。
                worker._stop_event.set()
                try:
                    await asyncio.wait_for(worker_task, timeout=60)
                except TimeoutError:
                    worker_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await worker_task


def _percentile(samples: list[float], pct: float) -> float:
    """简单百分位计算(线性插值)。"""

    if not samples:
        return float("inf")
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac
