"""后台任务 worker 进程主循环(issue #117 / #142)。

独立进程(``finboard worker run``)从 PostgreSQL ``background_jobs`` 队列用
``FOR UPDATE SKIP LOCKED`` 领取任务,按 ``kind`` 分发到执行器,
执行期间周期性续约心跳并在每个 checkpoint 重读任务状态以支持协作式取消。

停机语义(issue #307):第一次停止信号(POSIX SIGINT/SIGTERM,Windows
Ctrl-C / CTRL_BREAK)停止领新任务并收尾 in-flight —— ``shutdown_grace_seconds``
为 0 立即取消(现状语义),>0 先等任务完成至宽限上限再取消;等待中第二次
停止信号立即强退(退出码 130)。优雅退出码 0,未完成任务统一由 lease 过期
回收 → interrupted → 退避重排兜底。

session 隔离红线:**每个 job 用一个独立 session**,绝不复用 kernel / 请求 session,
避免一个任务的长事务阻塞另一个任务 / 污染 kernel 交易域。

边界:不连 broker / 账户 / 订单 / 持仓 / Kill Switch;payload 不含敏感凭据。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
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
    truncate_summary,
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

#: 第二次停止信号(优雅宽限等待中)强退时的进程退出码(128 + SIGINT = 130,
#: 约定俗成;正常优雅退出为 0,issue #307 退出码约定)。
WORKER_FORCE_EXIT_CODE = 130

#: grace>0 的 drain 等待轮询粒度(秒):Windows 上 ``signal.signal`` 注册的
#: 处理器只在主线程从 select 返回后的字节码边界执行 —— 等待按短窗口分片,
#: 保证第二次停止信号的反应延迟有界(≤ ~0.4s),而不是被整个宽限窗口卡住。
_DRAIN_POLL_SECONDS = 0.2


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
    #: 停机优雅宽限秒数(issue #307)。0(默认)= 收到停止信号立即取消
    #: in-flight(现状语义,任务留给 lease 过期回收);>0 = 先等 in-flight
    #: 自然完成至该上限、超时才取消(兜底语义与 0 相同)。等待中第二次停止
    #: 信号立即强退(退出码 :data:`WORKER_FORCE_EXIT_CODE`)。与 settings
    #: ``worker_shutdown_grace_seconds`` 对齐,``finboard dev`` /
    #: ``--workers N`` supervisor 的子进程收敛宽限共用同一值。
    shutdown_grace_seconds: float = 0.0


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
        self._force_stop_event = asyncio.Event()
        self._inflight: set[asyncio.Task[None]] = set()

    @property
    def stop_requested(self) -> bool:
        """是否已收到(第一次)停止信号。"""
        return self._stop_event.is_set()

    @property
    def force_stop_requested(self) -> bool:
        """是否已收到第二次停止信号(立即强退)。"""
        return self._force_stop_event.is_set()

    def request_stop(self) -> None:
        self._stop_event.set()

    def request_force_stop(self) -> None:
        """第二次停止信号:跳过剩余优雅宽限,立即强退(issue #307)。

        in-flight 任务照旧取消、留给 lease 过期回收(兜底语义不变);区别
        仅在于 ``run`` 返回 True,由 ``run_worker`` 以非 0 退出码结束进程。
        """
        self._stop_event.set()
        self._force_stop_event.set()

    async def run(self) -> bool:
        """主循环:领取任务直到停止信号,收尾后返回是否因第二次信号强退。"""
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
                # 维护是尽力而为:单次失败(如瞬时连接池耗尽)只记日志并顺延
                # 到下一周期,绝不让维护异常杀死整个 worker 进程(issue #212
                # 实测 QueuePool TimeoutError 曾把维护循环连同 worker 一起掀翻)。
                try:
                    await self._maintenance()
                except TimeoutError:
                    logger.warning("background_worker.maintenance_timeout")
                except Exception:
                    logger.exception("background_worker.maintenance_failed")
                next_maintenance = now + self._config.maintenance_interval_seconds
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._config.poll_interval_seconds,
                )
        force = await self._drain()
        logger.info(
            "background_worker.stop worker_id=%s force=%s",
            self._config.worker_id,
            force,
        )
        return force

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
                    # 保证失败原因写入任务行(grid 聚合可见)。截断保头保尾
                    # (issue #263):头部 stage/决策日上下文与尾部根因均保留。
                    error_summary=truncate_summary(
                        getattr(exc, "summary", None) or str(exc) or type(exc).__name__
                    ),
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

    async def _drain(self) -> bool:
        """关闭流程:按 ``shutdown_grace_seconds`` 收尾 in-flight tasks(#307)。

        * grace = 0(默认):立即取消全部 in-flight —— 任务留在 running,由
          lease 过期回收 → interrupted → 退避重排(现状语义,行为零回归);
        * grace > 0:先等 in-flight 自然完成至宽限上限;超时才取消剩余任务
          (兜底语义与 grace=0 相同);
        * 等待期间收到第二次停止信号(或进入 drain 前已收到):立即取消剩余
          任务并返回 True —— ``run_worker`` 据此以 :data:`WORKER_FORCE_EXIT_CODE`
          强退。
        """

        inflight = [task for task in self._inflight if not task.done()]
        if not inflight:
            self._inflight.clear()
            return False
        grace = self._config.shutdown_grace_seconds
        if grace <= 0 and not self._force_stop_event.is_set():
            for task in inflight:
                task.cancel()
            await asyncio.gather(*inflight, return_exceptions=True)
            self._inflight.clear()
            return False

        pending: set[asyncio.Task[None]] = set(inflight)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace if grace > 0 else 0.0
        while pending and not self._force_stop_event.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            _done, pending = await asyncio.wait(
                pending, timeout=min(remaining, _DRAIN_POLL_SECONDS)
            )
        if not pending:
            self._inflight.clear()
            return False
        if self._force_stop_event.is_set():
            logger.warning(
                "background_worker.force_stop inflight=%d (任务留给 lease 回收)",
                len(pending),
            )
        else:
            logger.warning(
                "background_worker.drain_timeout grace_seconds=%.1f inflight=%d",
                grace,
                len(pending),
            )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()
        return self._force_stop_event.is_set()


def route_stop_signal(worker: BackgroundWorker) -> None:
    """停止信号路由(issue #307):第一次 → 优雅停止,第二次 → 立即强退。"""

    if worker.stop_requested:
        worker.request_force_stop()
    else:
        worker.request_stop()


async def run_worker(
    *,
    engine: AsyncEngine,
    session_maker: async_sessionmaker[AsyncSession],
    registry: JobExecutorRegistry,
    config: WorkerConfig,
) -> None:
    """便捷入口:注册停止信号并阻塞运行,直到停止信号收尾完成。

    信号语义与退出码约定(issue #307):

    * **第一次停止信号**(POSIX SIGINT/SIGTERM;Windows Ctrl-C / CTRL_BREAK)——
      停止领取新任务并进入 :meth:`BackgroundWorker._drain`:grace=0 立即取消
      in-flight(现状语义),grace>0 先等 in-flight 完成至
      ``config.shutdown_grace_seconds`` 上限 —— 优雅完成退出码 0(宽限超时后
      的取消兜底同为 0,与 grace=0 同一终点语义);
    * **宽限等待中第二次停止信号** —— 立即强退,进程退出码
      :data:`WORKER_FORCE_EXIT_CODE`(130);未完成任务照旧由 lease 过期回收。
    """

    worker = BackgroundWorker(
        engine=engine, session_maker=session_maker, registry=registry, config=config
    )
    loop = asyncio.get_running_loop()
    win_restored: list[tuple[int, object]] = []
    if sys.platform == "win32":
        # Windows 的 asyncio 不支持 add_signal_handler(NotImplementedError):
        # 经 signal.signal 在主线程注册,回调里 call_soon_threadsafe 唤醒事件
        # 循环。注意:子进程按 CREATE_NEW_PROCESS_GROUP 托管时 Ctrl-C 被禁用,
        # 优雅信号是 CTRL_BREAK(映射为 SIGBREAK)—— 必须一并注册。
        def _on_stop_signal(signum: int, frame: object) -> None:
            loop.call_soon_threadsafe(route_stop_signal, worker)

        for sig in (signal.SIGINT, signal.SIGBREAK):
            # 非主线程(嵌入运行)或无控制台 —— 与既有 NotImplementedError
            # 抑制同口径:注册失败不阻断 worker 运行。
            with contextlib.suppress(OSError, ValueError):
                win_restored.append((sig, signal.signal(sig, _on_stop_signal)))
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, route_stop_signal, worker)
    try:
        force = await worker.run()
    finally:
        # 恢复既有 handler,避免嵌入调用方(如测试)残留全局信号状态。
        for restored_sig, restored_handler in win_restored:
            with contextlib.suppress(OSError, ValueError):
                signal.signal(restored_sig, restored_handler)  # type: ignore[arg-type]
        await engine.dispose()
    if force:
        raise SystemExit(WORKER_FORCE_EXIT_CODE)


__all__ = [
    "WORKER_FORCE_EXIT_CODE",
    "BackgroundWorker",
    "WorkerConfig",
    "default_worker_id",
    "route_stop_signal",
    "run_worker",
]
