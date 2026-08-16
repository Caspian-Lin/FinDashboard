"""绝对趋势 + 相对动量信号测试。"""

from __future__ import annotations

import pytest

from finboard_backtest.etf_rotation import (
    AbsoluteTrendMethod,
    EtfRotationConfig,
    compute_absolute_trend,
    compute_relative_momentum,
    generate_signals,
)


def _uptrend(n: int = 260, start: float = 1.0, slope: float = 0.002) -> list[float]:
    """生成 n 天单调上涨的价格序列。"""
    return [start + slope * i for i in range(n)]


def _downtrend(n: int = 260, start: float = 2.0, slope: float = 0.002) -> list[float]:
    """生成 n 天单调下跌的价格序列。"""
    return [start - slope * i for i in range(n)]


def _flat(n: int = 260, value: float = 1.0) -> list[float]:
    return [value] * n


class TestAbsoluteTrendSMA:
    def test_uptrend_passes(self) -> None:
        closes = {"A": _uptrend()}
        cfg = EtfRotationConfig()
        result = compute_absolute_trend(closes, cfg)
        assert result["A"].passes
        assert result["A"].detail > 1.0

    def test_downtrend_fails(self) -> None:
        closes = {"B": _downtrend()}
        cfg = EtfRotationConfig()
        result = compute_absolute_trend(closes, cfg)
        assert not result["B"].passes
        assert result["B"].detail < 1.0

    def test_flat_fails(self) -> None:
        closes = {"C": _flat()}
        cfg = EtfRotationConfig()
        result = compute_absolute_trend(closes, cfg)
        # price == SMA exactly, ratio == 1.0, not > 1.0
        assert not result["C"].passes

    def test_insufficient_data(self) -> None:
        closes = {"D": [1.0, 2.0, 3.0]}
        cfg = EtfRotationConfig(sma_window=200)
        result = compute_absolute_trend(closes, cfg)
        assert not result["D"].passes


class TestAbsoluteTrendLookbackReturn:
    def test_positive_return_passes(self) -> None:
        closes = {"A": _uptrend(260)}
        cfg = EtfRotationConfig(
            trend_method=AbsoluteTrendMethod.LOOKBACK_RETURN,
            lookback_months=3,
        )
        result = compute_absolute_trend(closes, cfg)
        assert result["A"].passes

    def test_negative_return_fails(self) -> None:
        closes = {"B": _downtrend(260)}
        cfg = EtfRotationConfig(
            trend_method=AbsoluteTrendMethod.LOOKBACK_RETURN,
            lookback_months=3,
        )
        result = compute_absolute_trend(closes, cfg)
        assert not result["B"].passes

    def test_zero_return_fails(self) -> None:
        closes = {"C": _flat(260)}
        cfg = EtfRotationConfig(
            trend_method=AbsoluteTrendMethod.LOOKBACK_RETURN,
            lookback_months=3,
        )
        result = compute_absolute_trend(closes, cfg)
        assert not result["C"].passes


class TestRelativeMomentum:
    def test_higher_momentum_gets_higher_score(self) -> None:
        closes = {
            "A": _uptrend(260, slope=0.003),  # stronger uptrend
            "B": _uptrend(260, slope=0.001),  # weaker uptrend
        }
        cfg = EtfRotationConfig()
        trend = compute_absolute_trend(closes, cfg)
        momentum = compute_relative_momentum(closes, trend, cfg)
        assert momentum["A"].score > momentum["B"].score

    def test_failed_trend_gets_nan(self) -> None:
        closes = {"A": _uptrend(260), "B": _downtrend(260)}
        cfg = EtfRotationConfig()
        trend = compute_absolute_trend(closes, cfg)
        momentum = compute_relative_momentum(closes, trend, cfg)
        # B fails trend, score should be NaN
        assert momentum["B"].score != momentum["B"].score

    def test_components_populated(self) -> None:
        closes = {"A": _uptrend(260)}
        cfg = EtfRotationConfig()
        trend = compute_absolute_trend(closes, cfg)
        momentum = compute_relative_momentum(closes, trend, cfg)
        assert "3m" in momentum["A"].components
        assert "6m" in momentum["A"].components
        assert "12m" in momentum["A"].components


class TestGenerateSignals:
    def test_selects_top_n(self) -> None:
        closes = {}
        for i in range(6):
            closes[f"E{i}.SH"] = _uptrend(260, slope=0.001 * (6 - i))
        cfg = EtfRotationConfig(top_n=3)
        signals = generate_signals(closes, cfg)
        selected = [s for s in signals if s.selected]
        assert len(selected) == 3
        # E0 has strongest uptrend (slope=0.006)
        assert "E0.SH" in [s.symbol for s in selected]

    def test_failed_trend_not_selected(self) -> None:
        closes = {
            "UP": _uptrend(260),
            "DOWN": _downtrend(260),
        }
        cfg = EtfRotationConfig(top_n=5)
        signals = generate_signals(closes, cfg)
        up_sig = next(s for s in signals if s.symbol == "UP")
        down_sig = next(s for s in signals if s.symbol == "DOWN")
        assert up_sig.passes_trend
        assert up_sig.selected
        assert not down_sig.passes_trend
        assert not down_sig.selected

    def test_all_fail_trend_returns_empty_selection(self) -> None:
        closes = {"A": _downtrend(260), "B": _downtrend(260)}
        cfg = EtfRotationConfig(top_n=3)
        signals = generate_signals(closes, cfg)
        selected = [s for s in signals if s.selected]
        assert len(selected) == 0

    def test_ties_broken_by_symbol_order(self) -> None:
        # Identical price series → identical score → tie broken by symbol
        closes = {"C": _uptrend(260), "A": _uptrend(260), "B": _uptrend(260)}
        cfg = EtfRotationConfig(top_n=1)
        signals = generate_signals(closes, cfg)
        selected = [s for s in signals if s.selected]
        assert len(selected) == 1
        assert selected[0].symbol == "A"  # alphabetically first

    def test_empty_closes(self) -> None:
        cfg = EtfRotationConfig()
        signals = generate_signals({}, cfg)
        assert len(signals) == 0

    def test_order_independence(self) -> None:
        # Same data, different dict construction order → same result
        base_closes = {
            f"E{i}.SH": _uptrend(260, slope=0.001 * (10 - i))
            for i in range(5)
        }
        reversed_closes = dict(reversed(list(base_closes.items())))
        cfg = EtfRotationConfig(top_n=3)
        s1 = generate_signals(base_closes, cfg)
        s2 = generate_signals(reversed_closes, cfg)
        # Sort both by symbol for comparison
        s1_sorted = sorted(s1, key=lambda x: x.symbol)
        s2_sorted = sorted(s2, key=lambda x: x.symbol)
        for a, b in zip(s1_sorted, s2_sorted, strict=True):
            assert a.symbol == b.symbol
            assert a.selected == b.selected
            assert (a.momentum_score or 0) == pytest.approx(b.momentum_score or 0, abs=1e-10)

    def test_insufficient_data_not_selected(self) -> None:
        closes = {"A": [1.0, 2.0, 3.0]}
        cfg = EtfRotationConfig(sma_window=200)
        signals = generate_signals(closes, cfg)
        assert not any(s.selected for s in signals)
        assert not any(s.passes_trend for s in signals)
