"""均值回归策略绩效分析测试。"""

from __future__ import annotations

import math

from finboard_backtest.mean_reversion.analysis import (
    analyze_mean_reversion,
    check_capital_tiers,
    compute_cost_attribution,
    compute_cost_sensitivity,
    compute_crisis_performance,
    compute_trend_correlation,
)
from finboard_backtest.mean_reversion.backtest import MeanReversionResult, run_backtest
from finboard_backtest.mean_reversion.config import (
    MeanReversionConfig,
    RegimeMethod,
    SignalFamily,
)


def _mean_revert_with_dips(
    n: int = 250,
    base: float = 10.0,
    amp: float = 0.3,
    dip_depth: float = 2.0,
    dip_interval: int = 50,
) -> list[float]:
    prices: list[float] = []
    for i in range(n):
        p = base + amp * math.sin(2 * math.pi * i / 20)
        if i > 0 and i % dip_interval == 0:
            p -= dip_depth
        prices.append(p)
    return prices


def _make_result() -> tuple[MeanReversionResult, dict[str, list[float]], MeanReversionConfig]:
    closes = {"AAA": _mean_revert_with_dips(n=250)}
    cfg = MeanReversionConfig(
        family=SignalFamily.Z_SCORE,
        lookback=20,
        regime_method=RegimeMethod.NONE,
        max_holding_days=3,
        commission_rate=0.0003,
        slippage_bps=5.0,
    )
    return run_backtest(closes, cfg), closes, cfg


class TestCostAttribution:
    def test_breakdown(self) -> None:
        result, _, _ = _make_result()
        attr = compute_cost_attribution(result, 100_000)
        assert attr.total_cost > 0
        assert attr.commission > 0
        assert attr.cost_as_pct_of_nav >= 0
        assert attr.cost_per_trade > 0

    def test_no_trades_zero_cost(self) -> None:
        closes = {"AAA": [10.0] * 250}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
        )
        result = run_backtest(closes, cfg)
        attr = compute_cost_attribution(result, 100_000)
        assert attr.total_cost == 0


class TestCostSensitivity:
    def test_doubled_cost_degrades_return(self) -> None:
        closes = {"AAA": _mean_revert_with_dips(n=250)}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
        )
        sens = compute_cost_sensitivity(closes, cfg)
        assert sens.return_degradation >= 0
        assert sens.base_net_return >= sens.doubled_cost_net_return

    def test_survives_flag(self) -> None:
        closes = {"AAA": _mean_revert_with_dips(n=250)}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
        )
        sens = compute_cost_sensitivity(closes, cfg)
        assert isinstance(sens.survives_doubled_cost, bool)


class TestCrisisPerformance:
    def test_max_drawdown_nonneg(self) -> None:
        result, _, _ = _make_result()
        crisis = compute_crisis_performance(result)
        assert crisis.max_drawdown >= 0

    def test_trades_during_drawdown(self) -> None:
        result, _, _ = _make_result()
        crisis = compute_crisis_performance(result)
        assert crisis.trades_during_drawdown >= 0


class TestTrendCorrelation:
    def test_correlation_range(self) -> None:
        result, _, _ = _make_result()
        trend_eq = [100_000 * (1 + 0.001 * i) for i in range(len(result.equity_curve))]
        corr = compute_trend_correlation(result, trend_eq)
        assert corr is not None
        assert -1.0 <= corr <= 1.0

    def test_none_when_no_trend(self) -> None:
        result, _, _ = _make_result()
        corr = compute_trend_correlation(result, None)
        assert corr is None

    def test_none_when_too_short(self) -> None:
        result, _, _ = _make_result()
        corr = compute_trend_correlation(result, [100_000, 101_000])
        assert corr is None


class TestCapitalTiers:
    def test_all_tiers_present(self) -> None:
        result, _, _ = _make_result()
        tiers = check_capital_tiers(result, 100_000)
        assert len(tiers) == 3
        labels = [t.tier for t in tiers]
        assert "10万" in labels
        assert "20万" in labels
        assert "50万" in labels

    def test_higher_capital_more_feasible(self) -> None:
        result, _, _ = _make_result()
        tiers = check_capital_tiers(result, 100_000)
        if result.trades:
            assert tiers[2].avg_trade_value >= tiers[0].avg_trade_value


class TestAnalyzeMeanReversion:
    def test_full_analysis(self) -> None:
        result, closes, _cfg = _make_result()
        analysis = analyze_mean_reversion(
            result,
            initial_capital=100_000,
            closes_for_sensitivity=closes,
        )
        assert analysis.trade_count >= 0
        assert analysis.sharpe_ratio is not None
        assert analysis.max_drawdown >= 0
        assert isinstance(analysis.rejected, bool)

    def test_rejected_on_negative_sharpe(self) -> None:
        result, _closes, _cfg = _make_result()
        result_with_neg = type(result)(
            config=result.config,
            equity_curve=[100_000, 99_000, 98_000, 97_000, 96_000],
            trades=result.trades,
            position_weights=result.position_weights,
            regime_states=result.regime_states,
            bars_processed=result.bars_processed,
            skipped_bars=result.skipped_bars,
            symbols_traded=result.symbols_traded,
            entry_count=result.entry_count,
            exit_signal_count=result.exit_signal_count,
            exit_max_holding_count=result.exit_max_holding_count,
            blocked_count=result.blocked_count,
        )
        analysis = analyze_mean_reversion(result_with_neg)
        assert analysis.rejected
        assert any("Sharpe" in r for r in analysis.rejection_reasons)

    def test_as_dict(self) -> None:
        result, closes, _ = _make_result()
        analysis = analyze_mean_reversion(
            result,
            closes_for_sensitivity=closes,
        )
        d = analysis.as_dict()
        assert "total_return" in d
        assert "cost" in d
        assert "rejected" in d
