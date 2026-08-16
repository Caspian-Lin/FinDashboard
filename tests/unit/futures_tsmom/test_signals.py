"""TSMOM 信号生成测试。"""

from __future__ import annotations

import math

import pytest

from finboard_backtest.futures_tsmom import (
    FuturesTsmomConfig,
    TsmomSignalFamily,
    compute_composite_score,
    generate_signals,
    generate_tsmom_signal,
    past_return,
    realized_volatility,
)


def _trending_up(n: int, start: float = 100.0) -> list[float]:
    return [start * (1.001 ** i) for i in range(n)]


def _trending_down(n: int, start: float = 100.0) -> list[float]:
    return [start * (0.999 ** i) for i in range(n)]


def _flat(n: int, price: float = 100.0) -> list[float]:
    return [price] * n


class TestRealizedVolatility:
    def test_flat_series_zero_vol(self) -> None:
        closes = _flat(100)
        vol = realized_volatility(closes, 60)
        assert vol == pytest.approx(0.0, abs=1e-6)

    def test_insufficient_data(self) -> None:
        vol = realized_volatility([100.0, 101.0], 60)
        assert math.isinf(vol)

    def test_positive_vol(self) -> None:
        closes = [100.0 + i % 5 for i in range(100)]
        vol = realized_volatility(closes, 60)
        assert vol > 0


class TestPastReturn:
    def test_positive_return(self) -> None:
        closes = [100.0, 105.0]
        assert past_return(closes, 1) == pytest.approx(0.05)

    def test_negative_return(self) -> None:
        closes = [100.0, 95.0]
        assert past_return(closes, 1) == pytest.approx(-0.05)

    def test_insufficient_data(self) -> None:
        assert past_return([100.0], 10) == 0.0


class TestCompositeScore:
    def test_uptrend_positive(self) -> None:
        closes = _trending_up(300)
        score = compute_composite_score(closes, (21, 63, 126, 252))
        assert score > 0

    def test_downtrend_negative(self) -> None:
        closes = _trending_down(300)
        score = compute_composite_score(closes, (21, 63, 126, 252))
        assert score < 0

    def test_flat_zero(self) -> None:
        closes = _flat(300)
        score = compute_composite_score(closes, (21, 63, 126, 252))
        assert abs(score) < 1e-6

    def test_single_lookback_family(self) -> None:
        closes = _trending_up(100)
        score = compute_composite_score(
            closes, (63,), family=TsmomSignalFamily.SINGLE_LOOKBACK,
        )
        assert score == pytest.approx(1.0)

    def test_multiple_lookback_average(self) -> None:
        closes = [100.0] * 62 + [110.0] * 63
        score = compute_composite_score(closes, (21, 63), family=TsmomSignalFamily.MULTIPLE_LOOKBACK)
        assert -1.0 <= score <= 1.0

    def test_score_range(self) -> None:
        closes = _trending_up(300)
        score = compute_composite_score(closes, (21, 63, 126, 252))
        assert -1.0 <= score <= 1.0


class TestGenerateTsmomSignal:
    def test_long_signal(self) -> None:
        closes = _trending_up(300)
        cfg = FuturesTsmomConfig()
        sig = generate_tsmom_signal("IF", closes, cfg)
        assert sig.direction == 1
        assert sig.target_weight > 0
        assert sig.realized_vol > 0

    def test_short_signal(self) -> None:
        closes = _trending_down(300)
        cfg = FuturesTsmomConfig()
        sig = generate_tsmom_signal("IF", closes, cfg)
        assert sig.direction == -1
        assert sig.target_weight > 0

    def test_flat_signal(self) -> None:
        closes = _flat(300)
        cfg = FuturesTsmomConfig()
        sig = generate_tsmom_signal("IF", closes, cfg)
        assert sig.direction == 0
        assert sig.target_weight == pytest.approx(0.0)

    def test_vol_floor_protection(self) -> None:
        closes = _flat(300)
        cfg = FuturesTsmomConfig(vol_floor=0.05)
        sig = generate_tsmom_signal("IF", closes, cfg)
        assert sig.realized_vol == 0.0
        assert sig.target_weight == pytest.approx(0.0)

    def test_vol_scaling(self) -> None:
        closes_low_vol = [100.0 + 0.01 * math.sin(i / 10) for i in range(300)]
        closes_high_vol = [100.0 + 5.0 * math.sin(i / 10) for i in range(300)]
        cfg = FuturesTsmomConfig()
        sig_low = generate_tsmom_signal("X", closes_low_vol, cfg)
        sig_high = generate_tsmom_signal("X", closes_high_vol, cfg)
        assert sig_low.realized_vol < sig_high.realized_vol


class TestGenerateSignals:
    def test_multiple_symbols(self) -> None:
        closes_by_symbol = {
            "IF": _trending_up(350),
            "T": _trending_down(350),
        }
        cfg = FuturesTsmomConfig()
        signals = generate_signals(closes_by_symbol, cfg)
        assert signals["IF"].direction == 1
        assert signals["T"].direction == -1

    def test_insufficient_data(self) -> None:
        closes_by_symbol = {"IF": [100.0, 101.0]}
        cfg = FuturesTsmomConfig()
        signals = generate_signals(closes_by_symbol, cfg)
        assert signals["IF"].direction == 0
        assert "insufficient_data" in signals["IF"].reasons
