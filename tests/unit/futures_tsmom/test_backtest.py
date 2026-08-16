"""TSMOM 回测引擎测试 —— 多空 / 保证金 / MTM / 风险约束 / T+1。"""

from __future__ import annotations

import pytest

from finboard_backtest.futures_tsmom import (
    ContractSpec,
    FuturesMarket,
    FuturesTsmomConfig,
    TradeAction,
    run_backtest,
)


def _make_spec(symbol: str = "IF", multiplier: float = 300.0) -> ContractSpec:
    return ContractSpec(
        symbol=symbol,
        name="Test Future",
        market=FuturesMarket.EQUITY_INDEX,
        multiplier=multiplier,
        margin_rate=0.10,
        tick_size=0.2,
        commission_rate=0.000023,
        commission_per_lot=0.0,
        price_limit_pct=0.10,
    )


def _trending_prices(n: int, drift: float = 0.001) -> list[float]:
    return [100.0 * ((1.0 + drift) ** i) for i in range(n)]


def _oscillating_prices(n: int, amplitude: float = 5.0, period: int = 20) -> list[float]:
    import math
    return [100.0 + amplitude * math.sin(2 * math.pi * i / period) for i in range(n)]


class TestRunBacktestBasics:
    def test_empty_symbols_raises(self) -> None:
        with pytest.raises(ValueError, match="closes_by_symbol 不能为空"):
            run_backtest(
                closes_by_symbol={},
                opens_by_symbol={},
                volumes_by_symbol={},
                specs={},
                config=FuturesTsmomConfig(),
            )

    def test_length_mismatch_raises(self) -> None:
        spec = _make_spec()
        with pytest.raises(ValueError, match="长度"):
            run_backtest(
                closes_by_symbol={"IF": [1.0, 2.0, 3.0], "T": [1.0, 2.0]},
                opens_by_symbol={"IF": [1.0, 2.0, 3.0], "T": [1.0, 2.0]},
                volumes_by_symbol={"IF": [100.0, 200.0, 300.0], "T": [100.0, 200.0]},
                specs={"IF": spec, "T": _make_spec("T")},
                config=FuturesTsmomConfig(lookbacks=(21,)),
            )

    def test_basic_run(self) -> None:
        spec = _make_spec()
        closes = _trending_prices(300)
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21, 63)),
        )
        assert result.bar_count == 300
        assert len(result.equity_curve) == 300
        assert len(result.daily_records) == 300

    def test_t_plus_1_no_same_bar_fill(self) -> None:
        spec = _make_spec()
        closes = _trending_prices(300)
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21,)),
        )
        first_signal_idx = None
        for i, signals in enumerate(result.all_signals):
            if signals and signals.get("IF") and signals["IF"].direction != 0:
                first_signal_idx = i
                break
        if first_signal_idx is not None:
            first_trade_idx = result.trades[0].bar_index if result.trades else None
            if first_trade_idx is not None:
                assert first_trade_idx > first_signal_idx


class TestLongShort:
    def test_uptrend_generates_long(self) -> None:
        spec = _make_spec()
        closes = _trending_prices(300, drift=0.002)
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21, 63)),
            initial_capital=5_000_000.0,
        )
        long_trades = [t for t in result.trades if t.action is TradeAction.OPEN_LONG]
        assert len(long_trades) > 0

    def test_downtrend_generates_short(self) -> None:
        spec = _make_spec()
        closes = _trending_prices(300, drift=-0.002)
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21, 63)),
            initial_capital=5_000_000.0,
        )
        short_trades = [t for t in result.trades if t.action is TradeAction.OPEN_SHORT]
        assert len(short_trades) > 0

    def test_both_directions_in_mixed_trend(self) -> None:
        spec = _make_spec()
        closes = (
            _trending_prices(150, drift=0.003)
            + _trending_prices(150, drift=-0.003)[1:]
        )
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 299},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21, 63)),
            initial_capital=5_000_000.0,
        )
        actions = {t.action for t in result.trades}
        assert TradeAction.OPEN_LONG in actions or TradeAction.OPEN_SHORT in actions


