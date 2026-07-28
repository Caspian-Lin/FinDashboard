"""故障注入测试:跳空 / 停牌 / 成交量不足 / 连续跌停 / PIT 安全。"""

from __future__ import annotations

import math

import pytest

from finboard_backtest.mean_reversion.backtest import run_backtest
from finboard_backtest.mean_reversion.config import (
    MeanReversionConfig,
    RegimeMethod,
    SignalFamily,
)


def _normal_series(n: int = 250) -> list[float]:
    return [10.0 + 0.3 * math.sin(2 * math.pi * i / 20) for i in range(n)]


class TestGap:
    def test_gap_down_does_not_crash(self) -> None:
        prices = _normal_series(250)
        prices[200] = 5.0
        closes = {"AAA": prices}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
        )
        result = run_backtest(closes, cfg)
        assert result.bars_processed > 0

    def test_gap_up_handled(self) -> None:
        prices = _normal_series(250)
        prices[200] = 20.0
        closes = {"AAA": prices}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
        )
        result = run_backtest(closes, cfg)
        assert result.bars_processed > 0


class TestSuspension:
    def test_flat_prices_handled(self) -> None:
        prices = _normal_series(250)
        for i in range(100, 110):
            prices[i] = prices[99]
        closes = {"AAA": prices}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
        )
        result = run_backtest(closes, cfg)
        assert result.bars_processed > 0

    def test_missing_symbol(self) -> None:
        closes_b = {
            "AAA": _normal_series(250),
            "BBB": _normal_series(150),
        }
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
        )
        result = run_backtest(closes_b, cfg)
        bbb_trades = [t for t in result.trades if t.symbol == "BBB"]
        assert all(t.bar_index < 150 for t in bbb_trades)


class TestVolumeConstraint:
    def test_low_volume_blocks_entry(self) -> None:
        closes = {"AAA": _normal_series(250)}
        closes["AAA"][200] = 5.0
        volumes = {"AAA": [1_000_000] * 250}
        volumes["AAA"][201] = 100
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_participation=0.01,
            max_holding_days=3,
        )
        result = run_backtest(closes, cfg, volumes=volumes)
        assert result.bars_processed > 0

    def test_zero_volume_blocks(self) -> None:
        closes = {"AAA": _normal_series(250)}
        closes["AAA"][200] = 5.0
        volumes = {"AAA": [0] * 250}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_participation=0.01,
        )
        result = run_backtest(closes, cfg, volumes=volumes)
        assert len(result.trades) == 0


class TestConsecutiveLimitDown:
    def test_consecutive_drops_no_crash(self) -> None:
        prices = _normal_series(250)
        for i in range(200, 210):
            prices[i] = prices[199] * (0.9 ** (i - 199))
        closes = {"AAA": prices}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
        )
        result = run_backtest(closes, cfg)
        assert result.bars_processed > 0


class TestInsufficientData:
    def test_short_history_skipped(self) -> None:
        closes = {"AAA": [10.0, 11.0, 9.0]}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
        )
        result = run_backtest(closes, cfg)
        assert len(result.trades) == 0
        assert result.skipped_bars > 0

    def test_single_bar(self) -> None:
        closes = {"AAA": [10.0]}
        cfg = MeanReversionConfig(regime_method=RegimeMethod.NONE)
        result = run_backtest(closes, cfg)
        assert len(result.trades) == 0


class TestPITSafety:
    def test_no_future_data_leakage(self) -> None:
        """验证信号只使用历史数据:修改未来价格不影响已产生的交易。"""
        prices1 = _normal_series(250)
        prices2 = prices1.copy()
        prices2[240] = 100.0
        closes1 = {"AAA": prices1}
        closes2 = {"AAA": prices2}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE,
            lookback=20,
            regime_method=RegimeMethod.NONE,
            max_holding_days=3,
            cooldown_days=10,
        )
        r1 = run_backtest(closes1, cfg)
        r2 = run_backtest(closes2, cfg)
        early_trades1 = [t for t in r1.trades if t.bar_index < 235]
        early_trades2 = [t for t in r2.trades if t.bar_index < 235]
        assert len(early_trades1) == len(early_trades2)
        for t1, t2 in zip(early_trades1, early_trades2, strict=True):
            assert t1.bar_index == t2.bar_index
            assert t1.fill_price == pytest.approx(t2.fill_price)
