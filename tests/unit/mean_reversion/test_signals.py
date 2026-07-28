"""均值回归信号计算(z-score / Bollinger / RSI / Reversal)测试。"""

from __future__ import annotations

import math

import pytest

from finboard_backtest.mean_reversion.config import (
    MeanReversionConfig,
    SignalFamily,
)
from finboard_backtest.mean_reversion.signals import (
    compute_bollinger_position,
    compute_reversal,
    compute_rsi,
    compute_zscore,
    generate_signals,
)


def _mean_reverting(n: int = 100, base: float = 10.0, amp: float = 0.5) -> list[float]:
    """生成均值回复序列:在 base 上下振荡。"""
    return [base + amp * math.sin(2 * math.pi * i / 20) for i in range(n)]


def _uptrend(n: int = 100, start: float = 10.0, slope: float = 0.05) -> list[float]:
    return [start + slope * i for i in range(n)]


def _downtrend(n: int = 100, start: float = 10.0, slope: float = -0.05) -> list[float]:
    return [start + slope * i for i in range(n)]


def _flat(n: int = 100, value: float = 10.0) -> list[float]:
    return [value] * n


class TestZScore:
    def test_oversold_triggers_enter(self) -> None:
        prices = _flat(30, 10.0)
        prices[-1] = 8.0
        z = compute_zscore(prices, 20)
        assert z < -2.0

    def test_overbought_no_enter(self) -> None:
        prices = _flat(30, 10.0)
        prices[-1] = 12.0
        z = compute_zscore(prices, 20)
        assert z > 2.0

    def test_nan_insufficient_data(self) -> None:
        z = compute_zscore([1.0, 2.0, 3.0], 20)
        assert z != z

    def test_nan_zero_std(self) -> None:
        z = compute_zscore(_flat(25, 10.0), 20)
        assert z != z


class TestBollinger:
    def test_near_lower_band(self) -> None:
        prices = _flat(30, 10.0)
        prices[-1] = 9.0
        pct_b = compute_bollinger_position(prices, 20, 2.0)
        assert pct_b < 0.1

    def test_near_upper_band(self) -> None:
        prices = _flat(30, 10.0)
        prices[-1] = 11.0
        pct_b = compute_bollinger_position(prices, 20, 2.0)
        assert pct_b > 0.9

    def test_nan_insufficient(self) -> None:
        pct_b = compute_bollinger_position([1, 2, 3], 20, 2.0)
        assert pct_b != pct_b


class TestRSI:
    def test_oversold(self) -> None:
        prices = [10.0 + i * 0.1 for i in range(20)]
        for _ in range(20, 25):
            prices.append(prices[-1] - 0.5)
        rsi = compute_rsi(prices, 14)
        assert rsi < 30

    def test_all_gains(self) -> None:
        prices = [10.0 + i * 0.1 for i in range(20)]
        rsi = compute_rsi(prices, 14)
        assert rsi == pytest.approx(100.0)

    def test_all_losses(self) -> None:
        prices = [10.0 - i * 0.1 for i in range(20)]
        rsi = compute_rsi(prices, 14)
        assert rsi < 10

    def test_nan_insufficient(self) -> None:
        rsi = compute_rsi([1, 2, 3], 14)
        assert rsi != rsi


class TestReversal:
    def test_sharp_drop(self) -> None:
        prices = _flat(25, 10.0)
        prices[-1] = 9.0
        ret = compute_reversal(prices, 5)
        assert ret < -0.05

    def test_sharp_rise(self) -> None:
        prices = _flat(25, 10.0)
        prices[-1] = 11.0
        ret = compute_reversal(prices, 5)
        assert ret > 0.05

    def test_nan_insufficient(self) -> None:
        ret = compute_reversal([1, 2, 3], 10)
        assert ret != ret


class TestGenerateSignals:
    def test_zscore_enters_on_dip(self) -> None:
        closes = {
            "AAA": _flat(30, 10.0),
            "BBB": _flat(30, 10.0),
        }
        closes["AAA"][-1] = 8.0
        cfg = MeanReversionConfig(family=SignalFamily.Z_SCORE, lookback=20)
        sigs = generate_signals(closes, cfg)
        aaa = next(s for s in sigs if s.symbol == "AAA")
        assert aaa.should_enter

    def test_no_enter_in_uptrend(self) -> None:
        closes = {"AAA": _uptrend(30)}
        cfg = MeanReversionConfig(family=SignalFamily.Z_SCORE, lookback=20)
        sigs = generate_signals(closes, cfg)
        assert not sigs[0].should_enter

    def test_rsi_enters_oversold(self) -> None:
        prices = [10.0 + i * 0.1 for i in range(20)]
        for _ in range(20, 25):
            prices.append(prices[-1] - 0.5)
        closes = {"AAA": prices}
        cfg = MeanReversionConfig(
            family=SignalFamily.RSI, rsi_period=14,
            entry_threshold=30.0, exit_threshold=50.0,
        )
        sigs = generate_signals(closes, cfg)
        assert sigs[0].should_enter

    def test_reversal_enters_on_drop(self) -> None:
        prices = _flat(25, 10.0)
        prices[-1] = 8.5
        closes = {"AAA": prices}
        cfg = MeanReversionConfig(
            family=SignalFamily.REVERSAL, lookback=5,
            entry_threshold=0.1, exit_threshold=0.01,
        )
        sigs = generate_signals(closes, cfg)
        assert sigs[0].should_enter

    def test_deterministic_order(self) -> None:
        closes = {
            "BBB": _flat(30, 10.0),
            "AAA": _flat(30, 10.0),
        }
        closes["BBB"][-1] = 8.0
        closes["AAA"][-1] = 8.0
        cfg = MeanReversionConfig(family=SignalFamily.Z_SCORE, lookback=20)
        sigs = generate_signals(closes, cfg)
        assert sigs[0].symbol == "AAA"
        assert sigs[1].symbol == "BBB"

    def test_exit_signal_on_reversion(self) -> None:
        prices = _mean_reverting(30)
        prices[-1] = 7.0
        closes = {"AAA": prices}
        cfg = MeanReversionConfig(
            family=SignalFamily.Z_SCORE, lookback=20,
            entry_threshold=1.5, exit_threshold=0.5,
        )
        sigs = generate_signals(closes, cfg)
        assert sigs[0].should_enter

        prices2 = prices.copy()
        prices2[-1] = prices2[-2]
        closes2 = {"AAA": prices2}
        sigs2 = generate_signals(closes2, cfg)
        assert sigs2[0].should_exit
