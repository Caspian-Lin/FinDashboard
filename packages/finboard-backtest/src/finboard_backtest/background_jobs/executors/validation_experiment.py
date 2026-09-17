"""``validation_experiment`` 执行器 —— #57 验证实验运营入口(issue #233)。

#219 晋级门的 OOS 半边要求实验达到 ``validated_oos + final_test_unsealed``;
在此之前 :class:`~finboard_backtest.validation.runner.ValidationRunner` 只有
单测在调用,任何实验都到不了该终态,promote 的 validation 门永远不可满足。
本执行器把「按计划真正跑 walk-forward 并揭盲」接入统一任务队列:

* 入队预检(MCP 语义工具 ``finboard_validation_experiment_run``)秒级拒绝
  不可运行 / 预算耗尽的实验;执行端重放同一组检查(双保险,fail-closed);
* worker 单并发领取后:重建实验 → 注入 :type:`TrialRunner` →
  ``run_in_sample``(逐 trial 落库,失败也算试验)→ ``run_walk_forward``
  (OOS 门 + 稳健性 + 统计修正)→ ``unseal_final_test``(一次性揭盲);
* **揭盲不可重做**:实验一旦持久化 ``final_test_unsealed`` / 终态,重复执行
  秒级拒绝(experiment_not_runnable),不产生第二次最终测试集评估;
* **断点续跑安全**:trial_id 由 ``(experiment_id, candidate 序号)`` 确定性
  生成且仓储按 trial_id upsert,重入时先加载已持久化 trial 并跳过已跑候选,
  ``trials_used`` 不重复递增;
* 失败映射:实验不存在 / 不可运行 / runner 未配置 / 配置不合法 → 具名
  ``ExecutorError``(均不可重试,自动重试只会重复消耗试验预算,
  ``max_attempts`` 默认 1);阈值不满足是**正常实验结论**(REJECTED),
  job failed 并携带可读原因,不是执行器错误。walk-forward / 揭盲段的
  意外异常兜底为具名 ``experiment_execution_failed``,实验状态不被推进
  (可修复后重新入队断点续跑,issue #244)。

:type:`TrialRunnerFactory` 是注入点:默认实现按实验
``version_stamp.selection_config["validation_trial_runner"]`` 声明的
``{strategy, symbols, provider?, params?}`` 构建注册表策略回测;未声明的
实验在执行期得到具名 ``trial_runner_unconfigured`` 错误(fail-visible,
不猜测回测语义)。测试注入合成 runner。

边界:纯研究/回测域,只写 ``research_experiments`` / ``research_trials`` 表,
不连 broker / 不下单 / 不修改持仓。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.validation.contracts import (
    ExperimentStatus,
    ResearchExperiment,
    TrialStatus,
)
from finboard_backtest.validation.runner import (
    TrialRunner,
    ValidationRunner,
    grid_candidates,
)

#: 每提交一个实验构建一次 trial runner(注入点;测试给合成 runner)。
TrialRunnerFactory = Callable[
    [AsyncSession, ResearchExperiment], Awaitable[TrialRunner]
]

_RUNNABLE_STATUSES = frozenset(
    {ExperimentStatus.HYPOTHESIS, ExperimentStatus.IN_SAMPLE}
)

logger = structlog.get_logger(__name__)


class ValidationExperimentExecutor:
    """``kind=validation_experiment`` 执行器。"""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        runner_factory: TrialRunnerFactory,
    ) -> None:
        self._session_maker = session_maker
        self._runner_factory = runner_factory

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        experiment_id = _extract_experiment_id(job)
        await progress(0, None, "validation_experiment:start")
        async with self._session_maker() as session:
            try:
                return await self._execute_locked(
                    session, progress, experiment_id
                )
            except ExecutorError:
                raise
            except Exception as exc:
                raise await _execution_failed_error(
                    session, experiment_id, exc
                ) from exc

    async def _execute_locked(
        self,
        session: AsyncSession,
        progress: ProgressCallback,
        experiment_id: str,
    ) -> JobResult:
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
            ResearchTrialRepository,
        )

        exp_repo = ResearchExperimentRepository(session)
        trial_repo = ResearchTrialRepository(session)
        experiment = await exp_repo.get(experiment_id)
        if experiment is None:
            raise ExecutorError(
                code="missing_experiment",
                summary=f"验证实验不存在: {experiment_id}",
                retryable=False,
                context={"experiment_id": experiment_id},
            )
        _precheck_runnable(experiment)
        trial_runner = await self._runner_factory(session, experiment)
        runner = ValidationRunner(
            experiment=experiment, trial_runner=trial_runner
        )
        # 断点续跑:预载已持久化 trial(upsert 幂等),trials_used 不重复递增。
        persisted = await trial_repo.list_by_experiment(experiment_id)
        runner.trials.extend(persisted)
        candidates = _candidates(experiment)
        pending = candidates[len(persisted):]

        total_stages = len(pending) + 3
        done = 0
        if pending:
            await progress(done, total_stages, "validation_experiment:in_sample")
            for trial in await runner.run_in_sample(pending):
                await trial_repo.save(trial)
                await exp_repo.save(runner.experiment)
                await session.commit()
                done += 1
                await progress(
                    done, total_stages, "validation_experiment:in_sample"
                )
        else:
            # 既有 trial 可能不是 runner 记忆里的最优;重算 IS 最优。
            _reselect_best(runner)
            done += 1

        if runner.best_trial_id is None:
            runner.reject(
                "no trial passed the in-sample gate "
                f"(trials_used={runner.experiment.trials_used}/"
                f"{runner.experiment.plan.trial_budget})"
            )
            await exp_repo.save(runner.experiment)
            await session.commit()
            await progress(total_stages, total_stages, "validation_experiment:rejected")
            return _experiment_result(runner.experiment)

        await progress(done, total_stages, "validation_experiment:walk_forward")
        best = await runner.run_walk_forward()
        if best is not None:
            await trial_repo.save(best)
            await session.commit()
        done += 1

        # 揭盲是一次性门:重入时已持久化 unsealed/终态的实验在上面
        # _precheck_runnable 已拒绝;走到这里说明本次会话内完成 IS+OOS。
        await progress(done, total_stages, "validation_experiment:final_test")
        verdict = await runner.unseal_final_test()
        await exp_repo.save(runner.experiment)
        if runner.best_trial_id is not None:
            final_best = next(
                (
                    t
                    for t in runner.trials
                    if t.trial_id == runner.best_trial_id
                ),
                None,
            )
            if final_best is not None:
                await trial_repo.save(final_best)
        await session.commit()
        await progress(
            total_stages,
            total_stages,
            f"validation_experiment:{runner.experiment.status.value}",
        )
        return _experiment_result(runner.experiment, verdict=verdict)


async def _execution_failed_error(
    session: AsyncSession,
    experiment_id: str,
    exc: Exception,
) -> ExecutorError:
    """意外异常兜底(issue #244):具名化 + 保留可续跑状态。

    实验状态由已持久化的事务决定(IS 段逐 trial 提交),本兜底只回滚
    失败事务并回读当前状态写进错误摘要;**不**把实验推进到终态 —— 崩溃
    是执行器/平台问题而非实验结论,REJECTED 会永久烧掉实验(揭盲不可
    重做),保留 ``in_sample`` 让修复后重入队断点续跑。
    """
    status_text = "unknown"
    try:
        await session.rollback()
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
        )

        current = await ResearchExperimentRepository(session).get(experiment_id)
        status_text = current.status.value if current else "missing"
    except Exception:
        logger.warning(
            "validation_experiment.status_probe_failed",
            experiment_id=experiment_id,
        )
    logger.warning(
        "validation_experiment.execution_failed",
        experiment_id=experiment_id,
        persisted_status=status_text,
        error=f"{type(exc).__name__}: {exc}",
    )
    return ExecutorError(
        code="experiment_execution_failed",
        summary=(
            f"实验执行异常中断: {type(exc).__name__}: {exc};"
            f"实验状态保留在 {status_text},修复后可重新入队断点续跑"
            "(已持久化的 trial 不重跑)"
        ),
        retryable=False,
        context={"experiment_id": experiment_id},
    )


def _extract_experiment_id(job: JobRecord) -> str:
    raw = job.payload.get("experiment_id")
    if not isinstance(raw, str) or not raw.strip():
        raise ExecutorError(
            code="invalid_payload",
            summary="validation_experiment 任务 payload 必须包含 experiment_id",
            retryable=False,
            context={"job_id": job.job_id},
        )
    return raw.strip()


def _precheck_runnable(experiment: ResearchExperiment) -> None:
    """执行端重放入队预检(双保险);不可运行一律具名拒绝。"""
    if experiment.status not in _RUNNABLE_STATUSES:
        raise ExecutorError(
            code="experiment_not_runnable",
            summary=(
                f"实验状态不可运行: status={experiment.status.value}"
                "(终态 / 已揭盲的实验不可重复执行——揭盲不可重做)"
            ),
            retryable=False,
            context={"experiment_id": experiment.experiment_id},
        )
    if experiment.final_test_unsealed:
        raise ExecutorError(
            code="experiment_not_runnable",
            summary=(
                "实验已揭盲(final_test_unsealed=true),重复执行被拒绝"
                "(揭盲不可重做,不产生第二次最终测试集评估)"
            ),
            retryable=False,
            context={"experiment_id": experiment.experiment_id},
        )


def _candidates(experiment: ResearchExperiment) -> list[dict[str, object]]:
    """从冻结的参数空间生成候选(grid 笛卡尔积,标量视作单点)。

    候选数截断到试验预算,预算耗尽即停止(runner 内同样有预算门)。
    """
    space = experiment.strategy_params_space or {}
    grid: dict[str, list[object]] = {}
    for key, value in space.items():
        grid[key] = list(value) if isinstance(value, (list, tuple)) else [value]
    candidates = grid_candidates(grid) if grid else [{}]
    return candidates[: experiment.plan.trial_budget]


def _reselect_best(runner: ValidationRunner) -> None:
    """续跑场景:既有 trial 里重算 IS 最优(run_in_sample 未新增时)。"""
    successful = [
        t
        for t in runner.trials
        if t.status is TrialStatus.SELECTED and t.in_sample_metrics
    ]
    if successful:
        runner.best_trial_id = max(
            successful,
            key=lambda t: (
                t.in_sample_metrics.sharpe_ratio if t.in_sample_metrics else float("-inf")
            ),
        ).trial_id


def _experiment_result(
    experiment: ResearchExperiment,
    *,
    verdict: Any = None,
) -> JobResult:
    """把实验终态映射为 JobResult;REJECTED 是实验结论,job failed 带原因。"""
    if experiment.status is ExperimentStatus.VALIDATED_OOS:
        return JobResult(
            status="succeeded",
            result_ref=experiment.experiment_id,
        )
    if experiment.status is ExperimentStatus.REJECTED:
        return JobResult(
            status="failed",
            result_ref=experiment.experiment_id,
            error_code="experiment_rejected",
            error_summary=(
                experiment.rejection_reason
                or (verdict.reason if verdict is not None else None)
                or "experiment rejected by validation gates"
            ),
        )
    # 理论不可达(unseal 后必为 validated_oos/rejected);fail-visible。
    return JobResult(
        status="failed",
        result_ref=experiment.experiment_id,
        error_code="experiment_incomplete",
        error_summary=f"实验未到达终态: status={experiment.status.value}",
    )


# ---------------------------------------------------------------------------
# 默认 TrialRunner 工厂:按实验 selection_config 声明构建注册表策略回测
# ---------------------------------------------------------------------------


async def default_trial_runner_factory(
    session: AsyncSession,
    experiment: ResearchExperiment,
) -> TrialRunner:
    """按 ``selection_config["validation_trial_runner"]`` 构建 trial runner。

    声明契约(放在实验冻结的 version_stamp.selection_config 内)::

        {
            "validation_trial_runner": {
                "strategy": "mean_reversion",   # 策略注册表名(必填)
                "symbols": ["510300.SH", ...],  # 标的(必填)
                "provider": "akshare",          # akshare|tushare|yfinance
                "params": {},                   # 基础参数,candidate 参数覆盖
                "capital": 100000
            }
        }

    未声明 / 声明不完整 → 具名 ``trial_runner_unconfigured``(fail-visible,
    不猜测回测语义;测试注入合成 runner)。声明了但配置不合法(capital 非
    数值 / params 非 mapping / 策略名未注册或基础参数不合法)→ 具名
    ``trial_runner_invalid_config``,同样在首个 trial 启动前失败,不消耗
    试验预算(issue #244)。回测直接走 ``BacktestEngine``,不落
    backtest_runs(试验属于实验,不属于回测历史)。
    """
    del session  # 预留:快照/数据面扩展时复用请求级 session
    raw_config = experiment.version_stamp.selection_config.get(
        "validation_trial_runner"
    )
    config: dict[str, Any] = dict(raw_config) if isinstance(raw_config, Mapping) else {}
    strategy_name = config.get("strategy")
    symbols = config.get("symbols")
    if not isinstance(strategy_name, str) or not strategy_name:
        raise ExecutorError(
            code="trial_runner_unconfigured",
            summary=(
                "实验未声明可执行的 trial runner:请在 version_stamp."
                'selection_config.validation_trial_runner 提供 {strategy, '
                "symbols}(注册表策略回测);或扩展执行器注入自定义工厂"
            ),
            retryable=False,
            context={"experiment_id": experiment.experiment_id},
        )
    if not isinstance(symbols, list) or not symbols:
        raise ExecutorError(
            code="trial_runner_unconfigured",
            summary="validation_trial_runner.symbols 必须为非空标的列表",
            retryable=False,
            context={"experiment_id": experiment.experiment_id},
        )

    from finboard_app.strategies import (
        create_strategy,
        list_strategy_definitions,
    )
    from finboard_backtest import BacktestConfig, BacktestEngine, BenchmarkConfig
    from finboard_backtest.background_jobs.executors._providers import (
        build_bar_provider,
        default_settings_factory,
        resolve_provider_name,
    )

    # 配置在 runner 构建期全部预校验(issue #244):任何一项不合法都在首个
    # trial 启动前具名失败,不消耗试验预算,也不留下「str + decimal.Decimal」
    # 式的逐 trial 不透明失败原因。
    base_params_raw = config.get("params", {})
    if not isinstance(base_params_raw, Mapping):
        raise ExecutorError(
            code="trial_runner_invalid_config",
            summary=(
                "validation_trial_runner.params 必须为对象(mapping),"
                f"得到 {type(base_params_raw).__name__}: {base_params_raw!r}"
            ),
            retryable=False,
            context={"experiment_id": experiment.experiment_id},
        )
    base_params = dict(base_params_raw)
    capital = _coerce_capital(
        config.get("capital", 100000), experiment.experiment_id
    )
    try:
        create_strategy(strategy_name, "validation-probe", **base_params)
    except Exception as exc:
        available = ", ".join(
            definition.kind for definition in list_strategy_definitions()
        )
        raise ExecutorError(
            code="trial_runner_invalid_config",
            summary=(
                f"validation_trial_runner 无法构建策略 {strategy_name!r}"
                f"(参数 {base_params!r}): {exc};可用策略: {available}"
            ),
            retryable=False,
            context={"experiment_id": experiment.experiment_id},
        ) from exc

    settings_factory = default_settings_factory
    provider = build_bar_provider(
        resolve_provider_name(
            str(config.get("provider")) if config.get("provider") else None,
            settings_factory,
        ),
        settings_factory,
    )

    benchmark = config.get("benchmark")
    symbol_codes = [str(s) for s in symbols]

    async def _run(
        *,
        start: Any,
        end: Any,
        params: dict[str, object],
        config_overrides: dict[str, object] | None = None,
    ) -> Any:
        del config_overrides  # 预留:窗口级成本/滑点压力覆盖
        merged: dict[str, Any] = {**base_params, **params}
        strategy = create_strategy(strategy_name, "backtest", **merged)
        backtest_config = BacktestConfig(
            symbols=symbol_codes,
            start=start,
            end=end,
            initial_capital=capital,
            strategy_params=merged,
            benchmark=(
                BenchmarkConfig(symbol=str(benchmark))
                if benchmark
                else BenchmarkConfig()
            ),
        )
        engine = BacktestEngine(
            strategy=strategy,
            data_provider=provider,
            config=backtest_config,
        )
        return await engine.run()

    return _run


def _coerce_capital(raw: object, experiment_id: str) -> Decimal:
    """``validation_trial_runner.capital`` 宽进严出(issue #244)。

    接受 int / float / 数值字符串,统一转 ``Decimal`` —— ``BacktestConfig``
    是 dataclass(``initial_capital: Decimal``),JSON 里的 str 直通会在引擎
    算术处炸出不透明的 ``str + decimal.Decimal``;bool 是 int 子类需显式
    排除。非正数 / 非有限值一律具名拒绝,在首个 trial 启动前 fail-fast。
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str, Decimal)):
        raise ExecutorError(
            code="trial_runner_invalid_config",
            summary=(
                "validation_trial_runner.capital 必须为数值"
                f"(int/float/数值字符串),得到 {type(raw).__name__}: {raw!r}"
            ),
            retryable=False,
            context={"experiment_id": experiment_id},
        )
    try:
        capital = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ExecutorError(
            code="trial_runner_invalid_config",
            summary=f"validation_trial_runner.capital 无法解析为数值: {raw!r}",
            retryable=False,
            context={"experiment_id": experiment_id},
        ) from exc
    if not capital.is_finite() or capital <= 0:
        raise ExecutorError(
            code="trial_runner_invalid_config",
            summary=(
                f"validation_trial_runner.capital 必须为正有限数,得到 {capital}"
            ),
            retryable=False,
            context={"experiment_id": experiment_id},
        )
    return capital


# JobExecutor 是 runtime_checkable Protocol,直接用类即满足结构子类型。
_: type[JobExecutor] = ValidationExperimentExecutor

__all__ = [
    "TrialRunnerFactory",
    "ValidationExperimentExecutor",
    "default_trial_runner_factory",
]
