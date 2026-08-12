"""``research_run`` 执行器 —— 把 ResearchRunCoordinator 接入统一队列(issue #143)。

worker 领取 ``kind=research_run`` 的任务后,本执行器从 ``job.payload.run_id`` 重建
``ResearchRunManifest``,委托 :class:`ResearchRunCoordinator.execute` 推进状态机。
Coordinator 内部已内建:

* QUEUED/INTERRUPTED/FAILED 重入(崩溃续跑)
* 每 decision ``checkpoint``(逐 commit,断点可恢复)
* 每 decision 重读 DB status(CANCELLED → 优雅退出,协作式取消)
* 异常分流(unsupported/constraint → REJECTED,interrupted → INTERRUPTED,
  其他 → FAILED)

因此执行器只需做:重建 manifest → 构造 adapter(注入)→ 调 execute → 映射终态。
``NormalizedSignal`` 等策略信号由注入的 ``adapter_factory`` 提供;本期 CLI 用固定
样本 :class:`~finboard_backtest.research_run.adapters.DecisionSequenceAdapter`,
真实「冻结产物 → PortfolioPipelineAdapter」信号引擎留后续 issue。

边界:不连 broker / 不下实盘单 / 不修改持仓;只写 ``research_runs`` 与
``research_run_artifacts`` 表(由 Coordinator 透过 store 写入)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.research_run import (
    ResearchRunCoordinator,
    ResearchRunManifest,
    ResearchRunStatus,
)
from finboard_backtest.research_run.adapters import ResearchStrategyAdapter

if TYPE_CHECKING:
    from finboard_backtest.research_run.store import ResearchRunStore

#: 把一个 AsyncSession 包成 ResearchRunStore(注入点,避免 finboard-backtest
#: 反向依赖 finboard-app 的 SqlAlchemyResearchRunStore 实现)。
StoreFactory = Callable[[AsyncSession], "ResearchRunStore"]

#: 按 manifest 构造策略适配器(注入点)。本期 CLI 提供固定样本工厂;真实工厂
#: 接 FrozenInputLoader + 策略信号引擎后注入。
AdapterFactory = Callable[[ResearchRunManifest], ResearchStrategyAdapter]


class ResearchRunExecutor:
    """``kind=research_run`` 执行器。"""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        store_factory: StoreFactory,
        adapter_factory: AdapterFactory,
    ) -> None:
        self._session_maker = session_maker
        self._store_factory = store_factory
        self._adapter_factory = adapter_factory

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        run_id = _extract_run_id(job)
        await progress(0, None, "research_run:start")
        async with self._session_maker() as session:
            store = self._store_factory(session)
            manifest = await _reconstruct_manifest(store, run_id)
            adapter = self._adapter_factory(manifest)
            coordinator = ResearchRunCoordinator(store)
            record = await coordinator.execute(manifest, adapter)
            await store.checkpoint()
        await progress(1, 1, f"research_run:{record.status.value}")
        return _record_to_result(record)


def _extract_run_id(job: JobRecord) -> str:
    raw = job.payload.get("run_id")
    if not isinstance(raw, str) or not raw.startswith("RR-"):
        raise ExecutorError(
            code="invalid_payload",
            summary="research_run 任务 payload 必须包含合法 run_id(RR- 前缀)",
            retryable=False,
            context={"job_id": job.job_id},
        )
    return raw


async def _reconstruct_manifest(
    store: ResearchRunStore,
    run_id: str,
) -> ResearchRunManifest:
    record = await store.get(run_id)
    if record is None:
        raise ExecutorError(
            code="missing_research_run",
            summary=f"研究运行 {run_id} 不存在,可能已被清理",
            retryable=False,
            context={"run_id": run_id},
        )
    return record.manifest


def _record_to_result(
    record: object,
) -> JobResult:
    """把 ``ResearchRunRecord`` 终态映射为 ``JobResult``。"""
    status: ResearchRunStatus = record.status  # type: ignore[attr-defined]
    run_id: str = record.manifest.run_id  # type: ignore[attr-defined]
    if status is ResearchRunStatus.COMPLETED:
        return JobResult(
            status="succeeded",
            result_ref=run_id,
        )
    if status is ResearchRunStatus.CANCELLED:
        return JobResult(status="cancelled", result_ref=run_id)
    if status is ResearchRunStatus.INTERRUPTED:
        return JobResult(
            status="retry_waiting",
            result_ref=run_id,
            error_code="interrupted",
            error_summary=getattr(record, "error_summary", None),
        )
    # FAILED / REJECTED 均视为不可重试失败(coordinator 已完成异常分流)。
    return JobResult(
        status="failed",
        result_ref=run_id,
        error_code=getattr(record, "error_code", None),
        error_summary=getattr(record, "error_summary", None),
    )


def default_store_factory(session: AsyncSession) -> ResearchRunStore:
    """默认 store 工厂:构造 ``SqlAlchemyResearchRunStore``。

    放在此处以便 CLI 直接复用;延迟导入避免 finboard-backtest 顶层依赖 finboard-app。
    """
    from finboard_app.research_run_store import SqlAlchemyResearchRunStore
    from finboard_persistence import ResearchRunRepository

    return SqlAlchemyResearchRunStore(ResearchRunRepository(session))


# JobExecutor 是 runtime_checkable Protocol,直接用类即满足结构子类型。
_: type[JobExecutor] = ResearchRunExecutor

__all__ = [
    "AdapterFactory",
    "ResearchRunExecutor",
    "StoreFactory",
    "default_store_factory",
]
