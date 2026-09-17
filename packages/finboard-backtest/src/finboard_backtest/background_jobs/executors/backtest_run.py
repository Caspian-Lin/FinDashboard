"""``backtest_run`` 执行器 —— 回测运行接入统一队列(issue #144)。

迁移自 ``finboard_api.routes.backtest.run_backtest`` 同步阻塞路径。worker 领取
``kind=backtest_run`` 任务后,从 payload 重建 :class:`BacktestRunRequest`,委托注入的
``runner``(:func:`finboard_api.backtest_service.run_backtest_and_persist`)运行策略
并把完整结果写入 ``backtest_runs`` 表。

runner 注入的原因:回测编排依赖 ``finboard-api`` 的 Pydantic schema 与策略工厂
(``validate_strategy_params_for_api`` / ``create_strategy``),``finboard-backtest``
不能反向依赖 ``finboard-api``;因此由 CLI(``finboard-app``)延迟导入 runner 并注入。

幂等键 = ``sha256(strategy+symbols+start+end+params)``。``result_ref = str(run_id)``。

边界:只写 ``backtest_runs`` 表(由 runner 写入),不连 broker / 不下实盘单 /
不修改持仓。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)

if TYPE_CHECKING:
    from finboard_api.schemas import BacktestRunRequest


#: 注入点:在给定 session 上运行 request 并持久化,返回 run_id。
Runner = Callable[[AsyncSession, "BacktestRunRequest", str], Awaitable[int]]


class BacktestRunError(RuntimeError):
    """runner 抛出的业务失败(code + summary)。"""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


class BacktestRunExecutor:
    """``kind=backtest_run`` 执行器。"""

    KIND = "backtest_run"

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        runner: Runner,
    ) -> None:
        self._session_maker = session_maker
        self._runner = runner

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        raw_request = job.payload.get("request")
        if not isinstance(raw_request, dict):
            raise ExecutorError(
                code="invalid_payload",
                summary="backtest_run 任务 payload 必须包含 request 字典",
                retryable=False,
                context={"job_id": job.job_id},
            )
        provider_name = job.payload.get("provider_name", "akshare")
        if not isinstance(provider_name, str) or not provider_name:
            raise ExecutorError(
                code="invalid_payload",
                summary="provider_name 必须是非空字符串",
                retryable=False,
                context={"job_id": job.job_id},
            )

        from pydantic import ValidationError

        from finboard_api.schemas import BacktestRunRequest

        try:
            request = BacktestRunRequest.model_validate(raw_request)
        except ValidationError as exc:
            raise ExecutorError(
                code="invalid_payload",
                summary=f"request 不是合法的 BacktestRunRequest: {exc}",
                retryable=False,
                context={"job_id": job.job_id},
            ) from exc

        await progress(0, None, "backtest_run:engine")
        async with self._session_maker() as session:
            try:
                run_id = await self._runner(session, request, provider_name)
            except BacktestRunError as exc:
                raise ExecutorError(
                    code=exc.code,
                    summary=exc.summary,
                    retryable=False,
                    context={"job_id": job.job_id},
                ) from exc

        await progress(1, 1, "backtest_run:done")
        return JobResult(status="succeeded", result_ref=str(run_id))


_: type[JobExecutor] = BacktestRunExecutor

__all__ = ["BacktestRunError", "BacktestRunExecutor", "Runner"]
