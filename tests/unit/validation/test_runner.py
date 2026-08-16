"""样本外验证 runner 编排测试。

用 fake trial_runner(返回合成 BacktestResult)端到端测试:
* IS 阶段选最优 trial;
* OOS 阶段聚合 walk-forward 窗口指标;
* 统计报告填充;
* 揭盲门一次性;
* 失败也算试验;
* 试验预算耗尽 → REJECTED;
* 属性:同种子 / 同版本结果一致。
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal

import pytest

from finboard_backtest.result import BacktestResult
from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    ResearchExperiment,
    RobustnessPlan,
    TrialStatus,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    new_experiment,
)
from finboard_backtest.validation.runner import (
    ValidationRunner,
    grid_candidates,
)


def _stamp() -> VersionStamp:
    return VersionStamp(
        matching_model_version="v2",
        asset_rules_version="v1",
        factor_version=None,
        dataset_versions={},
        selection_config={},
        strategy_kind="ma_cross",
    )


def _plan(
    *,
    trial_budget: int = 20,
    train_window_days: int = 21,
    test_window_days: int = 10,
    step_days: int = 10,
) -> ValidationPlan:
    return ValidationPlan(
        mode=ValidationMode.ROLLING,
        train_start=date(2020, 1, 1),
        train_end=date(2020, 6, 30),
        validation_start=date(2020, 7, 1),
        validation_end=date(2020, 12, 31),
        test_start=date(2021, 1, 1),
        test_end=date(2021, 6, 30),
        train_window_days=train_window_days,
        test_window_days=test_window_days,
        step_days=step_days,
        trial_budget=trial_budget,
    )


def _make_equity_curve(
    start: date,
    end: date,
    mu: float = 0.001,
    sigma: float = 0.01,
    seed: int = 0,
) -> list[tuple[date, Decimal]]:
    """生成一条合成权益曲线。"""
    rng = random.Random(seed)
    curve: list[tuple[date, Decimal]] = []
    cur = start
    equity = 1.0
    while cur <= end:
        curve.append((cur, Decimal(str(round(equity, 6)))))
        r = rng.gauss(mu, sigma)
        equity *= 1 + r
        cur += timedelta(days=1)
    return curve


class FakeTrialRunner:
    """合成的 TrialRunner 实现。

    根据 ``params["alpha"]`` 决定收益强弱:alpha 越高,生成的权益曲线 mu 越大。
    """

    def __init__(self, *, seed: int = 42, fail_on: set[int] | None = None) -> None:
        self.seed = seed
        self.fail_on = fail_on or set()
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        start: date,
        end: date,
        params: Mapping[str, object],
        config_overrides: Mapping[str, object] | None = None,
    ) -> BacktestResult:
        idx = len(self.calls)
        self.calls.append(
            {
                "start": start,
                "end": end,
                "params": dict(params),
                "config_overrides": dict(config_overrides or {}),
            }
        )
        if idx in self.fail_on:
            raise RuntimeError(f"fake failure on call {idx}")
        alpha = float(params.get("alpha", 0.001))  # type: ignore[arg-type]
        seed = self.seed + idx + int(start.toordinal())
        curve = _make_equity_curve(start, end, mu=alpha, sigma=0.01, seed=seed)
        bench = _make_equity_curve(start, end, mu=0.0003, sigma=0.008, seed=seed + 1000)
        return BacktestResult(
            equity_curve=curve,
            benchmark_curve=bench,
            fills=[],
            orders=[],
            selection_snapshots=[],
            total_return=0.0,
            annualized_return=0.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
            win_rate=0.0,
            trade_count=0,
            turnover=0.0,
            commission_paid=Decimal("0"),
            stamp_tax_paid=Decimal("0"),
            benchmark_return=0.0,
            excess_return=0.0,
            start_date=start,
            end_date=end,
            initial_capital=Decimal("100000"),
            final_equity=curve[-1][1] if curve else Decimal("100000"),
        )


def _make_experiment(
    *,
    trial_budget: int = 10,
    thresholds: AcceptanceThresholds | None = None,
) -> ResearchExperiment:
    return new_experiment(
        hypothesis="测试假设:均线交叉策略在 ETF 上有 alpha",
        version_stamp=_stamp(),
        plan=_plan(trial_budget=trial_budget),
        thresholds=thresholds or AcceptanceThresholds(
            min_in_sample_sharpe=-10.0,
            min_oos_sharpe=-10.0,
            max_oos_drawdown=1.0,
            min_oos_calmar=-100.0,
            min_oos_information_ratio=-100.0,
            min_pbo_pass=False,
            min_deflated_sharpe=-100.0,
            min_probabilistic_sharpe=0.0,
        ),
        robustness=RobustnessPlan(),
    )


class TestInSample:
    async def test_run_in_sample_records_all_trials(self) -> None:
        exp = _make_experiment(trial_budget=10)
        runner = ValidationRunner(experiment=exp, trial_runner=FakeTrialRunner())
        candidates = [{"alpha": 0.001}, {"alpha": 0.002}, {"alpha": 0.003}]
        trials = await runner.run_in_sample(candidates)
        assert len(trials) == 3
        assert all(t.status == TrialStatus.SELECTED for t in trials)
        assert runner.experiment.trials_used == 3
        assert runner.experiment.status == ExperimentStatus.IN_SAMPLE

    async def test_failed_trial_still_recorded(self) -> None:
        exp = _make_experiment(trial_budget=10)
        fake = FakeTrialRunner(fail_on={1})
        runner = ValidationRunner(experiment=exp, trial_runner=fake)
        candidates = [{"alpha": 0.001}, {"alpha": 0.002}, {"alpha": 0.003}]
        trials = await runner.run_in_sample(candidates)
        assert len(trials) == 3
        assert trials[1].status == TrialStatus.FAILED
        assert trials[1].failure_reason is not None
        assert runner.experiment.trials_used == 3  # 失败也算试验

    async def test_budget_exhaustion(self) -> None:
        exp = _make_experiment(trial_budget=2)
        runner = ValidationRunner(experiment=exp, trial_runner=FakeTrialRunner())
        candidates = [{"alpha": 0.001}, {"alpha": 0.002}, {"alpha": 0.003}]
        trials = await runner.run_in_sample(candidates)
        assert len(trials) == 2  # 只跑了前 2 个
        assert runner.experiment.trials_used == 2
        assert not runner.experiment.can_run_trial()

    async def test_best_trial_selected_by_sharpe(self) -> None:
        exp = _make_experiment(trial_budget=10)
        runner = ValidationRunner(experiment=exp, trial_runner=FakeTrialRunner(seed=99))
        candidates = [{"alpha": -0.001}, {"alpha": 0.002}, {"alpha": 0.001}]
        await runner.run_in_sample(candidates)
        assert runner.best_trial_id is not None
        best = next(t for t in runner.trials if t.trial_id == runner.best_trial_id)
        # 最高 alpha 对应最高 Sharpe
        assert best.parameters["alpha"] == 0.002
        assert best.in_sample_metrics is not None
        assert best.in_sample_metrics.sharpe_ratio > 0


class TestWalkForward:
    async def test_run_walk_forward_after_in_sample(self) -> None:
        exp = _make_experiment()
        runner = ValidationRunner(experiment=exp, trial_runner=FakeTrialRunner())
        candidates = [{"alpha": 0.002}]
        await runner.run_in_sample(candidates)
        updated = await runner.run_walk_forward()
        assert updated is not None
        # 应该有 walk-forward 窗口指标
        assert len(updated.walk_forward_windows) >= 1
        # 应该有统计报告
        assert updated.statistical_report is not None
        # 应该有稳健性 probe(至少 1 个邻域 + 0 个市场阶段,因为 2020 不在 stress_phase 内)
        # 但 _run_robustness_probes 会基于 walk_forward_windows 生成一个 neighbourhood probe

    async def test_walk_forward_requires_in_sample_first(self) -> None:
        exp = _make_experiment()
        runner = ValidationRunner(experiment=exp, trial_runner=FakeTrialRunner())
        # 没有 best_trial_id → 返回 None
        result = await runner.run_walk_forward()
        assert result is None


class TestUnseal:
    async def test_unseal_only_once(self) -> None:
        exp = _make_experiment()
        runner = ValidationRunner(experiment=exp, trial_runner=FakeTrialRunner())
        candidates = [{"alpha": 0.002}]
        await runner.run_in_sample(candidates)
        await runner.run_walk_forward()
        verdict1 = await runner.unseal_final_test()
        # 第二次揭盲应该 raise(state machine 拒绝)
        with pytest.raises(ValueError, match="already unsealed"):
            await runner.unseal_final_test()
        # 第一次揭盲返回裁决
        assert verdict1.status in (ExperimentStatus.VALIDATED_OOS, ExperimentStatus.REJECTED)


class TestReproducibility:
    async def test_same_seed_same_result(self) -> None:
        """同种子 + 同参数 → 同 IS Sharpe(属性测试)。"""
        exp1 = _make_experiment()
        runner1 = ValidationRunner(experiment=exp1, trial_runner=FakeTrialRunner(seed=7))
        exp2 = _make_experiment()
        runner2 = ValidationRunner(experiment=exp2, trial_runner=FakeTrialRunner(seed=7))
        candidates = [{"alpha": 0.001}]
        await runner1.run_in_sample(candidates)
        await runner2.run_in_sample(candidates)
        assert runner1.trials[0].in_sample_metrics is not None
        assert runner2.trials[0].in_sample_metrics is not None
        s1 = runner1.trials[0].in_sample_metrics.sharpe_ratio
        s2 = runner2.trials[0].in_sample_metrics.sharpe_ratio
        assert s1 == pytest.approx(s2, abs=1e-9)


class TestGridCandidates:
    def test_cartesian_product(self) -> None:
        grid: dict[str, list[int]] = {"window": [5, 10, 20], "threshold": [1, 2]}
        candidates = grid_candidates(grid)
        assert len(candidates) == 6
        # 每个候选应该有 2 个键
        for c in candidates:
            assert set(c.keys()) == {"window", "threshold"}

    def test_empty_grid(self) -> None:
        candidates = grid_candidates({})
        assert candidates == [{}]
