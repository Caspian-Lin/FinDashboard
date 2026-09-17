"""验证流水线编排器(issue #57)。

``ValidationRunner`` 编排一次完整的研究实验:

1. **IS 阶段**:对参数空间中每个候选参数跑训练集回测,记录全部试验(赢家 +
   输家),按门筛掉明显失败的。
2. **OOS 阶段**:对最优 trial 跑 walk-forward 多窗口 OOS,聚合 OOS 指标。
3. **稳健性阶段**:邻域 + 成本 + 延迟 + 市场阶段压力测试。
4. **统计修正阶段**:Deflated Sharpe / PSR / PBO / bootstrap CI。
5. **门判定**:根据 ``AcceptanceThresholds`` 判定 pass / fail。
6. **揭盲门**:可选的最终冻结测试集揭盲(只允许一次)。

设计原则
========

* **不触及交易红线** —— runner 只调度回测引擎,不连接券商 / 不发订单 / 不修
  改持仓。
* **失败也算试验** —— ``TrialRecord.status=FAILED`` / ``REJECTED`` 必须入库,
  多重试验修正需要真实试验总数。
* **门不可绕过** —— runner 仅判定 pass / fail,不调整阈值。揭盲前不可看冻结
  测试集。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Protocol

import structlog

from finboard_backtest.metrics import (
    annualized_return,
    calmar_ratio,
    conditional_value_at_risk,
    information_ratio,
    max_drawdown,
    max_drawdown_duration,
    monthly_win_rate,
    sharpe_ratio,
    sortino_ratio,
    total_return,
    turnover_ratio,
    value_at_risk,
)
from finboard_backtest.metrics import (
    daily_returns as compute_daily_returns,
)
from finboard_backtest.result import BacktestResult
from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    ExperimentVerdict,
    ResearchExperiment,
    RobustnessProbe,
    StatisticalReport,
    TrialRecord,
    TrialStatus,
    WindowMetrics,
    WindowRole,
    append_note,
    increment_trials_used,
    mark_final_test_unsealed,
    transition_status,
)
from finboard_backtest.validation.robustness import (
    DEFAULT_STRESS_PHASES,
    make_probe,
    stress_phases_for_range,
)
from finboard_backtest.validation.splitter import (
    generate_walk_forward_windows,
)
from finboard_backtest.validation.statistics import (
    build_statistical_report,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Trial 执行接口
# ---------------------------------------------------------------------------


class TrialRunner(Protocol):
    """单次回测执行接口。

    实现可以是真实的 ``BacktestEngine`` 包装,也可以是测试用的合成实现。
    runner 通过此接口完全解耦,不直接 import 实盘 / 数据下载逻辑。
    """

    def __call__(
        self,
        *,
        start: date,
        end: date,
        params: dict[str, object],
        config_overrides: dict[str, object] | None = ...
    ) -> Awaitable[BacktestResult]:
        ...


@dataclass(frozen=True, slots=True)
class WindowResult:
    """单窗口回测结果 + 衍生指标。"""

    metrics: WindowMetrics
    raw_result: BacktestResult | None = None
    daily_returns: tuple[float, ...] = ()


def compute_window_metrics(
    *,
    role: WindowRole,
    start: date,
    end: date,
    result: BacktestResult,
) -> WindowResult:
    """从 ``BacktestResult`` 提取单窗口 ``WindowMetrics``。"""
    eq = result.equity_curve
    bench = result.benchmark_curve

    return WindowResult(
        metrics=WindowMetrics(
            role=role,
            start=start,
            end=end,
            total_return=total_return(eq),
            annualized_return=annualized_return(eq),
            sharpe_ratio=sharpe_ratio(eq),
            sortino_ratio=sortino_ratio(eq),
            calmar_ratio=calmar_ratio(eq),
            information_ratio=information_ratio(eq, bench) if bench else 0.0,
            max_drawdown=max_drawdown(eq),
            max_drawdown_duration=max_drawdown_duration(eq),
            monthly_win_rate=monthly_win_rate(eq),
            var_95=value_at_risk(eq),
            cvar_95=conditional_value_at_risk(eq),
            trade_count=len(result.fills),
            turnover=turnover_ratio(result.fills, eq),
            benchmark_return=total_return(bench) if bench else 0.0,
            excess_return=total_return(eq) - (total_return(bench) if bench else 0.0),
        ),
        raw_result=result,
        daily_returns=tuple(compute_daily_returns(eq)),
    )


# ---------------------------------------------------------------------------
# 验证 runner
# ---------------------------------------------------------------------------


@dataclass
class ValidationRunner:
    """样本外验证流水线编排器。

    用法::

        runner = ValidationRunner(
            experiment=exp,
            trial_runner=my_backtest_runner,
        )
        verdict = await runner.run_in_sample()
        if verdict.status == ExperimentStatus.IN_SAMPLE:
            verdict = await runner.run_walk_forward(...)
            verdict = await runner.unseal_final_test(...)
    """

    experiment: ResearchExperiment
    trial_runner: TrialRunner
    trials: list[TrialRecord] = field(default_factory=list)
    best_trial_id: str | None = None
    # IS 阶段各 trial 的日收益(内存态,不持久化):PBO/CSCV 的输入矩阵要求
    # 行=竞争配置、列=同一观测轴,IS 各 trial 在同一训练窗回测天然满足。
    # 续跑时从仓储加载的 trial 不在其中 → PBO 跳过并具名说明。
    _in_sample_returns: dict[str, tuple[float, ...]] = field(default_factory=dict)
    _state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def experiment_status(self) -> ExperimentStatus:
        return self.experiment.status

    async def run_in_sample(
        self,
        candidates: Sequence[Mapping[str, object]],
    ) -> list[TrialRecord]:
        """在训练集上跑全部候选参数,返回所有 trial 记录(包括失败)。

        参数
        ----
        candidates
            参数候选列表。每个元素是策略的参数字典。

        返回
        ----
        ``list[TrialRecord]`` —— 长度 = len(candidates)。

        副作用
        ------
        * ``self.trials`` 累积全部记录;
        * ``self.experiment.trials_used`` 单调递增;
        * ``self.experiment.status`` 从 ``HYPOTHESIS`` 切到 ``IN_SAMPLE``;
        * 若试验预算耗尽 → 切到 ``REJECTED``。
        """
        async with self._state_lock:
            if self.experiment.status == ExperimentStatus.HYPOTHESIS:
                self.experiment = transition_status(
                    self.experiment, ExperimentStatus.IN_SAMPLE
                )

        new_trials: list[TrialRecord] = []
        for params in candidates:
            if not self.experiment.can_run_trial():
                logger.warning(
                    "validation.trial_budget_exhausted",
                    experiment_id=self.experiment.experiment_id,
                    used=self.experiment.trials_used,
                    budget=self.experiment.plan.trial_budget,
                )
                break
            trial = await self._run_single_trial(
                params=params,
                trial_index=len(self.trials),
            )
            new_trials.append(trial)
            self.trials.append(trial)
            async with self._state_lock:
                self.experiment = increment_trials_used(self.experiment)

        # 选 IS 最优 trial(按 IS Sharpe,失败 trial 不参与)
        successful = [t for t in self.trials if t.status == TrialStatus.SELECTED and t.in_sample_metrics]
        if successful:
            best = max(
                successful,
                key=lambda t: t.in_sample_metrics.sharpe_ratio if t.in_sample_metrics else float("-inf"),
            )
            self.best_trial_id = best.trial_id

        return new_trials

    async def _run_single_trial(
        self,
        *,
        params: Mapping[str, object],
        trial_index: int,
    ) -> TrialRecord:
        """跑一次 IS 回测,记录失败也算试验。"""
        plan = self.experiment.plan
        trial_id = f"{self.experiment.experiment_id}-t{trial_index}"
        created_at = datetime.now(UTC)

        try:
            result = await self.trial_runner(
                start=plan.train_start,
                end=plan.train_end,
                params=dict(params),
            )
        except Exception as exc:
            logger.warning(
                "validation.trial_failed",
                trial_id=trial_id,
                error=str(exc),
            )
            return TrialRecord(
                trial_id=trial_id,
                experiment_id=self.experiment.experiment_id,
                trial_index=trial_index,
                parameters=dict(params),
                status=TrialStatus.FAILED,
                failure_reason=str(exc),
                created_at=created_at,
                completed_at=datetime.now(UTC),
            )

        window_result = compute_window_metrics(
            role=WindowRole.TRAIN,
            start=plan.train_start,
            end=plan.train_end,
            result=result,
        )
        # 无论是否过 IS 门,收益都保留:PBO 要看全部竞争 trial(含被拒者)。
        self._in_sample_returns[trial_id] = window_result.daily_returns

        # IS 门判定
        thr = self.experiment.thresholds
        is_pass = (
            window_result.metrics.sharpe_ratio >= thr.min_in_sample_sharpe
        )
        status = TrialStatus.SELECTED if is_pass else TrialStatus.REJECTED
        return TrialRecord(
            trial_id=trial_id,
            experiment_id=self.experiment.experiment_id,
            trial_index=trial_index,
            parameters=dict(params),
            status=status,
            in_sample_metrics=window_result.metrics,
            failure_reason=None if is_pass else (
                f"is_sharpe={window_result.metrics.sharpe_ratio:.3f} "
                f"< min_in_sample_sharpe={thr.min_in_sample_sharpe:.3f}"
            ),
            created_at=created_at,
            completed_at=datetime.now(UTC),
        )

    async def run_walk_forward(
        self,
        *,
        config_overrides: dict[str, object] | None = None,
    ) -> TrialRecord | None:
        """对 IS 最优 trial 跑 walk-forward OOS。

        必须在 ``run_in_sample`` 之后、最优 trial 选定后调用。返回更新后的
        ``TrialRecord``(包含 OOS 窗口指标 + 统计报告)。
        """
        if self.best_trial_id is None:
            return None
        best = next(t for t in self.trials if t.trial_id == self.best_trial_id)

        plan = self.experiment.plan
        # Walk-forward 在 [train_start, test_start) 之间滑动,
        # 不进入冻结测试集(冻结集留给 unseal_final_test)。
        # test_start 由 plan 校验保证 > train_end >= validation_start > train_start
        windows = generate_walk_forward_windows(
            mode=plan.mode,
            train_start=plan.train_start,
            train_end=plan.validation_end,
            test_start=plan.validation_start,
            test_end=plan.test_start,
            train_window_days=plan.train_window_days,
            test_window_days=plan.test_window_days,
            step_days=plan.step_days,
        )

        if not windows:
            logger.warning(
                "validation.no_walk_forward_windows",
                experiment_id=self.experiment.experiment_id,
            )
            return replace(
                best,
                status=TrialStatus.REJECTED,
                failure_reason="no walk-forward windows generated (plan too short)",
            )

        oos_window_results: list[WindowResult] = []
        all_window_returns: list[list[float]] = []
        for window in windows:
            try:
                result = await self.trial_runner(
                    start=window.test.start,
                    end=window.test.end,
                    params=best.parameters,
                    config_overrides=config_overrides,
                )
            except Exception as exc:
                logger.warning(
                    "validation.walk_forward_window_failed",
                    window_index=window.window_index,
                    error=str(exc),
                )
                continue
            wr = compute_window_metrics(
                role=WindowRole.TEST,
                start=window.test.start,
                end=window.test.end,
                result=result,
            )
            oos_window_results.append(wr)
            all_window_returns.append(list(wr.daily_returns))

        if not oos_window_results:
            return replace(
                best,
                status=TrialStatus.REJECTED,
                failure_reason="all walk-forward windows failed",
            )

        # 聚合 OOS 指标(平均 / 加权)
        agg_oos = _aggregate_oos_metrics(oos_window_results)

        # 跑稳健性 probe
        probes = await self._run_robustness_probes(best=best)

        # 跑统计修正。PBO 矩阵取 IS 阶段各竞争 trial 的日收益(同窗同轴),
        # 不再用顺序 walk-forward 窗口序列:窗口按近似交易日切分、真实权益
        # 曲线按真实交易日产出,点数天然不等长,曾触发 CSCV 严格等长断言
        # 使实验对一切配置必然崩溃(issue #244);窗口拼接序列仍作为
        # best_returns 进入 DSR / PSR / bootstrap。
        best_returns: list[float] = []
        for r in all_window_returns:
            best_returns.extend(r)
        stat_report = self._build_stat_report(best_returns=best_returns)

        # OOS 门判定
        thr = self.experiment.thresholds
        oos_pass = self._evaluate_oos_gate(agg_oos, stat_report, thr)

        new_status = TrialStatus.SELECTED if oos_pass else TrialStatus.REJECTED
        new_failure = None if oos_pass else self._format_oos_failure(agg_oos, stat_report, thr)

        updated = replace(
            best,
            status=new_status,
            oos_metrics=agg_oos,
            walk_forward_windows=tuple(wr.metrics for wr in oos_window_results),
            robustness_probes=tuple(probes),
            statistical_report=stat_report,
            failure_reason=new_failure,
        )
        # 替换 self.trials 中的 best
        for i, t in enumerate(self.trials):
            if t.trial_id == best.trial_id:
                self.trials[i] = updated
                break
        return updated

    async def _run_robustness_probes(
        self,
        *,
        best: TrialRecord,
    ) -> list[RobustnessProbe]:
        """跑参数邻域 + 市场阶段压力测试。

        成本 / 滑点 / 延迟压力需要重跑回测,留给调用方在更高层注入。
        本方法只覆盖邻域(同种子、不同参数)+ 市场阶段(同参数、子区间)。
        """
        probes: list[RobustnessProbe] = []
        plan = self.experiment.plan

        # 邻域:围绕最优参数 ±10%
        # 注意:这里我们用最优参数本身在多个子区间上跑,作为邻域近似。
        # 完整邻域扫描需要在 run_in_sample 阶段生成 candidates。
        # 这里跳过完整邻域,改为直接复用 walk-forward 各窗口的 Sharpe 作为邻域近似。
        if best.walk_forward_windows:
            sharpes = [w.sharpe_ratio for w in best.walk_forward_windows]
            worst = min(sharpes)
            best_sharpe = best.in_sample_metrics.sharpe_ratio if best.in_sample_metrics else 0.0
            drop = (best_sharpe - worst) / best_sharpe if best_sharpe > 0 else 0.0
            probes.append(
                make_probe(
                    "neighbourhood",
                    "walk_forward_worst",
                    sharpe_ratio=worst,
                    max_drawdown=min(w.max_drawdown for w in best.walk_forward_windows),
                    total_return=min(w.total_return for w in best.walk_forward_windows),
                    thresholds={"min_sharpe": 0.0, "max_drawdown": -1.0},
                    detail={"drop_from_is": drop},
                )
            )

        # 市场阶段:同参数,在 2018-Q4 / 2020-Q1 / 2022-Q1 / 2024-Q1 上跑
        relevant_phases = stress_phases_for_range(
            plan.validation_start, plan.test_end, DEFAULT_STRESS_PHASES
        )
        for phase in relevant_phases:
            try:
                result = await self.trial_runner(
                    start=phase.start,
                    end=phase.end,
                    params=best.parameters,
                )
            except Exception as exc:
                logger.warning(
                    "validation.stress_phase_failed",
                    phase=phase.label,
                    error=str(exc),
                )
                probes.append(
                    make_probe(
                        "phase",
                        phase.label,
                        sharpe_ratio=float("-inf"),
                        max_drawdown=-1.0,
                        total_return=0.0,
                        thresholds={"min_sharpe": float("-inf"), "max_drawdown": -1.0},
                        detail={"error": str(exc)},
                    )
                )
                continue
            wr = compute_window_metrics(
                role=WindowRole.TEST,
                start=phase.start,
                end=phase.end,
                result=result,
            )
            probes.append(
                make_probe(
                    "phase",
                    phase.label,
                    sharpe_ratio=wr.metrics.sharpe_ratio,
                    max_drawdown=wr.metrics.max_drawdown,
                    total_return=wr.metrics.total_return,
                    thresholds={"min_sharpe": -10.0, "max_drawdown": -0.50},
                    detail={"start": phase.start.isoformat(), "end": phase.end.isoformat()},
                )
            )

        return probes

    def _pbo_matrix(self) -> tuple[list[list[float]] | None, str | None]:
        """PBO/CSCV 输入矩阵:IS 阶段各竞争 trial 的日收益。

        返回 ``(matrix, None)`` 或 ``(None, 跳过原因)``。CSCV 语义要求
        行 = 竞争配置、列 = 同一观测轴 —— IS 各 trial 在同一
        ``[train_start, train_end]`` 上回测,天然满足;<2 个可比 trial
        或行不等长(理论上仅数据面异常)即跳过,原因具名上报。
        """
        rows = [
            list(returns)
            for returns in self._in_sample_returns.values()
            if returns
        ]
        if len(rows) < 2:
            reason = (
                "fewer than 2 comparable in-sample trials "
                f"(n={len(rows)};walk-forward 窗口序列不再充当 trials,"
                "见 issue #244)"
            )
            return None, reason
        n_obs = len(rows[0])
        if n_obs == 0 or any(len(r) != n_obs for r in rows):
            return None, "non-rectangular in-sample returns matrix"
        return rows, None

    def _build_stat_report(
        self,
        *,
        best_returns: list[float],
    ) -> StatisticalReport:
        """计算统计修正报告(DSR / PSR / PBO / bootstrap CI)。

        PBO 输入见 :meth:`_pbo_matrix`;跳过时 ``pbo=0.0`` 并把原因写进
        ``methodology_notes``(fail-visible,不静默)。
        """
        matrix, pbo_skip_reason = self._pbo_matrix()
        notes = (
            "DSR=Deflated Sharpe (Bailey & López de Prado 2014); "
            "PSR=Probabilistic Sharpe (López de Prado 2012); "
            "PBO=CSCV (Bailey et al. 2017); "
            "CI=stationary bootstrap (Politis & Romano 1994)."
        )
        if pbo_skip_reason is not None:
            notes += f" PBO skipped: {pbo_skip_reason}"
        n_trials = max(1, len(self.trials))
        (
            dsr,
            psr,
            pbo,
            sharpe_low,
            sharpe_high,
            mdd_low,
            mdd_high,
        ) = build_statistical_report(
            best_trial_returns=best_returns,
            all_trial_returns_matrix=matrix,
            n_trials=n_trials,
            benchmark_sharpe=0.0,
            risk_free_annual=0.03,
            bootstrap_seed=self.experiment.plan.random_seed,
        )
        return StatisticalReport(
            deflated_sharpe_ratio=dsr,
            probabilistic_sharpe_ratio=psr,
            pbo=pbo,
            bootstrap_sharpe_ci_low=sharpe_low,
            bootstrap_sharpe_ci_high=sharpe_high,
            bootstrap_mdd_ci_low=mdd_low,
            bootstrap_mdd_ci_high=mdd_high,
            n_trials=n_trials,
            methodology_notes=notes,
        )

    def _evaluate_oos_gate(
        self,
        agg_oos: WindowMetrics,
        stat: StatisticalReport,
        thr: AcceptanceThresholds,
    ) -> bool:
        """OOS 门判定。"""
        return (
            agg_oos.sharpe_ratio >= thr.min_oos_sharpe
            and abs(agg_oos.max_drawdown) <= thr.max_oos_drawdown
            and agg_oos.calmar_ratio >= thr.min_oos_calmar
            and agg_oos.information_ratio >= thr.min_oos_information_ratio
            and not (thr.min_pbo_pass and stat.pbo > thr.max_pbo)
            and stat.deflated_sharpe_ratio >= thr.min_deflated_sharpe
            and stat.probabilistic_sharpe_ratio >= thr.min_probabilistic_sharpe
        )

    def _format_oos_failure(
        self,
        agg_oos: WindowMetrics,
        stat: StatisticalReport,
        thr: AcceptanceThresholds,
    ) -> str:
        reasons: list[str] = []
        if agg_oos.sharpe_ratio < thr.min_oos_sharpe:
            reasons.append(
                f"oos_sharpe={agg_oos.sharpe_ratio:.3f} < {thr.min_oos_sharpe:.3f}"
            )
        if abs(agg_oos.max_drawdown) > thr.max_oos_drawdown:
            reasons.append(
                f"|oos_mdd|={abs(agg_oos.max_drawdown):.3f} > {thr.max_oos_drawdown:.3f}"
            )
        if agg_oos.calmar_ratio < thr.min_oos_calmar:
            reasons.append(
                f"oos_calmar={agg_oos.calmar_ratio:.3f} < {thr.min_oos_calmar:.3f}"
            )
        if agg_oos.information_ratio < thr.min_oos_information_ratio:
            reasons.append(
                f"oos_ir={agg_oos.information_ratio:.3f} < {thr.min_oos_information_ratio:.3f}"
            )
        if thr.min_pbo_pass and stat.pbo > thr.max_pbo:
            reasons.append(
                f"pbo={stat.pbo:.3f} > {thr.max_pbo:.3f}"
            )
        if stat.deflated_sharpe_ratio < thr.min_deflated_sharpe:
            reasons.append(
                f"dsr={stat.deflated_sharpe_ratio:.3f} < {thr.min_deflated_sharpe:.3f}"
            )
        if stat.probabilistic_sharpe_ratio < thr.min_probabilistic_sharpe:
            reasons.append(
                f"psr={stat.probabilistic_sharpe_ratio:.3f} < {thr.min_probabilistic_sharpe:.3f}"
            )
        return "; ".join(reasons) if reasons else "no specific reason"

    async def unseal_final_test(
        self,
        *,
        config_overrides: dict[str, object] | None = None,
    ) -> ExperimentVerdict:
        """一次性揭盲最终冻结测试集。

        必须在 ``run_walk_forward`` 之后调用,且只能调用一次。返回 ``ExperimentVerdict``:
        * ``VALIDATED_OOS`` —— 揭盲后通过最终门;
        * ``REJECTED`` —— 揭盲后未通过门(已经"使用"了揭盲,无法重来)。

        issue #310:best trial 的 OOS 门被拒**不阻止揭盲**(#245 决策 A 下
        流程如此是设计使然,不硬阻断、不改状态机与一次性语义),但揭盲前打
        具名 WARNING ``validation.unseal_with_rejected_trials`` 并把警示写入
        ``experiment.notes``,明示 final test 揭盲机会消耗在被拒配置上——
        ``validated_oos`` 只代表 OOS 流程完成,不代表假设获支持(结论语义
        见 :func:`derive_oos_outcome`)。
        """
        if self.best_trial_id is None:
            return ExperimentVerdict(
                experiment_id=self.experiment.experiment_id,
                status=ExperimentStatus.REJECTED,
                reason="no best trial selected before unseal",
            )

        best = next(t for t in self.trials if t.trial_id == self.best_trial_id)
        oos_rejected = best.status is TrialStatus.REJECTED
        async with self._state_lock:
            experiment = mark_final_test_unsealed(self.experiment)
            if oos_rejected:
                experiment = append_note(experiment, _unseal_rejected_note(best))
                logger.warning(
                    "validation.unseal_with_rejected_trials",
                    experiment_id=experiment.experiment_id,
                    best_trial_id=best.trial_id,
                    oos_failure_reason=best.failure_reason,
                )
            self.experiment = experiment
        plan = self.experiment.plan

        try:
            result = await self.trial_runner(
                start=plan.test_start,
                end=plan.test_end,
                params=best.parameters,
                config_overrides=config_overrides,
            )
        except Exception as exc:
            logger.error(
                "validation.final_test_failed",
                experiment_id=self.experiment.experiment_id,
                error=str(exc),
            )
            self.experiment = transition_status(
                self.experiment,
                ExperimentStatus.REJECTED,
                rejection_reason=f"final_test_error: {exc}",
            )
            return ExperimentVerdict(
                experiment_id=self.experiment.experiment_id,
                status=ExperimentStatus.REJECTED,
                reason=f"final test error: {exc}",
                best_trial_id=best.trial_id,
            )

        unsealed_metrics = compute_window_metrics(
            role=WindowRole.TEST,
            start=plan.test_start,
            end=plan.test_end,
            result=result,
        )

        # 揭盲门:用 OOS 同样的门,但更严格(已经"消耗"了揭盲机会)
        thr = self.experiment.thresholds
        # 揭盲后的 Sharpe 必须在 bootstrap CI 下界之上才认账
        if best.statistical_report:
            lower_ci = best.statistical_report.bootstrap_sharpe_ci_low
        else:
            lower_ci = float("-inf")

        passes = (
            unsealed_metrics.metrics.sharpe_ratio >= thr.min_oos_sharpe
            and abs(unsealed_metrics.metrics.max_drawdown) <= thr.max_oos_drawdown
            and unsealed_metrics.metrics.sharpe_ratio >= lower_ci
        )

        if passes:
            self.experiment = transition_status(
                self.experiment, ExperimentStatus.VALIDATED_OOS
            )
            return ExperimentVerdict(
                experiment_id=self.experiment.experiment_id,
                status=ExperimentStatus.VALIDATED_OOS,
                reason="passed final test unseal",
                best_trial_id=best.trial_id,
                unsealed_metrics=unsealed_metrics.metrics,
                final_statistical_report=best.statistical_report,
            )
        self.experiment = transition_status(
            self.experiment,
            ExperimentStatus.REJECTED,
            rejection_reason=(
                f"final_test_fail: sharpe={unsealed_metrics.metrics.sharpe_ratio:.3f} "
                f"< {thr.min_oos_sharpe:.3f} or mdd="
                f"{unsealed_metrics.metrics.max_drawdown:.3f} "
                f"exceeds {thr.max_oos_drawdown:.3f} or below bootstrap lower CI {lower_ci:.3f}"
            ),
        )
        return ExperimentVerdict(
            experiment_id=self.experiment.experiment_id,
            status=ExperimentStatus.REJECTED,
            reason=(
                f"final test metrics below threshold "
                f"(sharpe={unsealed_metrics.metrics.sharpe_ratio:.3f}, "
                f"mdd={unsealed_metrics.metrics.max_drawdown:.3f})"
            ),
            best_trial_id=best.trial_id,
            unsealed_metrics=unsealed_metrics.metrics,
            final_statistical_report=best.statistical_report,
        )

    def reject(self, reason: str) -> ExperimentVerdict:
        """主动拒绝实验(用于失败 trial / 预算耗尽)。"""
        self.experiment = transition_status(
            self.experiment,
            ExperimentStatus.REJECTED,
            rejection_reason=reason,
        )
        return ExperimentVerdict(
            experiment_id=self.experiment.experiment_id,
            status=ExperimentStatus.REJECTED,
            reason=reason,
            best_trial_id=self.best_trial_id,
        )


# ---------------------------------------------------------------------------
# OOS 指标聚合
# ---------------------------------------------------------------------------

_UNSEAL_REJECTED_NOTE_PREFIX = "unseal_with_rejected_trials"


def _unseal_rejected_note(best: TrialRecord) -> str:
    """best trial OOS 被拒仍揭盲时的实验备注文案(issue #310)。

    与 structlog ``validation.unseal_with_rejected_trials`` 同名前缀,随
    ``experiment.notes`` 持久化,明示 final test 揭盲机会消耗在被拒配置上。
    """
    reason = best.failure_reason or "oos gate rejected"
    return (
        f"{_UNSEAL_REJECTED_NOTE_PREFIX}: best trial {best.trial_id} 的 OOS 门"
        f"未通过({reason});final test 揭盲机会消耗在被拒配置上——"
        "validated_oos 只代表 OOS 流程完成,不代表假设获支持(issue #310)"
    )


def _aggregate_oos_metrics(windows: Sequence[WindowResult]) -> WindowMetrics:
    """把多个 walk-forward 窗口的指标聚合成一个 OOS 指标。

    聚合规则:
    * ``total_return`` —— 复合累乘 ``(1+r1)(1+r2)... - 1``;
    * ``sharpe / sortino / calmar / IR / mdd / var / cvar`` —— 加权平均(按窗口长度);
    * ``trade_count / turnover`` —— 求和;
    * ``monthly_win_rate`` —— 加权平均。
    """
    if not windows:
        return WindowMetrics(
            role=WindowRole.TEST,
            start=date(1970, 1, 1),
            end=date(1970, 1, 1),
        )
    if len(windows) == 1:
        return windows[0].metrics

    # 复合收益
    compounded = 1.0
    for w in windows:
        compounded *= (1.0 + w.metrics.total_return)
    total_ret = compounded - 1.0

    # 加权平均(按窗口长度)
    total_days = sum(
        max(1, (w.metrics.end - w.metrics.start).days) for w in windows
    )

    def weighted(getter: Callable[[WindowMetrics], float]) -> float:
        return sum(
            getter(w.metrics) * max(1, (w.metrics.end - w.metrics.start).days)
            for w in windows
        ) / total_days

    start = min(w.metrics.start for w in windows)
    end = max(w.metrics.end for w in windows)

    return WindowMetrics(
        role=WindowRole.TEST,
        start=start,
        end=end,
        total_return=total_ret,
        annualized_return=weighted(lambda m: m.annualized_return),
        sharpe_ratio=weighted(lambda m: m.sharpe_ratio),
        sortino_ratio=weighted(lambda m: m.sortino_ratio),
        calmar_ratio=weighted(lambda m: m.calmar_ratio),
        information_ratio=weighted(lambda m: m.information_ratio),
        max_drawdown=weighted(lambda m: m.max_drawdown),
        max_drawdown_duration=int(weighted(lambda m: float(m.max_drawdown_duration))),
        monthly_win_rate=weighted(lambda m: m.monthly_win_rate),
        var_95=weighted(lambda m: m.var_95),
        cvar_95=weighted(lambda m: m.cvar_95),
        trade_count=sum(w.metrics.trade_count for w in windows),
        turnover=sum(w.metrics.turnover for w in windows),
        benchmark_return=weighted(lambda m: m.benchmark_return),
        excess_return=weighted(lambda m: m.excess_return),
    )


# ---------------------------------------------------------------------------
# 简单候选生成器(参数网格)
# ---------------------------------------------------------------------------


def grid_candidates(
    param_grid: Mapping[str, Sequence[object]],
) -> list[dict[str, object]]:
    """参数网格笛卡尔积 → 候选列表。

    用于在 ``run_in_sample`` 之前生成候选参数。
    """
    keys = list(param_grid.keys())
    if not keys:
        return [{}]
    result: list[dict[str, object]] = [{}]
    for key in keys:
        values = param_grid[key]
        new_result: list[dict[str, object]] = []
        for r in result:
            for v in values:
                new_result.append({**r, key: v})
        result = new_result
    return result
