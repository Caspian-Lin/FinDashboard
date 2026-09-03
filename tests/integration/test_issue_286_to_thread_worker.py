"""worker 心跳续约与长 CPU 段共存集成测试(issue #286)。

验证目标(issue #286 验收标准「长计算期间 heartbeat 正常续约、任务不被 reclaim」):

* CPU 密集执行段经 ``asyncio.to_thread`` 卸载后,worker 事件循环线程被解放
  —— heartbeat 协程(默认 30s,测试缩短到 50ms)在长计算段期间持续续约;
* 任务以 succeeded 终态收口、attempt == 1(无回收重排 = 无重复执行)。

误 reclaim 的真实场景是**多消费者**:执行段阻塞事件循环的 worker(进程 A,
循环被冻结,心跳无法调度)与健康的维护循环(进程 B,独立循环)并存时,
B 会在 A 的租约过期后把它回收成 interrupted —— 旧实现下重复执行 / 丢失收口。
因此对照组用独立线程 + 独立事件循环扮演「进程 B 的维护循环」(每 50ms 调
``reclaim_stale``),这正是 ``--workers N`` 多进程部署下必然并存的形态:

* 卸载(offload=True):A 的循环空闲、心跳正常续约 → B 永远看不到过期租约 →
  succeeded;
* 不卸载(offload=False):A 的循环被 2.5s 忙等冻结(lease 2.0s)→ B 在
  ~2.0s 回收 → interrupted(旧行为缺陷,固化对照)。

单进程孤立部署时维护循环与执行段同处一个被冻结的循环,反而观察不到该竞态
—— 这正是 issue #286 要求「to_thread 卸载先于 / 随 --workers N 落地」的
顺序依赖原因。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import threading
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs import JobExecutorRegistry
from finboard_backtest.background_jobs.contracts import (
    JobExecutor,
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

#: 模拟 CPU 密集段的时长(秒):必须显著大于 lease_timeout 以复现「不卸载即被回收」。
CPU_SEGMENT_SECONDS = 2.5
#: lease 时长(秒):旧实现(阻塞循环)下 2.5s 计算段必然跨过该租约。
LEASE_TIMEOUT_SECONDS = 2.0
#: 心跳间隔(秒)。
HEARTBEAT_INTERVAL_SECONDS = 0.05
# 参数选择的实测依据(Windows + GIL):to_thread 卸载后事件循环线程不再被计算
# 段占死,但多线程 GIL 争用下循环侧唤醒延迟实测可达 ~0.4s(计时器晚触发 /
# DB 回调延迟),该延迟不随 lease 等比缩小 —— lease 不能缩得太紧,否则
# 「首次续约落在租约到期之后」成为测试自身的竞态。生产配置(心跳 30s /
# lease 600s)延迟余量充裕,不受影响。


def _burn(seconds: float) -> None:
    """持 GIL 的纯 CPU 忙等。"""

    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        pass


class CpuBurnExecutor(JobExecutor):
    """执行段可切换卸载模式的 stub 执行器(#286 模式 vs 旧行为对照)。"""

    def __init__(self, *, offload: bool = True) -> None:
        self._offload = offload

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        await progress(0, 1, "cpu_burn:compute")
        if self._offload:
            await asyncio.to_thread(_burn, CPU_SEGMENT_SECONDS)
        else:
            # 旧模式对照:直接在事件循环线程阻塞(心跳被饿死)。
            _burn(CPU_SEGMENT_SECONDS)
        await progress(1, 1, "cpu_burn:done")
        return JobResult(status="succeeded")


class _ReclaimActor:
    """扮演「进程 B 的维护循环」:独立线程 + 独立事件循环周期性 reclaim_stale。"""

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _ReclaimActor:
        self._thread = threading.Thread(
            target=_run_reclaim_actor, args=(self._db_url, self._stop), daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)


def _run_reclaim_actor(db_url: str, stop: threading.Event) -> None:
    """独立循环里每 50ms 回收一次过期租约(等价 worker._recover_stale)。"""

    async def _main() -> None:
        engine = create_async_engine(db_url)
        try:
            while not stop.is_set():
                async with session_factory(engine)() as session:
                    repo = BackgroundJobRepository(session)
                    reclaimed = await repo.reclaim_stale(datetime.now(UTC))
                    if reclaimed:
                        await repo.checkpoint()
                # 轮询间隔即回收延迟上界;stop 由主线程置位后线程在下一 tick 退出。
                await asyncio.sleep(0.05)
        finally:
            await engine.dispose()

    asyncio.run(_main())


def _checksum(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


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


@pytest.fixture
def reclaim_db_url(_db_url: str) -> str:
    """给 reclaim actor 用的测试库 URL(下划线前缀参数会触发 PT019)。"""

    return _db_url


async def _enqueue_cpu_burn(engine: AsyncEngine) -> str:
    job_id = generate_background_job_id()
    payload: dict[str, object] = {"kind": "cpu_burn"}
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        await repo.create_or_get(
            job_id=job_id,
            idempotency_key=f"idem-{job_id}",
            kind="cpu_burn",
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


async def _run_one_job_to_terminal(
    engine: AsyncEngine,
    registry: JobExecutorRegistry,
) -> tuple[str, int, float]:
    """跑一个 worker 消费单个 cpu_burn 任务到终态。

    返回 ``(final_status, attempt, heartbeat_span_seconds)``。
    """

    worker = BackgroundWorker(
        engine=engine,
        session_maker=session_factory(engine),
        registry=registry,
        config=WorkerConfig(
            worker_id="w-286-test",
            poll_interval_seconds=0.05,
            max_concurrent=1,
            lease_timeout_seconds=LEASE_TIMEOUT_SECONDS,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            maintenance_interval_seconds=0.1,
            retry_backoff_seconds=0.2,
        ),
    )
    rows = await _read_background_job_ids(engine)
    assert len(rows) == 1
    job_id = rows[0]
    worker_task = asyncio.create_task(worker.run())
    try:
        final_status: str | None = None
        started_at: datetime | None = None
        heartbeat_at: datetime | None = None
        attempt: int | None = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            async with session_factory(engine)() as session:
                repo = BackgroundJobRepository(session)
                row = await repo.get(job_id)
            if row is not None and row.status in {
                BackgroundJobStatus.SUCCEEDED.value,
                BackgroundJobStatus.FAILED.value,
                BackgroundJobStatus.CANCELLED.value,
                BackgroundJobStatus.INTERRUPTED.value,
            }:
                final_status = row.status
                started_at = row.started_at
                heartbeat_at = row.heartbeat_at
                attempt = row.attempt
                break
            await asyncio.sleep(0.02)

        assert final_status is not None, "30s 内任务未到达终态"
        assert attempt is not None
        span = (
            (heartbeat_at - started_at).total_seconds()
            if started_at is not None and heartbeat_at is not None
            else 0.0
        )
        return final_status, attempt, span
    finally:
        worker.request_stop()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(worker_task, timeout=10)


async def _read_background_job_ids(engine: AsyncEngine) -> list[str]:
    async with engine.begin() as conn:
        result = await conn.execute(text("select job_id from background_jobs"))
        return [str(row[0]) for row in result]


class TestLeaseRenewalDuringCpuSegment:
    async def test_heartbeat_renews_and_job_not_reclaimed(
        self, engine: AsyncEngine, reclaim_db_url: str
    ) -> None:
        """长 CPU 段(to_thread 卸载)期间心跳持续续约,任务不被外部维护循环回收。"""

        await _enqueue_cpu_burn(engine)
        registry = JobExecutorRegistry()
        registry.register("cpu_burn", CpuBurnExecutor(offload=True))
        with _ReclaimActor(reclaim_db_url):
            status, attempt, span = await _run_one_job_to_terminal(engine, registry)

        assert status == BackgroundJobStatus.SUCCEEDED.value, (
            "长 CPU 段期间任务被回收/失败 —— 心跳续约未生效"
        )
        assert attempt == 1, "任务被 reclaim 后重排会导致 attempt > 1"
        assert span >= CPU_SEGMENT_SECONDS * 0.5, (
            f"最后一次心跳距开始仅 {span:.2f}s,计算段({CPU_SEGMENT_SECONDS}s)"
            "期间心跳被饿死"
        )

    async def test_blocking_executor_gets_reclaimed_by_peer_maintenance(
        self, engine: AsyncEngine, reclaim_db_url: str
    ) -> None:
        """负面对照:不卸载(阻塞循环)时,外部维护循环必然回收成 interrupted。

        锁定测试灵敏度与旧行为缺陷:worker 自身循环被 2.5s 忙等冻结、心跳无法
        调度,lease(2.0s)过期后由独立循环的维护方(多进程部署下的进程 B)
        回收 —— succeeded 只有在「卸载 → 心跳可调度」时才可能成立。
        """

        await _enqueue_cpu_burn(engine)
        registry = JobExecutorRegistry()
        registry.register("cpu_burn", CpuBurnExecutor(offload=False))
        with _ReclaimActor(reclaim_db_url):
            status, attempt, _span = await _run_one_job_to_terminal(engine, registry)

        assert status == BackgroundJobStatus.INTERRUPTED.value, (
            "阻塞执行段应当跨过 lease 被外部维护循环回收(旧行为),实际终态: "
            f"{status}"
        )
        assert attempt == 1