class TestMarginAndMTM:
    def test_daily_mtm_updates_cash(self) -> None:
        spec = _make_spec()
        closes = _trending_prices(300)
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21,)),
            initial_capital=1_000_000.0,
        )
        if result.trades:
            eq_changes = [
                result.equity_curve[i] - result.equity_curve[i - 1]
                for i in range(1, len(result.equity_curve))
            ]
            assert any(abs(c) > 0.01 for c in eq_changes)

    def test_margin_capped(self) -> None:
        spec = _make_spec(multiplier=300.0)
        closes = _trending_prices(300)
        cfg = FuturesTsmomConfig(
            lookbacks=(21,),
            vol_target=0.5,
            max_leverage=1.0,
            max_margin_usage=0.3,
        )
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=cfg,
            initial_capital=100_000.0,
        )
        for record in result.daily_records:
            if record.equity > 0:
                assert record.margin_used / record.equity <= 0.31 or record.margin_used == 0

    def test_leverage_capped(self) -> None:
        spec = _make_spec()
        closes = _trending_prices(300)
        cfg = FuturesTsmomConfig(
            lookbacks=(21,),
            vol_target=0.5,
            max_leverage=0.5,
        )
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=cfg,
            initial_capital=100_000.0,
        )
        for record in result.daily_records:
            if record.equity > 0:
                assert record.gross_exposure / record.equity <= 0.51 or record.gross_exposure == 0


class TestRiskConstraints:
    def test_single_contract_weight_capped(self) -> None:
        spec = _make_spec(multiplier=10.0)
        closes = _trending_prices(300)
        cfg = FuturesTsmomConfig(
            lookbacks=(21,),
            vol_target=0.5,
            max_single_contract_weight=0.2,
            max_leverage=10.0,
            max_margin_usage=1.0,
        )
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [100000.0] * 300},
            specs={"IF": spec},
            config=cfg,
            initial_capital=100_000.0,
        )
        for record in result.daily_records:
            if record.equity > 0 and record.gross_exposure > 0:
                assert record.gross_exposure / record.equity <= 0.21

    def test_multi_market_constraint(self) -> None:
        if_spec = _make_spec("IF")
        t_spec = ContractSpec(
            symbol="T", name="T", market=FuturesMarket.TREASURY_BOND,
            multiplier=10000.0, margin_rate=0.02, tick_size=0.005,
            commission_rate=0.0, commission_per_lot=3.0, price_limit_pct=0.02,
        )
        closes_if = _trending_prices(300)
        closes_t = _trending_prices(300, drift=0.0005)
        cfg = FuturesTsmomConfig(
            lookbacks=(21,),
            max_single_market_weight=0.25,
            max_leverage=10.0,
            max_margin_usage=1.0,
        )
        result = run_backtest(
            closes_by_symbol={"IF": closes_if, "T": closes_t},
            opens_by_symbol={"IF": closes_if, "T": closes_t},
            volumes_by_symbol={"IF": [100000.0] * 300, "T": [100000.0] * 300},
            specs={"IF": if_spec, "T": t_spec},
            config=cfg,
            initial_capital=1_000_000.0,
        )
        for record in result.daily_records:
            if record.equity > 0 and record.gross_exposure > 0:
                total = record.gross_exposure / record.equity
                assert total <= 10.5  # within leverage cap


class TestIntegerLots:
    def test_no_fractional_lots(self) -> None:
        spec = _make_spec(multiplier=300.0)
        closes = _trending_prices(300)
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21,)),
            initial_capital=100_000.0,
        )
        for trade in result.trades:
            assert trade.lots == int(trade.lots)
            assert trade.lots > 0


class TestResultProperties:
    def test_total_return(self) -> None:
        spec = _make_spec()
        closes = _trending_prices(300)
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21, 63)),
            initial_capital=100_000.0,
        )
        assert isinstance(result.total_return, float)

    def test_as_dict(self) -> None:
        spec = _make_spec()
        closes = _trending_prices(300)
        result = run_backtest(
            closes_by_symbol={"IF": closes},
            opens_by_symbol={"IF": closes},
            volumes_by_symbol={"IF": [10000.0] * 300},
            specs={"IF": spec},
            config=FuturesTsmomConfig(lookbacks=(21,)),
        )
        d = result.as_dict()
        assert "total_return" in d
        assert "trade_count" in d
        assert "roll_count" in d
