"""issue #266:最大 IR(切点)分配器 —— 求解器数学正确性与 ERC 基准对比。

求解器为 capped simplex 上的投影梯度上升(纯 numpy,零新增依赖);
本文件用解析切点解(2 资产 ``w* ∝ Σ⁻¹μ``)与投影可行性逐项断言,
并按验收要求与 ERC 基准做「可运行 + 指标合理 + 可复现」对比,
不预设两种方法谁更优。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from finboard_backtest.portfolio.allocators import (
    AllocationError,
    ErcAllocator,
    make_allocator,
)
from finboard_backtest.portfolio.builder import (
    PortfolioBuildInput,
    PortfolioBuildResult,
    build_portfolio,
)
from finboard_backtest.portfolio.contracts import (
    CovarianceFailureMode,
    PortfolioConstraints,
    Signal,
)
from finboard_backtest.portfolio.covariance import CovarianceEstimate, estimate_covariance
from finboard_backtest.portfolio.max_ir import (
    DEFAULT_MAX_ITER,
    DEFAULT_TOLERANCE,
    MaxIrAllocator,
    project_onto_capped_simplex,
    solve_max_ir,
)

TS = date(2025, 6, 27)


def _signals(scores: dict[str, float]) -> list[Signal]:
    return [
        Signal(symbol=symbol, score=score, timestamp=TS, strategy_id="t")
        for symbol, score in sorted(scores.items())
    ]


def _cov(tickers: list[str], vols: list[float], corr: np.ndarray | float) -> CovarianceEstimate:
    n = len(tickers)
    corr_mat = (
        np.full((n, n), corr) if np.isscalar(corr) else np.asarray(corr, dtype=np.float64)
    )
    np.fill_diagonal(corr_mat, 1.0)
    vol_arr = np.array(vols)
    return CovarianceEstimate(
        matrix=np.outer(vol_arr, vol_arr) * corr_mat,
        tickers=tickers,
        shrinkage=0.1,
        n_observations=252,
    )


def _ex_post_ir(
    weights: dict[str, float],
    mu: dict[str, float],
    cov: CovarianceEstimate,
) -> float:
    index = {t: i for i, t in enumerate(cov.tickers)}
    vector = np.zeros(len(cov.tickers))
    for symbol, weight in weights.items():
        vector[index[symbol]] = weight
    excess = sum(mu[symbol] * weight for symbol, weight in weights.items())
    variance = float(vector @ cov.matrix @ vector)
    if variance <= 0:
        return float("-inf")
    return excess / float(np.sqrt(variance))


class TestCappedSimplexProjection:
    def test_known_waterfall_projection(self) -> None:
        v = np.array([1.0, 0.5, 0.0])
        projected = project_onto_capped_simplex(v, cap=0.4)
        assert np.allclose(projected, [0.4, 0.4, 0.2], atol=1e-9)

    def test_projection_preserves_budget_and_bounds(self) -> None:
        rng = np.random.default_rng(266)
        for _ in range(20):
            v = rng.normal(scale=5, size=7)
            projected = project_onto_capped_simplex(v, cap=0.35)
            assert projected.sum() == pytest.approx(1.0, abs=1e-9)
            assert (projected >= -1e-12).all()
            assert (projected <= 0.35 + 1e-9).all()

    def test_infeasible_cap_rejected(self) -> None:
        with pytest.raises(AllocationError, match="单标的上限不可行"):
            project_onto_capped_simplex(np.array([1.0, 1.0, 1.0]), cap=0.3)


class TestSolveMaxIr:
    def test_two_asset_analytic_tangency_recovery(self) -> None:
        """解析切点解 w* ∝ Σ⁻¹μ:求解器须恢复到数值精度。"""
        cov = np.array([[0.04, 0.01], [0.01, 0.09]])
        mu = np.array([0.10, 0.08])
        expected = np.linalg.solve(cov, mu)
        expected = expected / expected.sum()
        solution = solve_max_ir(mu, cov)
        assert solution.converged is True
        assert np.allclose(solution.weights, expected, atol=1e-6)
        assert solution.objective > 0
        assert solution.iterations <= DEFAULT_MAX_ITER

    def test_three_asset_with_cap(self) -> None:
        cov = np.array(
            [[0.04, 0.01, 0.002], [0.01, 0.09, 0.003], [0.002, 0.003, 0.16]]
        )
        mu = np.array([0.10, 0.08, 0.05])
        solution = solve_max_ir(mu, cov, cap=0.4)
        assert solution.converged is True
        assert solution.weights.sum() == pytest.approx(1.0, abs=1e-9)
        assert (solution.weights <= 0.4 + 1e-9).all()
        assert (solution.weights >= -1e-12).all()
        # 带上限的最优值不超过无上限最优值(约束只能更紧)。
        uncapped = solve_max_ir(mu, cov)
        assert solution.objective <= uncapped.objective + 1e-9

    def test_negative_mu_assets_get_zero_weight(self) -> None:
        """long-only 形态:负 μ̃ 标的最优权重为 0。"""
        cov = np.array([[0.04, 0.005], [0.005, 0.09]])
        mu = np.array([0.10, -0.05])
        solution = solve_max_ir(mu, cov)
        assert solution.converged is True
        assert solution.weights[1] <= 1e-9
        assert solution.weights[0] == pytest.approx(1.0, abs=1e-9)

    def test_scale_invariance_of_mu(self) -> None:
        """目标对 μ̃ 的正尺度变换不变:同一解、同一权重。"""
        cov = np.array([[0.04, 0.01], [0.01, 0.09]])
        base = solve_max_ir(np.array([0.10, 0.08]), cov)
        scaled = solve_max_ir(np.array([10.0, 8.0]), cov)
        assert np.allclose(base.weights, scaled.weights, atol=1e-9)

    def test_deterministic_reproducibility(self) -> None:
        rng = np.random.default_rng(266)
        n = 8
        a = rng.normal(size=(n, n))
        cov = a @ a.T / n + np.eye(n) * 0.01
        mu = np.abs(rng.normal(size=n)) + 0.01
        first = solve_max_ir(mu, cov, cap=0.3)
        second = solve_max_ir(mu, cov, cap=0.3)
        assert np.array_equal(first.weights, second.weights)
        assert first.iterations == second.iterations
        assert first.objective == second.objective

    def test_stationarity_residual_bounded_by_tolerance(self) -> None:
        rng = np.random.default_rng(7)
        n = 6
        a = rng.normal(size=(n, n))
        cov = a @ a.T / n + np.eye(n) * 0.02
        mu = np.array([0.2, 0.15, 0.1, 0.08, 0.05, 0.02])
        solution = solve_max_ir(mu, cov)
        assert solution.converged is True
        assert solution.residual <= DEFAULT_TOLERANCE

    def test_invalid_inputs_rejected(self) -> None:
        cov = np.eye(2) * 0.04
        mu = np.array([0.1, 0.1])
        with pytest.raises(AllocationError, match="维度不匹配"):
            solve_max_ir(np.array([0.1]), cov)
        with pytest.raises(AllocationError, match="max_iter"):
            solve_max_ir(mu, cov, max_iter=0)
        with pytest.raises(AllocationError, match="上限"):
            solve_max_ir(mu, cov, cap=1.5)
        with pytest.raises(AllocationError, match="中性"):
            solve_max_ir(np.array([0.0, 0.0]), cov)
        with pytest.raises(AllocationError, match="奇异或非正定"):
            solve_max_ir(mu, np.ones((2, 2)) * 0.04)


class TestMaxIrAllocator:
    def test_requires_covariance(self) -> None:
        alloc = MaxIrAllocator()
        with pytest.raises(AllocationError, match="需要协方差矩阵"):
            alloc.allocate(_signals({"A": 1.0}), None, PortfolioConstraints())

    def test_no_positive_signal_rejected(self) -> None:
        alloc = MaxIrAllocator()
        cov = _cov(["A", "B"], [0.2, 0.3], 0.3)
        signals = _signals({"A": -1.0, "B": -0.5})
        with pytest.raises(AllocationError, match="无正预期超额收益信号"):
            alloc.allocate(
                signals,
                cov,
                PortfolioConstraints(min_cash_buffer=0.0, long_only=False),
            )

    def test_allocate_respects_constraints(self) -> None:
        cov = _cov(["A", "B", "C"], [0.2, 0.1, 0.4], 0.2)
        signals = _signals({"A": 1.0, "B": 0.8, "C": 0.3})
        constraints = PortfolioConstraints(
            max_weight_per_asset=0.4,
            min_cash_buffer=0.05,
        )
        target = MaxIrAllocator().allocate(_signals({"A": 1.0, "B": 0.8, "C": 0.3}), cov, constraints)
        for weight in target.weights.values():
            assert weight <= 0.4 + 1e-9
            assert weight >= 0.0
        assert target.gross_weight <= 0.95 + 1e-9
        assert target.covariance_version == "lw_delta0.1000"
        assert signals[0].symbol in target.weights

    def test_make_allocator_registry(self) -> None:
        assert isinstance(make_allocator("max_ir"), MaxIrAllocator)
        with pytest.raises(AllocationError, match="max_ir"):
            make_allocator("no_such_method")


class TestAgainstErcBenchmark:
    """验收要求:与 ERC 基准组合指标对比 —— 可运行、指标合理、可复现,
    不预设谁更优;仅锁定「max_ir 的事前 IR 不劣于 ERC」这一目标函数性质。"""

    def _fixture(self) -> tuple[list[Signal], CovarianceEstimate]:
        cov = _cov(
            ["A", "B", "C", "D"],
            [0.15, 0.10, 0.20, 0.30],
            np.array(
                [
                    [1.0, 0.2, 0.5, 0.1],
                    [0.2, 1.0, 0.1, 0.3],
                    [0.5, 0.1, 1.0, 0.2],
                    [0.1, 0.3, 0.2, 1.0],
                ]
            ),
        )
        signals = _signals({"A": 1.0, "B": 0.9, "C": 0.6, "D": 0.4})
        return signals, cov

    def test_ex_ante_ir_not_worse_than_erc(self) -> None:
        """同一 μ̃/Σ 下,最大 IR 组合的事前 IR ≥ ERC 组合(目标函数性质)。"""
        signals, cov = self._fixture()
        relaxed = PortfolioConstraints(
            max_weight_per_asset=1.0,
            max_weight_per_sleeve=1.0,
            min_cash_buffer=0.0,
        )
        mu = {s.symbol: s.score for s in signals}
        max_ir_target = MaxIrAllocator().allocate(signals, cov, relaxed)
        erc_target = ErcAllocator().allocate(signals, cov, relaxed)
        ir_max_ir = _ex_post_ir(max_ir_target.weights, mu, cov)
        ir_erc = _ex_post_ir(erc_target.weights, mu, cov)
        assert ir_max_ir >= ir_erc - 1e-9

    def test_both_runners_produce_valid_reproducible_portfolios(self) -> None:
        """组合指标合理(预算/上限/波动率为正)且同输入两次完全一致。"""
        signals, cov = self._fixture()
        relaxed = PortfolioConstraints(
            max_weight_per_asset=1.0,
            max_weight_per_sleeve=1.0,
            min_cash_buffer=0.0,
        )
        from finboard_backtest.portfolio.risk_budget import portfolio_volatility

        for allocator in (MaxIrAllocator(), ErcAllocator()):
            first = allocator.allocate(signals, cov, relaxed)
            second = allocator.allocate(signals, cov, relaxed)
            assert first.weights == second.weights  # 确定性复现
            assert first.gross_weight <= 1.0 + 1e-9
            assert all(weight >= 0.0 for weight in first.weights.values())
            volatility = portfolio_volatility(first.weights, cov, annualized=False)
            assert 0.0 < volatility < 1.0


class TestBuilderIntegration:
    def _covariance(self) -> CovarianceEstimate:
        return _cov(["A", "B", "C"], [0.2, 0.1, 0.35], 0.15)

    def _build(self, method: str, **overrides) -> PortfolioBuildResult:
        constraints = PortfolioConstraints(
            max_weight_per_asset=0.6,
            min_cash_buffer=0.0,
            **overrides,
        )
        cov = self._covariance()
        return build_portfolio(
            PortfolioBuildInput(
                signals=tuple(_signals({"A": 1.0, "B": 0.7, "C": 0.4})),
                method=method,
                constraints=constraints,
                covariance=cov,
            )
        )

    def test_max_ir_runs_end_to_end(self) -> None:
        result = self._build("max_ir")
        target = result.target_after_constraints
        assert target.weights
        assert all(weight > 0 for weight in target.weights.values())
        assert target.gross_weight <= 1.0 + 1e-9
        assert all(weight <= 0.6 + 1e-9 for weight in target.weights.values())
        assert result.covariance_fallback_used is False
        # 高信号强度 + 低波动标的应获得最高权重(方向合理性,非最优性断言)。
        assert max(target.weights, key=lambda s: target.weights[s]) in {"A", "B"}

    def test_missing_covariance_fail_closed(self) -> None:
        constraints = PortfolioConstraints(min_cash_buffer=0.0)
        with pytest.raises(AllocationError, match="缺少协方差矩阵"):
            build_portfolio(
                PortfolioBuildInput(
                    signals=tuple(_signals({"A": 1.0, "B": 0.7})),
                    method="max_ir",
                    constraints=constraints,
                    covariance=None,
                )
            )

    def test_solver_failure_falls_back_to_equal_weight_in_fail_safe(self) -> None:
        """covariance_failure_mode=fallback_equal_weight:求解失败降级等权,
        fallback 标记可见;fail_closed(默认)则显式报错。"""
        # long_only=False 且全负信号:max_ir 分配器拒绝,触发降级路径。
        negative = [
            Signal(symbol=s, score=-1.0, timestamp=TS, strategy_id="t")
            for s in ("A", "B")
        ]
        constraints = PortfolioConstraints(
            long_only=False,
            covariance_failure_mode=CovarianceFailureMode.FALLBACK_EQUAL_WEIGHT,
        )
        result = build_portfolio(
            PortfolioBuildInput(
                signals=tuple(negative),
                method="max_ir",
                constraints=constraints,
                covariance=self._covariance(),
            )
        )
        assert result.covariance_fallback_used is True
        assert result.target_after_constraints.weights  # 等权兜底已产出

        constraints_closed = PortfolioConstraints(
            long_only=False,
            covariance_failure_mode=CovarianceFailureMode.FAIL_CLOSED,
        )
        with pytest.raises(AllocationError, match="fail_closed"):
            build_portfolio(
                PortfolioBuildInput(
                    signals=tuple(negative),
                    method="max_ir",
                    constraints=constraints_closed,
                    covariance=self._covariance(),
                )
            )

    def test_estimate_covariance_path_compatible(self) -> None:
        """与 REST/MCP 相同的 returns→LW 协方差入口兼容。"""
        returns = {
            "A": [0.01, -0.02, 0.015, 0.008, -0.005] * 12,
            "B": [0.005, 0.007, -0.004, 0.011, 0.002] * 12,
        }
        cov = estimate_covariance({k: np.array(v) for k, v in returns.items()})
        constraints = PortfolioConstraints(min_cash_buffer=0.0)
        result = build_portfolio(
            PortfolioBuildInput(
                signals=tuple(_signals({"A": 1.0, "B": 0.6})),
                method="max_ir",
                constraints=constraints,
                covariance=cov,
            )
        )
        assert result.target_after_constraints.weights
