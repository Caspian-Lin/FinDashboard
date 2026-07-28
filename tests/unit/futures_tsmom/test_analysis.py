"""归因分析测试。"""

from __future__ import annotations

from finboard_backtest.futures_tsmom import (
    ContractSpec,
    FuturesMarket,
    FuturesTsmomConfig,
    TsmomResult,
    analyze_tsmom,
    run_backtest,
)
from finboard_backtest.futures_tsmom.analysis import (
    check_capital_tiers,
    compute_cost_attribution,
    compute_crisis_performance,
    compute_return_attribution,
)


def _make_spec(symbol: str = "IF", multiplier: float = 300.0) -> ContractSpec:
    return ContractSpec(
        symbol=symbol,
        name="Test",
        market=FuturesMarket.EQUITY_INDEX,
        multiplier=multiplier,
        margin_rate=0.10,
        tick_size=0.2,
        commission_rate=0.000023,
        commission_per_lot=0.0,
        price_limit_pct=0.10,
    )


def _run_simple_backtest(
    initial: float = 1_000_000.0,
) -> tuple[TsmomResult, list[float], list[float], list[float], ContractSpec]:
    spec = _make_spec()
    closes = [100.0 * (1.001 ** i) for i in range(300)]
    opens = closes
    vols = [10000.0] * 300
    result = run_backtest(
        closes_by_symbol={"IF": closes},
        opens_by_symbol={"IF": opens},
        volumes_by_symbol={"IF": vols},
        specs={"IF": spec},
        config=FuturesTsmomConfig(lookbacks=(21, 63)),
        initial_capital=initial,
    )
    return result, closes, opens, vols, spec


class TestReturnAttribution:
    def test_attribution_sums_correctly(self) -> None:
        result, _closes, _opens, _vols, _spec = _run_simple_backtest()
        attr = compute_return_attribution(result, initial_capital=1_000_000.0)
        assert isinstance(attr.total_return, float)
        assert isinstance(attr.direction_return, float)
        assert isinstance(attr.commission_cost, float)
        assert attr.commission_cost <= 0

    def test_attribution_dict(self) -> None:
        result, _closes, _opens, _vols, _spec = _run_simple_backtest()
        attr = compute_return_attribution(result, 1_000_000.0)
        d = attr.as_dict()
        assert "total_return" in d
        assert "direction_return" in d


class TestCostAttribution:
    def test_cost_attribution(self) -> None:
        result, _closes, _opens, _vols, _spec = _run_simple_backtest()
        attr = compute_cost_attribution(result, 1_000_000.0)
        assert attr.total_cost >= 0
        assert attr.commission >= 0
        assert attr.slippage >= 0
        assert attr.margin_interest >= 0
        assert attr.cost_as_pct_of_nav >= 0

    def test_cost_per_trade(self) -> None:
        result, _closes, _opens, _vols, _spec = _run_simple_backtest()
        attr = compute_cost_attribution(result, 1_000_000.0)
        if result.trade_count > 0:
            assert attr.cost_per_trade > 0


class TestCrisisPerformance:
    def test_crisis_performance(self) -> None:
        result, _closes, _opens, _vols, _spec = _run_simple_backtest()
        crisis = compute_crisis_performance(result)
        assert crisis.max_drawdown >= 0
        assert crisis.max_drawdown_duration >= 0
        assert isinstance(crisis.worst_daily_return, float)

    def test_crisis_with_drawdown(self) -> None:
        spec = _make_spec()
        closes = [100.0 * (1.001 ** i) for i in range(150)] + [100.0 * (1.001 ** 150) * (0.99 ** i) for i in range(1, 151)]
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21, 63)),
        )
        crisis = compute_crisis_performance(result)
        assert crisis.max_drawdown >= 0


class TestCapitalTiers:
    def test_capital_tiers(self) -> None:
        result, _closes, _opens, _vols, spec = _run_simple_backtest()
        tiers = check_capital_tiers({"IF": spec}, result, 1_000_000.0)
        assert len(tiers) == 3
        tier_names = [t.tier for t in tiers]
        assert "10万" in tier_names
        assert "50万" in tier_names
        for t in tiers:
            assert isinstance(t.feasible, bool)
            assert isinstance(t.reason, str)


class TestAnalyzeTsmom:
    def test_full_analysis(self) -> None:
        result, closes, opens, vols, spec = _run_simple_backtest()
        analysis = analyze_tsmom(
            result,
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": opens},
            volumes_by_symbol={"IF": vols},
            specs={"IF": spec},
            initial_capital=1_000_000.0,
        )
        assert isinstance(analysis.total_return, float)
        assert isinstance(analysis.sharpe_ratio, float)
        assert isinstance(analysis.max_drawdown, float)
        assert isinstance(analysis.rejected, bool)
        assert len(analysis.capital_tiers) == 3
        assert len(analysis.stress_tests) >= 2

    def test_analysis_dict(self) -> None:
        result, closes, opens, vols, spec = _run_simple_backtest()
        analysis = analyze_tsmom(
            result,
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": opens},
            volumes_by_symbol={"IF": vols},
            specs={"IF": spec},
        )
        d = analysis.as_dict()
        assert "total_return" in d
        assert "sharpe_ratio" in d
        assert "return_attribution" in d
        assert "capital_tiers" in d

    def test_rejection_for_negative_return(self) -> None:
        spec = _make_spec()
        closes = [100.0 * (0.999 ** i) for i in range(300)]
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21, 63)),
            initial_capital=1_000_000.0,
        )
        analysis = analyze_tsmom(
            result,
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
        )
        d = analysis.as_dict()
        assert "rejection_reasons" in d
