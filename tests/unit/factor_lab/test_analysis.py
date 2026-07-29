"""Alpha/收益关系、衰减、成本、邻域和状态分层测试。"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from finboard_backtest.factor_lab import (
    FactorAnalysisError,
    FactorPeriod,
    analyze_alpha_factor,
)


def _periods() -> list[FactorPeriod]:
    rng = np.random.default_rng(78)
    start = date(2020, 1, 2)
    result: list[FactorPeriod] = []
    for period_index in range(16):
        symbols = [f"S{index:02d}" for index in range(30)]
        scores = {
            symbol: float(rng.normal() + period_index * 0.001)
            for symbol in symbols
        }
        one_day = {
            symbol: scores[symbol] * 0.01 + float(rng.normal(scale=0.001))
            for symbol in symbols
        }
        five_day = {
            symbol: scores[symbol] * 0.006 + float(rng.normal(scale=0.002))
            for symbol in symbols
        }
        result.append(
            FactorPeriod(
                period=start + timedelta(days=period_index * 7),
                scores=scores,
                forward_returns={1: one_day, 5: five_day},
                transaction_costs=dict.fromkeys(symbols, 0.0005),
                market_regime="bull" if period_index < 8 else "bear",
            )
        )
    return result


def test_complete_alpha_analysis_is_reproducible() -> None:
    periods = _periods()
    neighbourhood = {
        "window_18": [
            {
                symbol: score * 0.95
                for symbol, score in period.scores.items()
            }
            for period in periods
        ],
        "window_22": [
            {
                symbol: score + (0.01 if index % 2 else -0.01)
                for index, (symbol, score) in enumerate(
                    period.scores.items()
                )
            }
            for period in periods
        ],
    }
    report = analyze_alpha_factor(
        "momentum",
        periods,
        neighbourhood_scores=neighbourhood,
    )
    assert report.rank_ic > 0.9
    assert report.pearson_ic > 0.9
    assert report.rank_ic_p_value < 0.01
    assert report.quantile_returns[-1].gross_return > report.quantile_returns[0].gross_return
    assert report.net_long_short_return < report.gross_long_short_return
    assert report.average_turnover >= 0
    assert {point.horizon for point in report.decay} == {1, 5}
    assert set(report.neighbourhood_rank_ic) == {"window_18", "window_22"}
    assert {item.regime for item in report.regimes} == {"bull", "bear"}
    assert report.as_dict()["factor_name"] == "momentum"


def test_parameter_neighbourhood_must_use_same_frozen_periods() -> None:
    periods = _periods()
    with pytest.raises(FactorAnalysisError, match="期数"):
        analyze_alpha_factor(
            "momentum",
            periods,
            neighbourhood_scores={"bad": [periods[0].scores]},
        )


def test_analysis_rejects_risk_factor_and_insufficient_data() -> None:
    with pytest.raises(FactorAnalysisError, match="不是 alpha"):
        analyze_alpha_factor("market_beta", _periods())
    with pytest.raises(FactorAnalysisError, match="不能为空"):
        analyze_alpha_factor("momentum", [])


def test_analysis_excludes_non_finite_pairs_without_hiding_extreme_values() -> None:
    periods = _periods()
    first = periods[0]
    scores = dict(first.scores)
    returns = dict(first.forward_returns[1])
    scores["S00"] = float("nan")
    returns["S01"] = float("inf")
    scores["S02"] = 1e12
    periods[0] = FactorPeriod(
        period=first.period,
        scores=scores,
        forward_returns={**first.forward_returns, 1: returns},
        transaction_costs=first.transaction_costs,
        market_regime=first.market_regime,
    )

    report = analyze_alpha_factor("momentum", periods)

    assert np.isfinite(report.rank_ic)
    assert np.isfinite(report.pearson_ic)
    assert np.isfinite(report.net_long_short_return)
