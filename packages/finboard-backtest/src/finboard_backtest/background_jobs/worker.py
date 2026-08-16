"""后台任务 worker 进程主循环(issue #117 / #142)。

独立进程(``finboard worker run``)从 PostgreSQL ``background_jobs`` 队列用
``FOR UPDATE SKIP LOCKED`` 领取 queued 任务,按 ``kind`` 分发到执行器,
执行期间周期性续约心跳并在每个 checkpoint 重读任务状态以支持协作式取消。

session 隔离红线:**每个 job 用一个独立 session**,绝不复用 kernel / 请求 session,
避免一个任务的长事务阻塞另一个任务 / 污染 kernel 交易域。

边界:不连 broker / 账户 / 订单 / 持仓 / Kill Switch;payload 不含敏感凭据。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
)
from finboard_backtest.background_jobs.registry import (
    JobExecutorRegistry,
    UnknownJobKindError,
)
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)
from finboard_shared.background_jobs import BackgroundJobStatus

logger = logging.getLogger(__name__)


@dataclass
class WorkerConfig:
    """worker 运行参数。"""

    worker_id: str
    poll_interval_seconds: float
    max_concurrent: int
    lease_timeout_seconds: float
    heartbeat_interval_seconds: float
    queues: Sequence[str] | None = None
    #: 按 ``kind`` 限制全局并发(issue #144)。key=kind, value=最大同时 running 数。
    #: 仅对列出的 kind 生效;未列出的 kind 不限制。空 / None 表示不限制。
    kind_concurrency: Mapping[str, int] | None = None
    #: 周期性维护(回收过期租约 + 重试任务自动重排)的间隔(issue #161)。
    maintenance_interval_seconds: float = 10.0
    #: retry_waiting / interrupted 自动重排前必须等待的退避秒数。
    retry_backoff_seconds: float = 30.0


def default_worker_id() -> str:
    return f"worker-{uuid.uuid4().hex[:8]}"


class BackgroundWorker:
    """worker 主循环;构造后 :func:`run` 直到收到停止信号。"""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        session_maker: async_sessionmaker[AsyncSession],
        registry: JobExecutorRegistry,
        config: WorkerConfig,
    ) -> None:
        self._engine = engine
        self._session_maker = session_maker
        self._registry = registry
        self._config = config
        self._stop_event = asyncio.Event()
        self._inflight: set[asyncio.Task[None]] = set()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        logger.info(
            "background_worker.start worker_id=%s queues=%s max_concurrent=%d",
            self._config.worker_id,
            list(self._config.queues) if self._config.queues else "all",
            self._config.max_concurrent,
        )
        await self._recover_stale()
        next_maintenance = asyncio.get_running_loop().time() + (
            self._config.maintenance_interval_seconds
        )
        while not self._stop_event.is_set():
            await self._fill_concurrency()
            now = asyncio.get_running_loop().time()
            if now >= next_maintenance:
                await self._maintenance()
                next_maintenance = now + self._config.maintenance_interval_seconds
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._config.poll_interval_seconds,
                )
        await self._drain()
        logger.info("background_worker.stop worker_id=%s", self._config.worker_id)

    # ------------------------------------------------------------- 内部流程
    async def _recover_stale(self) -> None:
        """回收 lease 已过期的 running / cancel_requested 任务(interrupted)。

        启动时与周期性维护都会调用 —— 长期存活的 worker 不再依赖崩溃后重启
        才回收,僵尸 running 不会永久占用 per-kind 并发额度(issue #161)。
        """

        async with self._session_maker() as session:
            repo = BackgroundJobRepository(session)
            reclaimed = await repo.reclaim_stale(datetime.now(UTC))
            if reclaimed:
                await repo.checkpoint()
                logger.info("background_worker.reclaim count=%d", len(reclaimed))

    async def _maintenance(self) -> None:
        """周期性维护:回收过期租约 + 自动重排进入重试态的任务(issue #161)。"""

        await self._recover_stale()
        async with self._session_maker() as session:
            repo = BackgroundJobRepository(session)
            requeued, exhausted = await repo.requeue_due(
                datetime.now(UTC),
                backoff_seconds=self._config.retry_backoff_seconds,
            )
            if requeued or exhausted:
                await repo.checkpoint()
                logger.info(
                    "background_worker.requeue requeued=%d exhausted=%d",
                    len(requeued),
                    len(exhausted),
                )

    async def _fill_concurrency(self) -> None:
        """把空闲的并发槽填满:领取任务并为每个任务起一个独立 task。"""

        free = self._config.max_concurrent - len(self._inflight)
        if free <= 0:
            return
        async with self._session_maker() as session:
            repo = BackgroundJobRepository(session)
            rows = await repo.claim_next(
                worker_id=self._config.worker_id,
                lease_until=datetime.now(UTC)
                + timedelta(seconds=self._config.lease_timeout_seconds),
                queues=self._config.queues,
                limit=free,
                max_per_kind=self._config.kind_concurrency,
            )
            if rows:
                await repo.checkpoint()
        for row in rows:
            task = asyncio.create_task(self._run_one(row.job_id), name=f"job:{row.job_id}")
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _run_one(self, job_id: str) -> None:
        """单个任务的完整生命周期:取 executor → 执行 → 收口。每 job 一个 session。

        关键:任何 ``_finalize`` / ``_execute_with_heart`` 调用都**必须在外层
        领取 session 关闭之后**进行 —— 否则外层 ``get(for_update=True)`` 持有的
        行锁会与 finalize 内部再次 ``get(for_update=True)`` 互相等待,造成死锁。
        """

        try:
            early_result: JobResult | None = None
            record: JobRecord | None = None
            executor: JobExecutor | None = None
            async with self._session_maker() as session:
                repo = BackgroundJobRepository(session)
                row = await repo.get(job_id, for_update=True)
                if row is None or row.status != BackgroundJobStatus.RUNNING.value:
                    # 在排队后被取消 / 被其他 worker 抢走 —— 直接放弃。
                    return
                if row.status == BackgroundJobStatus.CANCEL_REQUESTED.value:
                    early_result = JobResult(status=BackgroundJobStatus.CANCELLED.value)
                else:
                    try:
                        executor = self._registry.get(row.kind)
                    except UnknownJobKindError:
                        early_result = JobResult(
                            status=BackgroundJobStatus.FAILED.value,
                            error_code="unknown_kind",
                            error_summary=f"无注册执行器: {row.kind}",
                        )
                    else:
                        record = JobRecord(
                            job_id=row.job_id,
                            kind=row.kind,
                            queue=row.queue,
                            payload=dict(row.payload),
                            attempt=row.attempt,
                            max_attempts=row.max_attempts,
                            requested_by=row.requested_by,
                            progress_total=row.progress_total,
                            progress_done=row.progress_done,
                            phase=row.phase,
                        )
            # 外层领取 session 已关闭,行锁释放 —— 此后才能开新 session 收口。
            if early_result is not None:
                await self._finalize(job_id, early_result)
                return
            assert executor is not None
            assert record is not None
            result = await self._execute_with_heart(job_id, executor, record)
            await self._finalize(job_id, result)
        except asyncio.CancelledError:
            # 进程关闭时取消 in-flight task:把任务留在 running,由 lease 过期回收。
            raise
        except Exception as exc:
            # worker 不得让单任务崩溃带垮主循环 —— 兜底为 failed/retry_waiting。
            logger.exception("background_worker.job_crash job_id=%s", job_id)
            status = (
                BackgroundJobStatus.RETRY_WAITING.value
                if isinstance(exc, ExecutorError) and exc.retryable
                else BackgroundJobStatus.FAILED.value
            )
            await self._finalize(
                job_id,
                JobResult(
                    status=status,
                    error_code=getattr(exc, "code", type(exc).__name__),
                    # ExecutorError 是 dataclass,str() 为空 —— 优先取 .summary,
                    # 保证失败原因写入任务行(grid 聚合可见)。
                    error_summary=(getattr(exc, "summary", None) or str(exc) or type(exc).__name__)[
                        :1000
                    ],
                ),
            )

    async def _execute_with_heart(
        self,
        job_id: str,
        executor: JobExecutor,
        record: JobRecord,
    ) -> JobResult:
        """跑 executor,后台心跳 + 协作式取消检测。"""

        cancel_state = {"cancelled": False}

        async def progress(done: int, total: int | None, phase: str | None) -> None:
            async with self._session_maker() as session:
                repo = BackgroundJobRepository(session)
                row = await repo.get(job_id, for_update=True)
                if row is None:
                    return
                if row.status == BackgroundJobStatus.CANCEL_REQUESTED.value:
                    cancel_state["cancelled"] = True
                    raise asyncio.CancelledError()
                await repo.update_progress(job_id, done=done, total=total, phase=phase)
                await repo.checkpoint()

        async def heartbeat() -> None:
            while not cancel_state["cancelled"]:
                await asyncio.sleep(self._config.heartbeat_interval_seconds)
                async with self._session_maker() as session:
                    repo = BackgroundJobRepository(session)
                    row = await repo.get(job_id, for_update=True)
                    if row is None:
                        return
                    if row.status == BackgroundJobStatus.CANCEL_REQUESTED.value:
                        cancel_state["cancelled"] = True
                        return
                    await repo.update_heartbeat(
                        job_id,
                        worker_id=self._config.worker_id,
                        lease_until=datetime.now(UTC)
                        + timedelta(seconds=self._config.lease_timeout_seconds),
                    )
                    await repo.checkpoint()

        heart = asyncio.create_task(heartbeat(), name=f"heart:{job_id}")
        result: JobResult | None = None
        try:
            result = await executor.execute(record, progress)
        finally:
            heart.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heart
        if cancel_state["cancelled"]:
            return JobResult(status=BackgroundJobStatus.CANCELLED.value)
        assert result is not None  # executor.execute 正常返回即非 None
        return result

    async def _finalize(self, job_id: str, result: JobResult) -> None:
        """把执行结果落库(running/cancel_requested → succeeded/failed/cancelled)。"""

        async with self._session_maker() as session:
            repo = BackgroundJobRepository(session)
            try:
                await repo.finish(
                    job_id,
                    target=result.status,
                    result_ref=result.result_ref,
                    error_code=result.error_code,
                    error_summary=result.error_summary,
                )
                await repo.checkpoint()
            except BackgroundJobPersistenceConflictError as exc:
                # 状态已被取消 / 回收等流转改变 —— 不强行覆盖,保留真实终态。
                logger.warning(
                    "background_worker.finalize_skipped job_id=%s reason=%s",
                    job_id,
                    exc,
                )

    async def _drain(self) -> None:
        """关闭流程:取消 in-flight tasks 并等待它们落库后退出。"""

        for task in list(self._inflight):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        self._inflight.clear()


async def run_worker(
    *,
    engine: AsyncEngine,
    session_maker: async_sessionmaker[AsyncSession],
    registry: JobExecutorRegistry,
    config: WorkerConfig,
) -> None:
    """便捷入口:注册信号处理并阻塞运行直到 SIGINT/SIGTERM。"""

    worker = BackgroundWorker(
        engine=engine, session_maker=session_maker, registry=registry, config=config
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_stop)
    try:
        await worker.run()
    finally:
        await engine.dispose()


__all__ = [
    "BackgroundWorker",
    "WorkerConfig",
    "default_worker_id",
    "run_worker",
]
