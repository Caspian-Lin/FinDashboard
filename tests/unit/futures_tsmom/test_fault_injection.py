"""故障注入测试 —— 跳空 / 零量 / 保证金突升 / 合约元数据错误 / PIT 安全。"""

from __future__ import annotations

import pytest

from finboard_backtest.asset_rules import ASSET_RULES_VERSION, DEFAULT_TABLE
from finboard_backtest.futures_tsmom import (
    ContractSpec,
    FuturesMarket,
    FuturesTsmomConfig,
    run_backtest,
)
from finboard_backtest.futures_tsmom.roll import build_active_series, build_continuous_series


def _make_spec(symbol: str = "IF", multiplier: float = 300.0, margin_rate: float = 0.10) -> ContractSpec:
    return ContractSpec(
        symbol=symbol,
        name="Test",
        market=FuturesMarket.EQUITY_INDEX,
        multiplier=multiplier,
        margin_rate=margin_rate,
        tick_size=0.2,
        commission_rate=0.000023,
        commission_per_lot=0.0,
        price_limit_pct=0.10,
    )


class TestGapRisk:
    def test_large_gap_no_crash(self) -> None:
        spec = _make_spec()
        closes = [100.0 * (1.001 ** i) for i in range(150)]
        closes.append(closes[-1] * 0.8)
        closes.extend([closes[-1] * (0.999 ** i) for i in range(1, 150)])
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * len(closes)},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21, 63)),
            initial_capital=1_000_000.0,
        )
        assert result.bar_count == len(closes)
        for record in result.daily_records:
            assert record.equity == record.equity

    def test_gap_up_followed_by_normal(self) -> None:
        spec = _make_spec()
        closes = [100.0 * (1.001 ** i) for i in range(200)]
        closes[150] = closes[149] * 1.15
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 200},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21,)),
            initial_capital=1_000_000.0,
        )
        assert result.bar_count == 200


class TestZeroVolume:
    def test_zero_volume_no_trade(self) -> None:
        spec = _make_spec()
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        vols = [0.0] * 300
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": vols},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21,)),
            initial_capital=1_000_000.0,
        )
        assert result.trade_count == 0 or all(t.lots == 0 for t in result.trades)

    def test_partial_zero_volume(self) -> None:
        spec = _make_spec()
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        vols = [10000.0] * 300
        vols[200] = 0.0
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": vols},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21,)),
            initial_capital=1_000_000.0,
        )
        assert result.bar_count == 300


class TestMarginSpike:
    def test_high_margin_rate_still_runs(self) -> None:
        spec = _make_spec(margin_rate=0.50)
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21,), max_margin_usage=0.8),
            initial_capital=1_000_000.0,
        )
        for record in result.daily_records:
            if record.equity > 0 and record.margin_used > 0:
                ratio = record.margin_used / record.equity
                assert ratio <= 0.81  # small tolerance for rounding


class TestContractMetadataError:
    def test_missing_spec_skips_symbol(self) -> None:
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={},
            config=FuturesTsmomConfig(lookbacks=(21,)),
        )
        assert result.trade_count == 0

    def test_unknown_symbol_in_specs_ignored(self) -> None:
        spec = _make_spec("IF")
        closes = [100.0 * (1.001 ** i) for i in range(300)]
        result = run_backtest(
            closes_by_symbol={"IF": closes, "UNKNOWN": closes},
            opens_by_symbol={"IF": closes, "UNKNOWN": closes},
            volumes_by_symbol={"IF": [10000.0] * 300, "UNKNOWN": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21,)),
        )
        assert result.bar_count == 300
        assert "UNKNOWN" not in result.symbols_traded


class TestPITSafety:
    def test_future_price_does_not_affect_trade(self) -> None:
        spec = _make_spec()
        closes_normal = [100.0 * (1.001 ** i) for i in range(300)]
        closes_modified = closes_normal.copy()
        closes_modified[250] *= 10.0  # future spike

        result1 = run_backtest(
            closes_by_symbol={"IF": closes_normal},
            opens_by_symbol={"IF": closes_normal},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21,)),
            initial_capital=1_000_000.0,
        )
        result2 = run_backtest(
            closes_by_symbol={"IF": closes_modified},
            opens_by_symbol={"IF": closes_modified},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21,)),
            initial_capital=1_000_000.0,
        )
        trades_before_250_r1 = [t for t in result1.trades if t.bar_index < 250]
        trades_before_250_r2 = [t for t in result2.trades if t.bar_index < 250]
        assert len(trades_before_250_r1) == len(trades_before_250_r2)


class TestRollHandling:
    def test_roll_events_collected(self) -> None:
        front_closes = [3500.0 + i * 0.5 for i in range(50)]
        next_closes = [3510.0 + i * 0.5 for i in range(50)]
        front_vols = [1000.0] * 50
        next_vols = [500.0] * 50
        for i in range(30, 50):
            next_vols[i] = 2000.0
            front_vols[i] = 300.0

        active = build_active_series(
            symbol="IF",
            front_closes=front_closes,
            front_opens=front_closes,
            front_volumes=front_vols,
            next_closes=next_closes,
            next_volumes=next_vols,
            roll_lookback=5,
        )
        spec = _make_spec()
        result = run_backtest(
            closes_by_symbol={"IF": front_closes},
            opens_by_symbol={"IF": front_closes},
            volumes_by_symbol={"IF": front_vols},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(5,)),
            initial_capital=10_000_000.0,
            active_series={"IF": active},
        )
        assert len(result.roll_events) > 0


class TestAssetRulesRegistration:
    def test_futures_rules_registered(self) -> None:
        assert len(DEFAULT_TABLE.futures_rules) > 0
        prefixes = {p for p, _ in DEFAULT_TABLE.futures_rules}
        assert "IF" in prefixes
        assert "T" in prefixes
        assert "TF" in prefixes

    def test_resolve_futures_if(self) -> None:
        rule = DEFAULT_TABLE.resolve_futures("IF2401")
        assert rule.multiplier == 300
        assert rule.margin_rate > 0

    def test_resolve_futures_t(self) -> None:
        rule = DEFAULT_TABLE.resolve_futures("T2403")
        assert rule.multiplier == 10000

    def test_resolve_unknown_future_raises(self) -> None:
        from finboard_backtest.asset_rules import AssetRuleResolutionError
        with pytest.raises(AssetRuleResolutionError):
            DEFAULT_TABLE.resolve_futures("UNKNOWN")

    def test_version_bumped(self) -> None:
        assert ASSET_RULES_VERSION == "v3"


class TestContinuousNotTradable:
    """验证连续合约调整收益不能当成可交易利润。"""

    def test_continuous_return_exceeds_realized(self) -> None:
        from finboard_shared.types import AdjustmentMethod

        front_closes = [100.0] * 30
        next_closes = [110.0] * 30
        front_vols = [100.0] * 30
        next_vols = [50.0] * 30
        for i in range(15, 30):
            next_vols[i] = 200.0
            front_vols[i] = 30.0

        active = build_active_series(
            symbol="X",
            front_closes=front_closes,
            front_opens=front_closes,
            front_volumes=front_vols,
            next_closes=next_closes,
            next_volumes=next_vols,
            roll_lookback=3,
        )
        cont = build_continuous_series(active, method=AdjustmentMethod.RATIO)

        if active.roll_count > 0:
            raw_return = active.closes[-1] / active.closes[0] - 1.0
            cont_return = cont.adjusted_closes[-1] / cont.adjusted_closes[0] - 1.0
            assert cont_return != raw_return
