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
    #: 僵尸无进展检测阈值(issue #306):heartbeat 正常续租但 progress_done /
    #: phase 持续无变化超过该秒数 → job 具名失败 ``zombie_no_progress`` 并转
    #: retry_waiting 走重试。默认 3600s(1 小时)—— 宽松值:健康的长任务
    #: (研究加载分块探针 / 数据摄取逐 symbol / 回测引擎)至少每小时推进一次
    #: 进度或阶段,再紧就开始有误杀长尾任务的风险;RR-7a74 类僵尸 7.5h 才被
    #: 人肉发现,1h 已能把它压缩到十分之一。0 = 关闭检测。
    zombie_no_progress_seconds: float = 3600.0


class _ZombieProgressWatch:
    """进程内「心跳在续但进度不动」监视器(issue #306,纯逻辑可单测)。

    维护路径每轮对全部 running job 观察一次指纹 ``(progress_done, phase)``:

    * 指纹变化(或首次见到)→ 记为基准点,不判僵尸;
    * 指纹不变且距基准点超过阈值 → 判僵尸(True);
    * :meth:`retain_only` 清掉本轮不在 running 列表的监视项,防字典无界增长。

    时间源由调用方注入(:func:`asyncio.get_running_loop().time` 单调钟),
    本类不做任何 IO;并发安全由维护路径的 ``transition(expected={running})``
    行锁 + 状态守卫保证(多 worker 同时判定时只有一个完成失败转移,另一方
    收到 conflict 后静默跳过),无需额外 advisory lock。
    """

    def __init__(self, threshold_seconds: float) -> None:
        self._threshold = threshold_seconds
        self._baseline: dict[str, tuple[tuple[object, object], float]] = {}

    @property
    def enabled(self) -> bool:
        return self._threshold > 0

    def observe(self, job_id: str, fingerprint: tuple[object, object], now: float) -> bool:
        """记录一次观察;指纹超阈值未变化返回 True(建议按僵尸失败)。"""

        entry = self._baseline.get(job_id)
        if entry is None or entry[0] != fingerprint:
            self._baseline[job_id] = (fingerprint, now)
            return False
        return (now - entry[1]) >= self._threshold

    def retain_only(self, job_ids: set[str]) -> None:
        self._baseline = {
            key: value for key, value in self._baseline.items() if key in job_ids
        }


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
        # issue #306:僵尸无进展监视(仅本进程观察到的指纹;判定动作的并发
        # 安全由 DB 行锁 + 状态守卫承担,见 _ZombieProgressWatch 文档)。
        self._zombie_watch = _ZombieProgressWatch(config.zombie_no_progress_seconds)

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
        """周期性维护:回收过期租约 + 重排重试任务 + 僵尸无进展检测(issue #161/#306)。"""

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
        await self._detect_zombie_jobs()

    async def _detect_zombie_jobs(self) -> None:
        """无进展僵尸检测(issue #306):heartbeat 在续但进度/阶段长期不动。

        活跃续租的 job 永远不会被 :meth:`_recover_stale` 回收(lease 每次心跳
        都被推远)—— 若 executor 卡死(如研究加载期阻塞、下游 IO 悬挂),任务
        会以「running + 新鲜 lease」的形态永久占坑。本检测在维护路径对全部
        ``running`` 且 heartbeat 新鲜的 job 观察指纹 ``(progress_done, phase)``:
        超过 ``zombie_no_progress_seconds`` 无任何变化 → ``transition(expected=
        {running}, target=retry_waiting, error_code=zombie_no_progress)`` 走既有
        重试语义(attempt 预算耗尽可能由 requeue_due 转 failed)。

        并发安全:``transition`` 的行锁 + ``expected={running}`` 状态守卫是唯一
        串行化点 —— 多 worker 同时判定同一僵尸时只有一个完成转移,另一方收到
        conflict 静默跳过(参考 claim_next 先例,但无需 advisory lock:无
        「计数 + 领取」式两段逻辑,单条守卫转移天然原子)。被转移 job 的原属主
        worker 心跳见状态已非 running 即停止续租(见心跳循环守卫),其 executor
        若日后自行完成,``_finalize`` 亦因状态冲突被跳过,不会覆盖重试结果。
        """

        if not self._zombie_watch.enabled:
            return
        now = datetime.now(UTC)
        loop_now = asyncio.get_running_loop().time()
        # heartbeat 新鲜判定:正常续租间隔为 heartbeat_interval,留 4 倍 +
        # 120s 下限余量吸收 Windows 调度抖动(#286 实测唤醒延迟可达 ~0.4s,
        # 放大余量避免把「心跳稍慢」误判为「心跳已死」—— 后者由 lease 回收兜底)。
        heartbeat_cutoff = now - timedelta(
            seconds=max(120.0, 4.0 * self._config.heartbeat_interval_seconds)
        )
        async with self._session_maker() as session:
            repo = BackgroundJobRepository(session)
            rows = await repo.list_recent(
                statuses=(BackgroundJobStatus.RUNNING.value,), limit=500
            )
            candidates = [
                row
                for row in rows
                if row.heartbeat_at is not None and row.heartbeat_at >= heartbeat_cutoff
            ]
            killed: list[str] = []
            for row in candidates:
                fingerprint = (row.progress_done, row.phase)
                if not self._zombie_watch.observe(row.job_id, fingerprint, loop_now):
                    continue
                try:
                    await repo.transition(
                        row.job_id,
                        expected=frozenset({BackgroundJobStatus.RUNNING.value}),
                        target=BackgroundJobStatus.RETRY_WAITING.value,
                        error_code="zombie_no_progress",
                        error_summary=(
                            f"僵尸检测:heartbeat 正常续租但 progress/phase 超过 "
                            f"{self._config.zombie_no_progress_seconds:.0f}s 无变化"
                            f"(done={row.progress_done}, phase={row.phase!r}),"
                            "按无进展失败并自动重试"
                        ),
                    )
                except BackgroundJobPersistenceConflictError:
                    # 另一 worker 的维护路径已处理(或状态已变)—— 只认赢家。
                    continue
                killed.append(row.job_id)
                logger.warning(
                    "background_worker.zombie_no_progress job_id=%s kind=%s "
                    "progress_done=%d phase=%s threshold=%.0fs",
                    row.job_id,
                    row.kind,
                    row.progress_done or 0,
                    row.phase,
                    self._config.zombie_no_progress_seconds,
                )
            if killed:
                await repo.checkpoint()
            # 清掉已不在 running 列表的监视项(终态 / 被本检测击杀 / 被回收)。
            self._zombie_watch.retain_only({row.job_id for row in rows} - set(killed))

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

        cancel_state = {"cancelled": False, "aborted_by_progress": False}

        async def progress(done: int, total: int | None, phase: str | None) -> None:
            async with self._session_maker() as session:
                repo = BackgroundJobRepository(session)
                row = await repo.get(job_id, for_update=True)
                if row is None:
                    return
                if row.status == BackgroundJobStatus.CANCEL_REQUESTED.value:
                    cancel_state["cancelled"] = True
                    # issue #306:标记「执行器经 progress 回调自愿中止」,让
                    # _execute_with_heart 把这次 CancelledError 收口为 cancelled
                    # 终态,而不是把孤儿 cancel_requested 行留给 lease 回收
                    # (旧路径最长悬挂一个 lease 周期,且 reclaim+requeue 会把
                    # 被取消的任务原样重放)。
                    cancel_state["aborted_by_progress"] = True
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
                    # issue #306:任务已离开本 worker 名下(僵尸检测转
                    # retry_waiting / reclaim 回收 / 重试被其他 worker 重新领取)
                    # 时停止续租 —— 否则僵尸检测击杀任务后,旧心跳仍把 lease
                    # 不断推远,重试编排与属主判定(research_run 启动恢复的
                    # job_ownership 探针)都会被污染。
                    if (
                        row.status != BackgroundJobStatus.RUNNING.value
                        or row.worker_id != self._config.worker_id
                    ):
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
            try:
                result = await executor.execute(record, progress)
            except asyncio.CancelledError:
                if not cancel_state["aborted_by_progress"]:
                    raise
                # issue #306:协作式取消经 progress 回调中止执行器 —— 立即按
                # cancelled 收口;外部(task/进程级)取消仍原样上抛,维持
                # 「留在 running 由 lease 回收」的停机语义不变。
                result = JobResult(status=BackgroundJobStatus.CANCELLED.value)
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
