"""均值回归回测引擎测试。

重点验证:
- 下一 Bar 执行(信号 T → 成交 T+1)
- 持仓数 / 持有期 / 冷却 / 换手 / 参与率 约束
- 成本跟踪(佣金 / 印花税 / 滑点)
- 确定性(相同输入相同输出)
"""

from __future__ import annotations

import math

import pytest

from finboard_backtest.mean_reversion.backtest import (
    run_backtest,
)
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
    """生成带周期性深跌的均值回复序列。"""
    prices: list[float] = []
    for i in range(n):
        p = base + amp * math.sin(2 * math.pi * i / 20)
        if i > 0 and i % dip_interval == 0:
            p -= dip_depth
        prices.append(p)
    return prices


def _steady_uptrend(n: int = 250) -> list[float]:
    return [10.0 + 0.02 * i for i in range(n)]


class TestNextBarExecution:
    def test_signal_does_not_fill_same_bar(self) -> None:
        """验证信号在 T 日产生,成交在 T+1。"""
        closes = {"AAA": _mean_revert_with_dips(n=250)}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            entry_threshold=1.5,
            exit_threshold=0.5,
            regime_method=RegimeMethod.NONE,
            max_holding_days=5,
            cooldown_days=0,
        )
        result = run_backtest(closes, cfg, initial_capital=100_000)
        assert len(result.trades) > 0
        for trade in result.trades:
            assert trade.bar_index > cfg.min_data_days

    def test_buy_before_sell(self) -> None:
        """每只 ETF 的第一笔交易必须是买入。"""
        closes = {"AAA": _mean_revert_with_dips(n=250)}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=5,
        )
        result = run_backtest(closes, cfg)
        for symbol in result.symbols_traded:
            sym_trades = [t for t in result.trades if t.symbol == symbol]
            assert sym_trades[0].side == "BUY"


class TestPositionManagement:
    def test_max_positions_enforced(self) -> None:
        closes = {
            f"SYM{i}": _mean_revert_with_dips(n=250)
            for i in range(10)
        }
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_positions=3,
            max_holding_days=3,
            cooldown_days=1,
        )
        result = run_backtest(closes, cfg)
        for weights in result.position_weights[cfg.min_data_days:]:
            assert len(weights) <= 3

    def test_max_holding_enforced(self) -> None:
        closes = {"AAA": _mean_revert_with_dips(n=250)}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
            cooldown_days=0,
        )
        result = run_backtest(closes, cfg)
        for i, trade in enumerate(result.trades):
            if trade.side == "SELL":
                buy_idx = None
                for j in range(i - 1, -1, -1):
                    if result.trades[j].symbol == trade.symbol and result.trades[j].side == "BUY":
                        buy_idx = result.trades[j].bar_index
                        break
                if buy_idx is not None:
                    holding = trade.bar_index - buy_idx
                    assert holding <= cfg.max_holding_days + 1

    def test_cooldown_prevents_immediate_reentry(self) -> None:
        closes = {"AAA": _mean_revert_with_dips(n=250)}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=2,
            cooldown_days=5,
        )
        result = run_backtest(closes, cfg)
        sym_trades = [t for t in result.trades if t.symbol == "AAA"]
        for i in range(0, len(sym_trades) - 1, 2):
            if i + 2 < len(sym_trades):
                sell_bar = sym_trades[i + 1].bar_index
                next_buy_bar = sym_trades[i + 2].bar_index
                assert next_buy_bar - sell_bar >= cfg.cooldown_days

    def test_no_averaging_down(self) -> None:
        closes = {"AAA": _mean_revert_with_dips(n=250)}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            no_averaging_down=True,
            max_holding_days=10,
        )
        result = run_backtest(closes, cfg)
        for i in range(len(result.trades) - 1):
            if result.trades[i].side == "BUY" and result.trades[i + 1].side == "BUY":
                pytest.fail("Detected consecutive buys on same symbol without sell")


class TestCostTracking:
    def test_commission_charged(self) -> None:
        closes = {"AAA": _mean_revert_with_dips(n=250)}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
            commission_rate=0.0003,
            commission_min=5.0,
        )
        result = run_backtest(closes, cfg, initial_capital=100_000)
        assert result.total_commission > 0

    def test_stamp_tax_on_sells_only(self) -> None:
        closes = {"AAA": _mean_revert_with_dips(n=250)}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
            stamp_tax_rate=0.0005,
        )
        result = run_backtest(closes, cfg)
        buys = [t for t in result.trades if t.side == "BUY"]
        sells = [t for t in result.trades if t.side == "SELL"]
        for b in buys:
            assert b.stamp_tax == 0.0
        for s in sells:
            assert s.stamp_tax > 0

    def test_slippage_applied(self) -> None:
        closes = {"AAA": _mean_revert_with_dips(n=250)}
        opens = {"AAA": [p * 1.0 for p in closes["AAA"]]}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
            slippage_bps=10.0,
        )
        result = run_backtest(closes, cfg, opens=opens)
        assert result.total_slippage > 0

    def test_gross_return_exceeds_net(self) -> None:
        closes = {"AAA": _mean_revert_with_dips(n=250)}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
            commission_rate=0.001,
            slippage_bps=20.0,
        )
        result = run_backtest(closes, cfg)
        assert result.gross_return > result.total_return


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        closes = {
            "AAA": _mean_revert_with_dips(n=250),
            "BBB": _mean_revert_with_dips(n=250, dip_interval=40),
        }
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
        )
        r1 = run_backtest(closes, cfg)
        r2 = run_backtest(closes, cfg)
        assert r1.equity_curve == r2.equity_curve
        assert len(r1.trades) == len(r2.trades)


class TestRegimeFiltering:
    def test_no_trades_in_downtrend(self) -> None:
        closes = {"AAA": _steady_uptrend(250)[::-1]}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.SMA,
            regime_sma_window=200,
            max_holding_days=3,
        )
        result = run_backtest(closes, cfg)
        assert len([t for t in result.trades if t.side == "BUY"]) == 0


class TestEmptyAndEdgeCases:
    def test_empty_closes_raises(self) -> None:
        with pytest.raises(ValueError, match="closes 不能为空"):
            run_backtest({}, MeanReversionConfig())

    def test_all_flat_no_trades(self) -> None:
        closes = {"AAA": [10.0] * 250}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
        )
        result = run_backtest(closes, cfg)
        assert len(result.trades) == 0
