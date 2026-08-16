"""因子分析报告测试。"""

from __future__ import annotations

import numpy as np

from finboard_backtest.factors.analysis import (
    compute_factor_analysis,
)


class TestComputeFactorAnalysis:
    def test_basic(self) -> None:
        rng = np.random.default_rng(42)
        n_periods = 10
        n_symbols = 30
        factor_scores_history: list[dict[str, dict[str, float]]] = []
        forward_returns: list[dict[str, float]] = []
        selected_history: list[tuple[str, ...]] = []

        for _t in range(n_periods):
            syms = [f"s{i}" for i in range(n_symbols)]
            scores = {
                "value": {s: float(rng.standard_normal()) for s in syms},
                "quality": {s: float(rng.standard_normal()) for s in syms},
            }
            factor_scores_history.append(scores)
            fwd = {s: float(rng.standard_normal() * 0.02) for s in syms}
            forward_returns.append(fwd)
            selected = tuple(sorted(syms, key=lambda x: scores["value"][x], reverse=True)[:10])
            selected_history.append(selected)

        report = compute_factor_analysis(
            factor_scores_history, forward_returns, selected_history,
            capital_tiers={"100k": 100_000.0, "500k": 500_000.0},
        )
        assert report.n_periods == n_periods
        assert "value" in report.single_factor_ic
        assert "quality" in report.single_factor_ic
        assert abs(report.single_factor_ic["value"]) <= 1.0
        assert len(report.factor_correlation) >= 2
        assert "value" in report.portfolio_factor_exposure

    def test_ic_with_signal(self) -> None:
        rng = np.random.default_rng(42)
        n = 50
        syms = [f"s{i}" for i in range(n)]
        scores = {s: float(rng.standard_normal()) for s in syms}
        returns = {s: scores[s] * 0.01 + float(rng.standard_normal() * 0.001) for s in syms}
        factor_scores_history = [{"value": scores}]
        forward_returns = [returns]
        selected_history = [tuple(sorted(syms, key=lambda x: scores[x], reverse=True)[:10])]
        report = compute_factor_analysis(factor_scores_history, forward_returns, selected_history)
        assert report.single_factor_ic["value"] > 0.3

    def test_empty(self) -> None:
        report = compute_factor_analysis([], [], [])
        assert report.n_periods == 0
        assert report.avg_turnover == 0.0

    def test_turnover(self) -> None:
        rng = np.random.default_rng(42)
        n_periods = 5
        n_symbols = 20
        factor_scores_history: list[dict[str, dict[str, float]]] = []
        forward_returns: list[dict[str, float]] = []
        selected_history: list[tuple[str, ...]] = []
        syms = [f"s{i}" for i in range(n_symbols)]
        for _t in range(n_periods):
            scores = {s: float(rng.standard_normal()) for s in syms}
            factor_scores_history.append({"value": scores})
            forward_returns.append({s: float(rng.standard_normal() * 0.01) for s in syms})
            selected = tuple(sorted(syms, key=lambda x: scores[x], reverse=True)[:5])
            selected_history.append(selected)
        report = compute_factor_analysis(factor_scores_history, forward_returns, selected_history)
        assert report.avg_turnover >= 0.0
        assert report.avg_n_holdings == 5.0

    def test_capital_feasibility(self) -> None:
        rng = np.random.default_rng(42)
        n = 20
        syms = [f"s{i}" for i in range(n)]
        scores = {s: float(rng.standard_normal()) for s in syms}
        factor_scores_history = [{"value": scores}]
        forward_returns = [dict.fromkeys(syms, 0.01)]
        selected_history = [tuple(syms[:20])]
        report = compute_factor_analysis(
            factor_scores_history, forward_returns, selected_history,
            capital_tiers={"100k": 100_000.0, "500k": 500_000.0},
            min_price=5.0,
        )
        assert "100k" in report.capital_tier_feasibility
        assert "500k" in report.capital_tier_feasibility

    def test_factor_correlation_identity(self) -> None:
        rng = np.random.default_rng(42)
        n = 30
        syms = [f"s{i}" for i in range(n)]
        scores1 = {s: float(rng.standard_normal()) for s in syms}
        scores2 = {s: float(rng.standard_normal()) for s in syms}
        factor_scores_history = [{"f1": scores1, "f2": scores2}]
        forward_returns = [dict.fromkeys(syms, 0.0)]
        selected_history = [tuple(syms[:5])]
        report = compute_factor_analysis(factor_scores_history, forward_returns, selected_history)
        assert abs(report.factor_correlation["f1"]["f1"] - 1.0) < 1e-10

    def test_best_factor(self) -> None:
        rng = np.random.default_rng(42)
        n = 50
        syms = [f"s{i}" for i in range(n)]
        strong_signal = {s: float(rng.standard_normal()) for s in syms}
        noise = {s: float(rng.standard_normal()) for s in syms}
        returns = {s: strong_signal[s] * 0.02 + float(rng.standard_normal() * 0.001) for s in syms}
        factor_scores_history = [{"strong": strong_signal, "noise": noise}]
        forward_returns = [returns]
        selected_history = [tuple(sorted(syms, key=lambda x: strong_signal[x], reverse=True)[:10])]
        report = compute_factor_analysis(factor_scores_history, forward_returns, selected_history)
        assert report.best_factor == "strong"
