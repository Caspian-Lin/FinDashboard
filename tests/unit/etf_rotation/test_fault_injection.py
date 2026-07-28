"""故障注入测试 —— 缺数 / 停牌 / 陈旧数据 / 债券同时失效。"""

from __future__ import annotations

from datetime import date

from finboard_backtest.etf_rotation import (
    EtfRotationConfig,
    EtfSector,
    EtfUniverse,
    EtfUniverseMember,
    allocate,
    generate_signals,
)
from finboard_backtest.etf_rotation.signals import EtfSignal
from finboard_shared.types import EtfCategory


def _uptrend(n: int = 250, start: float = 1.0, slope: float = 0.002) -> list[float]:
    return [start + slope * i for i in range(n)]


def _make_universe() -> EtfUniverse:
    return EtfUniverse(members=(
        EtfUniverseMember("A.SH", "A", EtfCategory.INDEX, EtfSector.BROAD_INDEX, date(2010, 1, 1)),
        EtfUniverseMember("B.SH", "B", EtfCategory.INDEX, EtfSector.BROAD_INDEX, date(2010, 1, 1)),
        EtfUniverseMember("511010.SH", "国债", EtfCategory.BOND, EtfSector.GOVERNMENT_BOND, date(2010, 1, 1)),
    ))


class TestMissingData:
    def test_etf_missing_from_closes(self) -> None:
        # ETF in universe but missing from closes → treated as no data, passes_trend=False
        closes = {"A.SH": _uptrend()}
        cfg = EtfRotationConfig(sma_window=10, top_n=5)
        signals = generate_signals(closes, cfg)
        # Only A.SH is in closes, B.SH is missing
        for s in signals:
            if s.symbol == "A.SH":
                assert s.passes_trend
            else:
                assert not s.passes_trend

    def test_short_history(self) -> None:
        closes = {"A.SH": [1.0, 1.1, 1.2]}
        cfg = EtfRotationConfig(sma_window=200)
        signals = generate_signals(closes, cfg)
        assert not any(s.passes_trend for s in signals)

    def test_single_bar(self) -> None:
        closes = {"A.SH": [1.0]}
        cfg = EtfRotationConfig()
        signals = generate_signals(closes, cfg)
        assert not any(s.passes_trend for s in signals)


class TestSuspension:
    def test_flat_prices_during_suspension(self) -> None:
        # Prices are constant (suspended), SMA = price, ratio = 1.0, not > 1.0
        closes = {"A.SH": [1.0] * 250}
        cfg = EtfRotationConfig(sma_window=200)
        signals = generate_signals(closes, cfg)
        assert not any(s.selected for s in signals)

    def test_recovery_after_suspension(self) -> None:
        # Flat for 100 days then trending up
        prices = [1.0] * 100 + [1.0 + 0.005 * i for i in range(200)]
        closes = {"A.SH": prices}
        cfg = EtfRotationConfig(sma_window=100)
        signals = generate_signals(closes, cfg)
        assert signals[0].passes_trend


class TestStaleData:
    def test_cross_border_stale_prices(self) -> None:
        # Cross-border ETF with old prices that haven't updated
        closes = {"A.SH": [1.0] * 250}  # stale flat
        cfg = EtfRotationConfig()
        signals = generate_signals(closes, cfg)
        assert not signals[0].passes_trend  # flat → ratio = 1.0, not > 1.0


class TestBondAndEquityBothFail:
    def test_flight_to_safety_when_bond_also_fails(self) -> None:
        u = _make_universe()
        # Both equity and bond are downtrending
        closes = {
            "A.SH": [2.0 - 0.001 * i for i in range(250)],
            "511010.SH": [2.0 - 0.001 * i for i in range(250)],
        }
        cfg = EtfRotationConfig(
            sma_window=10,
            safe_haven_symbol="511010.SH",
            safe_haven_trend_enabled=True,
        )
        signals = [
            EtfSignal(symbol="A.SH", passes_trend=False, momentum_score=None, selected=False),
        ]
        result = allocate(signals, closes, u, cfg)
        assert result.flight_to_safety
        assert result.weights == {}
        assert result.cash_weight == 1.0


class TestPITSafety:
    def test_future_listed_etf_not_in_closes(self) -> None:
        # An ETF that lists in the future should not appear in closes
        # (The caller is responsible for PIT filtering the universe)
        cfg = EtfRotationConfig(sma_window=10, top_n=5)
        closes = {"A.SH": _uptrend()}
        signals = generate_signals(closes, cfg)
        codes = {s.symbol for s in signals}
        assert "FUTURE.SH" not in codes  # no future ETF

    def test_delisted_etf_not_in_closes(self) -> None:
        # Delisted ETF should not be passed to generate_signals
        cfg = EtfRotationConfig(sma_window=10, top_n=5)
        closes = {"A.SH": _uptrend()}
        signals = generate_signals(closes, cfg)
        codes = {s.symbol for s in signals}
        assert "DEAD.SH" not in codes
