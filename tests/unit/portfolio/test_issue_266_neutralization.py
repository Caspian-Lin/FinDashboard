"""issue #266:风险因子中性化约束投影的数学正确性与降级路径。

投影语义:``|Σ_i (w_i - baseline_i)·f_i| <= limit``,只减仓、释放权重转现金;
本文件用已知解析解的小案例逐项断言投影前后暴露数值,并锁定
fail-closed(不可满足)与具名降级(暴露观测缺失)两条边界。
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from finboard_backtest.portfolio.builder import PortfolioBuildResult
from finboard_backtest.portfolio.contracts import (
    MAX_WEIGHT_EPSILON,
    RiskFactorLimit,
)
from finboard_backtest.portfolio.neutralization import (
    DEFAULT_MAX_ITERATIONS,
    NEUTRALIZATION_CONSTRAINT,
    NEUTRALIZATION_INACTIVE_WARNING,
    NEUTRALIZATION_SKIPPED_CONSTRAINT,
    FactorNeutralizationResult,
    neutralization_audit_rows,
    project_risk_factor_neutralization,
)


def _active_exposure(
    weights: dict[str, float],
    factor: dict[str, float],
    baseline: dict[str, float] | None = None,
) -> float:
    baseline = baseline or {}
    symbols = set(weights) | set(baseline)
    return sum(
        (weights.get(symbol, 0.0) - baseline.get(symbol, 0.0))
        * factor.get(symbol, 0.0)
        for symbol in symbols
    )


class TestSingleFactorAnalytic:
    def test_offender_only_projection_matches_closed_form(self) -> None:
        """2 资产对冲结构:解析解只缩 offender(A: 0.6→0.5),B 不动。

        w=[0.6, 0.4]、f=[1, -1]、limit=0.1:闭式解 s = 1 + (0.1-0.2)/0.6
        = 5/6 → A = 0.5;暴露 = 0.5 - 0.4 = 0.1,一步精确命中。
        """
        result = project_risk_factor_neutralization(
            {"A": 0.6, "B": 0.4},
            (RiskFactorLimit(factor="beta", max_active_exposure=0.1),),
            {"beta": {"A": 1.0, "B": -1.0}},
        )
        assert result.converged is True
        assert result.changed is True
        assert result.iterations == 1
        assert result.weights["A"] == pytest.approx(0.5, abs=1e-12)
        # 非 offender(对冲方向持仓)不被破坏 —— 全向量缩放的错误实现
        # 会把 B 从 0.4 砍到 0.2,这里锁定逐项语义。
        assert result.weights["B"] == pytest.approx(0.4, abs=1e-12)
        audit = result.audits[0]
        assert audit.enforced is True
        assert audit.before_exposure == pytest.approx(0.2, abs=1e-12)
        assert audit.after_exposure == pytest.approx(0.1, abs=1e-9)

    def test_negative_direction_violation_mirrors(self) -> None:
        """负向超限等价镜像处理:深亏方向持仓被同号缩放。"""
        result = project_risk_factor_neutralization(
            {"A": 0.3, "B": 0.5},
            (RiskFactorLimit(factor="beta", max_active_exposure=0.1),),
            {"beta": {"A": -1.0, "B": 0.5}},
        )
        # before = -0.3 + 0.25 = -0.05 → 已满足,不动。
        assert result.changed is False
        assert result.converged is True
        assert result.weights == {"A": pytest.approx(0.3), "B": pytest.approx(0.5)}

        violated = project_risk_factor_neutralization(
            {"A": 0.3, "B": 0.5},
            (RiskFactorLimit(factor="beta", max_active_exposure=0.05),),
            {"beta": {"A": -1.0, "B": 1.0}},
        )
        # before = -0.3 + 0.5 = +0.2 → 缩 B(唯一 offender):B* = 0.35,
        # after = -0.3 + 0.35 = 0.05。
        assert violated.converged is True
        assert violated.weights["B"] == pytest.approx(0.35, abs=1e-12)
        assert violated.weights["A"] == pytest.approx(0.3, abs=1e-12)
        assert abs(violated.audits[0].after_exposure or 0.0) <= 0.05 + 1e-9

    def test_already_within_limits_is_noop(self) -> None:
        result = project_risk_factor_neutralization(
            {"A": 0.2, "B": 0.2},
            (RiskFactorLimit(factor="beta", max_active_exposure=0.5),),
            {"beta": {"A": 1.0, "B": 1.0}},
        )
        assert result.converged is True
        assert result.changed is False
        assert result.iterations == 0
        assert result.audits[0].before_exposure == pytest.approx(0.4, abs=1e-12)
        assert result.audits[0].iterations == 0

    def test_empty_limits_or_weights_return_identity(self) -> None:
        empty_limits = project_risk_factor_neutralization(
            {"A": 0.5}, (), {"beta": {"A": 1.0}}
        )
        assert empty_limits.converged is True
        assert empty_limits.changed is False
        assert empty_limits.audits == ()
        empty_weights = project_risk_factor_neutralization(
            {}, (RiskFactorLimit(factor="beta", max_active_exposure=0.1),), {}
        )
        assert empty_weights.converged is True
        assert empty_weights.weights == {}


class TestMultiFactor:
    def test_multi_factor_alternating_projection_converges(self) -> None:
        """两个因子交替投影至全部满足,迭代数有界且暴露全部在限内。"""
        weights = {"A": 0.5, "B": 0.3, "C": 0.2}
        limits = (
            RiskFactorLimit(factor="market_beta", max_active_exposure=0.15),
            RiskFactorLimit(factor="size", max_active_exposure=0.1),
        )
        exposures = {
            "market_beta": {"A": 1.2, "B": 1.0, "C": 0.6},
            "size": {"A": -0.8, "B": 0.2, "C": 1.1},
        }
        result = project_risk_factor_neutralization(weights, limits, exposures)
        assert result.converged is True
        assert 1 <= result.iterations < DEFAULT_MAX_ITERATIONS
        for audit in result.audits:
            assert audit.enforced is True
            assert abs(audit.after_exposure or 0.0) <= audit.limit + 1e-9
            assert audit.after_exposure is not None
            assert abs(audit.after_exposure) <= abs(audit.before_exposure or 0.0) + 1e-12
        # 只减仓:任何权重都不增。
        for symbol, weight in result.weights.items():
            assert weight <= weights[symbol] + 1e-12
            assert weight >= -MAX_WEIGHT_EPSILON
        # 逐因子复核(独立口径)。
        for factor, limit in (
            ("market_beta", 0.15),
            ("size", 0.1),
        ):
            exposure = _active_exposure(result.weights, exposures[factor])
            assert abs(exposure) <= limit + 1e-9

    def test_projection_keeps_monotone_weights_across_rounds(self) -> None:
        """多轮投影的中间态也是单调不增(逐次缩放闭式解 ∈ [0,1])。"""
        weights = {"A": 0.6, "B": 0.25, "C": 0.15}
        limits = (
            RiskFactorLimit(factor="f1", max_active_exposure=0.05),
            RiskFactorLimit(factor="f2", max_active_exposure=0.05),
        )
        exposures = {
            "f1": {"A": 1.0, "B": 0.9, "C": 0.4},
            "f2": {"A": 0.2, "B": 1.0, "C": 1.0},
        }
        result = project_risk_factor_neutralization(weights, limits, exposures)
        assert result.converged is True
        total_before = sum(weights.values())
        total_after = sum(result.weights.values())
        assert total_after < total_before
        assert result.audits[0].iterations >= 1


class TestBaselineSemantics:
    def test_cash_baseline_default_is_zero_active(self) -> None:
        result = project_risk_factor_neutralization(
            {"A": 0.5},
            (RiskFactorLimit(factor="beta", max_active_exposure=0.2),),
            {"beta": {"A": 1.0}},
        )
        # active = w = 0.5 → 缩到 0.2。
        assert result.converged is True
        assert result.weights["A"] == pytest.approx(0.2, abs=1e-12)

    def test_baseline_only_symbols_count_into_exposure(self) -> None:
        """基准独有标的贡献 (0 - b)·f 常数项,度量域为持仓与基准的并集。"""
        result = project_risk_factor_neutralization(
            {"A": 0.6, "B": 0.4},
            (RiskFactorLimit(factor="beta", max_active_exposure=0.1),),
            {"beta": {"A": 1.0, "B": -1.0, "C": 1.0}},
            baseline_weights={"C": 0.5},
        )
        # before = 0.6 - 0.4 - 0.5 = -0.3; offender = B(同号缩小使暴露向 0)
        # s = 1 + (0.1 - 0.3)/0.4 = 0.5 → B = 0.2,after = -0.1。
        assert result.converged is True
        assert result.audits[0].before_exposure == pytest.approx(-0.3, abs=1e-12)
        assert result.audits[0].after_exposure == pytest.approx(-0.1, abs=1e-9)
        assert result.weights["A"] == pytest.approx(0.6, abs=1e-12)
        assert result.weights["B"] == pytest.approx(0.2, abs=1e-12)
        # 输出键集与输入一致:基准独有标的不得伪装成 0 权重目标。
        assert set(result.weights) == {"A", "B"}

    def test_baseline_relative_holding_scales_toward_baseline(self) -> None:
        """基准相对语义:持仓低配(w < b)时减权会加深低配,不可用于修复。"""
        # A 持仓 0.2、基准 0.5:active = -0.3,负向已超 -0.1。
        # 该方向的「修复」需要加仓到 0.4,只减仓做不到 → fail-closed。
        result = project_risk_factor_neutralization(
            {"A": 0.2},
            (RiskFactorLimit(factor="beta", max_active_exposure=0.1),),
            {"beta": {"A": 1.0}},
            baseline_weights={"A": 0.5},
        )
        assert result.converged is False
        assert result.audits[0].after_exposure is not None
        assert abs(result.audits[0].after_exposure) > 0.1 + 1e-9


class TestFailClosed:
    def test_baseline_dominated_exposure_is_infeasible(self) -> None:
        """减到 0 权重仍超限(基准主导)→ 不收敛,调用方必须失败关闭。"""
        result = project_risk_factor_neutralization(
            {"A": 0.3},
            (RiskFactorLimit(factor="beta", max_active_exposure=0.1),),
            {"beta": {"A": 1.0}},
            baseline_weights={"A": 0.5},
        )
        assert result.converged is False
        audit = result.audits[0]
        assert audit.enforced is True
        assert abs(audit.after_exposure or 0.0) > audit.limit

    def test_offsetting_structure_without_actionable_offender(self) -> None:
        """offender 全是不持仓方向(无法通过减权改善)→ fail-closed。

        w=0.4、b=0.5(低配):active = -0.1,signed 暴露 = +0.1(镜像后);
        减权使暴露更接近 b·f = 0.5,只会更糟 → 无进展。
        """
        result = project_risk_factor_neutralization(
            {"A": 0.4},
            (RiskFactorLimit(factor="beta", max_active_exposure=0.05),),
            {"beta": {"A": -1.0}},
            baseline_weights={"A": 0.5},
        )
        assert result.converged is False

    def test_invalid_parameters_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_iterations"):
            project_risk_factor_neutralization(
                {"A": 0.5},
                (RiskFactorLimit(factor="beta", max_active_exposure=0.1),),
                {"beta": {"A": 1.0}},
                max_iterations=0,
            )
        with pytest.raises(ValueError, match="tolerance"):
            project_risk_factor_neutralization(
                {"A": 0.5},
                (RiskFactorLimit(factor="beta", max_active_exposure=0.1),),
                {"beta": {"A": 1.0}},
                tolerance=0.0,
            )


class TestMissingObservationDegradation:
    def test_missing_held_observation_skips_with_named_warning(self) -> None:
        """持仓标的缺暴露观测 → 跳过约束 + 具名 warning,不静默失效。"""
        result = project_risk_factor_neutralization(
            {"A": 0.6, "B": 0.4},
            (RiskFactorLimit(factor="industry_exposure", max_active_exposure=0.1),),
            {"industry_exposure": {"A": 1.0}},  # B 缺失
        )
        assert result.converged is True  # 跳过不是失败
        assert result.changed is False
        audit = result.audits[0]
        assert audit.enforced is False
        assert audit.before_exposure is None
        assert audit.after_exposure is None
        assert audit.warning is not None
        assert audit.warning.startswith(NEUTRALIZATION_INACTIVE_WARNING)
        assert "industry_exposure" in audit.warning
        assert "1/2" in audit.warning  # 2 个持仓中 1 个缺失

    def test_baseline_only_missing_observation_also_skips(self) -> None:
        """基准独有标的缺观测同样使约束不可测量 → 跳过并具名。"""
        result = project_risk_factor_neutralization(
            {"A": 0.5},
            (RiskFactorLimit(factor="beta", max_active_exposure=0.1),),
            {"beta": {"A": 1.0}},
            baseline_weights={"B": 0.3},
        )
        assert result.audits[0].enforced is False
        assert result.audits[0].warning is not None

    def test_none_and_nonfinite_observations_treated_as_missing(self) -> None:
        result = project_risk_factor_neutralization(
            {"A": 0.5, "B": 0.5},
            (RiskFactorLimit(factor="beta", max_active_exposure=0.1),),
            {"beta": {"A": None, "B": float("nan")}},  # type: ignore[dict-item]
        )
        assert result.audits[0].enforced is False

    def test_mixed_enforced_and_skipped_factors(self) -> None:
        """一个因子可执行、一个因子缺观测:只跳过缺失者,可见降级。"""
        result = project_risk_factor_neutralization(
            {"A": 0.6, "B": 0.4},
            (
                RiskFactorLimit(factor="market_beta", max_active_exposure=0.1),
                RiskFactorLimit(factor="industry", max_active_exposure=0.1),
            ),
            {"market_beta": {"A": 1.0, "B": -1.0}},
        )
        by_factor = {audit.factor: audit for audit in result.audits}
        assert by_factor["market_beta"].enforced is True
        assert by_factor["industry"].enforced is False
        assert by_factor["industry"].warning is not None
        assert result.converged is True


class TestAuditRows:
    def test_audit_rows_shape_and_names(self) -> None:
        result = project_risk_factor_neutralization(
            {"A": 0.6, "B": 0.4},
            (
                RiskFactorLimit(factor="market_beta", max_active_exposure=0.1),
                RiskFactorLimit(factor="industry", max_active_exposure=0.2),
            ),
            {"market_beta": {"A": 1.0, "B": -1.0}},
        )
        rows = neutralization_audit_rows(result)
        by_constraint = {row[0]: row for row in rows}
        assert NEUTRALIZATION_CONSTRAINT in by_constraint
        assert NEUTRALIZATION_SKIPPED_CONSTRAINT in by_constraint

        enforced = by_constraint[NEUTRALIZATION_CONSTRAINT]
        _constraint, symbol, before, after, limit, passed, reason = enforced
        assert symbol == "market_beta"
        assert before == pytest.approx(0.2, abs=1e-12)
        assert after <= 0.1 + 1e-9
        assert limit == 0.1
        assert passed is True
        assert "factor_iterations=1" in reason

        skipped = by_constraint[NEUTRALIZATION_SKIPPED_CONSTRAINT]
        assert skipped[1] == "industry"
        assert skipped[5] is False  # passed=False 保留可见
        assert skipped[6].startswith(NEUTRALIZATION_INACTIVE_WARNING)


class TestBuilderIntegration:
    """build_portfolio 内的端到端行为(fail-closed / 复核语义)。"""

    def _build(self, **overrides) -> PortfolioBuildResult:
        from datetime import date

        from finboard_backtest.portfolio.builder import (
            PortfolioBuildInput,
            build_portfolio,
        )
        from finboard_backtest.portfolio.contracts import (
            PortfolioConstraints,
            Signal,
        )
        from finboard_backtest.portfolio.covariance import CovarianceEstimate

        signals = tuple(
            Signal(symbol=symbol, score=1.0, timestamp=date(2025, 3, 14), strategy_id="t")
            for symbol in ("A", "B", "C")
        )
        cov = CovarianceEstimate(
            matrix=np.array(
                [[0.04, 0.006, 0.002], [0.006, 0.01, 0.003], [0.002, 0.003, 0.02]]
            ),
            tickers=["A", "B", "C"],
            shrinkage=0.0,
            n_observations=252,
        )
        constraints = PortfolioConstraints(
            max_weight_per_asset=1.0,
            max_weight_per_sleeve=1.0,
            min_cash_buffer=0.0,
            risk_factor_limits=(
                RiskFactorLimit(factor="beta", max_active_exposure=0.1),
            ),
        )
        inputs: dict[str, object] = {
            "factor_exposures": {"beta": {"A": 1.0, "B": 1.0, "C": 1.0}},
        }
        inputs.update(overrides)
        build_input = PortfolioBuildInput(
            signals=signals,
            method="equal_weight",
            constraints=constraints,
            covariance=cov,
            **inputs,  # type: ignore[arg-type]
        )

        return build_portfolio(build_input)

    def test_projection_runs_after_all_other_stages(self) -> None:
        result = self._build()
        rows = [
            item for item in result.adjustments if item.constraint == NEUTRALIZATION_CONSTRAINT
        ]
        assert len(rows) == 1
        row = rows[0]
        assert row.passed is True
        # 等权 1/3 x 暴露 1.0 → before = 1.0,缩到 0.1。
        assert row.before_value == pytest.approx(1.0, abs=1e-9)
        assert row.after_value <= 0.1 + 1e-9
        # 释放的权重转现金:cash_buffer 补足差额。
        gross = sum(result.target_after_constraints.weights.values())
        assert gross <= 1.0 + 1e-9
        assert result.target_after_constraints.cash_buffer >= 1.0 - gross - 1e-9

    def test_missing_exposures_soft_skip_row_not_hard_fail(self) -> None:
        """暴露观测缺失 → skipped 软约束行(passed=False),构建不失败。"""
        result = self._build(factor_exposures={})
        skipped = [
            item
            for item in result.adjustments
            if item.constraint == NEUTRALIZATION_SKIPPED_CONSTRAINT
        ]
        assert len(skipped) == 1
        assert skipped[0].passed is False
        assert skipped[0].reason.startswith(NEUTRALIZATION_INACTIVE_WARNING)
        outcomes = None
        from finboard_backtest.portfolio.builder import to_research_constraint_outcomes

        outcomes = to_research_constraint_outcomes(result)
        skipped_outcome = next(
            item
            for item in outcomes
            if item.constraint == NEUTRALIZATION_SKIPPED_CONSTRAINT
        )
        assert skipped_outcome.hard is False
        assert skipped_outcome.passed is False
        hard_failed = [
            item.constraint for item in outcomes if item.hard and not item.passed
        ]
        assert hard_failed == []

    def test_infeasible_constraint_fails_closed(self) -> None:
        """约束本体不可满足 → AllocationError 而非静默放行。

        全部持仓相对基准低配 0.5(active = 1/3 - 0.5 = -1/6,f=1):
        暴露 = -0.5,超 -0.1;「修复」需要加仓回基准,只减仓只会
        加深低配 → 无进展,fail-closed。
        """
        from finboard_backtest.portfolio.allocators import AllocationError

        with pytest.raises(AllocationError, match="风险因子中性化约束不可满足"):
            self._build(
                neutralization_baseline={"A": 0.5, "B": 0.5, "C": 0.5},
                factor_exposures={"beta": {"A": 1.0, "B": 1.0, "C": 1.0}},
            )


def test_result_is_frozen_dataclass() -> None:
    result = project_risk_factor_neutralization(
        {"A": 0.5},
        (RiskFactorLimit(factor="beta", max_active_exposure=0.1),),
        {"beta": {"A": 1.0}},
    )
    assert isinstance(result, FactorNeutralizationResult)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.converged = False  # type: ignore[misc]
