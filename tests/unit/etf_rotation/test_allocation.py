"""ETF 组合分配测试 —— 等权 / 逆波动率 / 避险切换。"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_backtest.etf_rotation import (
    AllocationMethod,
    EtfRotationConfig,
    EtfSector,
    EtfUniverse,
    EtfUniverseMember,
    allocate,
    allocate_equal_weight,
    allocate_inverse_volatility,
)
from finboard_backtest.etf_rotation.signals import EtfSignal
from finboard_shared.types import EtfCategory


def _make_universe() -> EtfUniverse:
    return EtfUniverse(members=(
        EtfUniverseMember("A.SH", "A", EtfCategory.INDEX, EtfSector.BROAD_INDEX, date(2010, 1, 1)),
        EtfUniverseMember("B.SH", "B", EtfCategory.INDEX, EtfSector.BROAD_INDEX, date(2010, 1, 1)),
        EtfUniverseMember("C.SH", "C", EtfCategory.INDEX, EtfSector.SECTOR, date(2010, 1, 1)),
        EtfUniverseMember("D.SH", "D", EtfCategory.COMMODITY, EtfSector.GOLD, date(2010, 1, 1)),
        EtfUniverseMember("511010.SH", "国债", EtfCategory.BOND, EtfSector.GOVERNMENT_BOND, date(2010, 1, 1)),
    ))


def _uptrend(n: int = 250, start: float = 1.0, slope: float = 0.002) -> list[float]:
    return [start + slope * i for i in range(n)]


class TestEqualWeightAllocation:
    def test_basic(self) -> None:
        u = _make_universe()
        # 3 ETFs: A(equity), B(equity), C(equity). All equity.
        # max_weight_per_asset_class=1.0 to avoid equity cap reducing weights
        cfg = EtfRotationConfig(max_weight_per_etf=0.50, max_weight_per_asset_class=1.0)
        weights = allocate_equal_weight(("A.SH", "B.SH", "C.SH"), u, cfg)
        assert len(weights) == 3
        for w in weights.values():
            assert w == pytest.approx(0.95 / 3, abs=0.01)

    def test_etf_cap_enforced(self) -> None:
        u = _make_universe()
        cfg = EtfRotationConfig(max_weight_per_etf=0.20, max_weight_per_asset_class=1.0, cash_buffer=0.05)
        weights = allocate_equal_weight(("A.SH", "B.SH"), u, cfg)
        # 0.95/2 = 0.475 > 0.20 → capped
        for w in weights.values():
            assert w <= 0.20 + 1e-9

    def test_asset_class_cap_enforced(self) -> None:
        u = _make_universe()
        cfg = EtfRotationConfig(
            max_weight_per_asset_class=0.40,
            max_weight_per_etf=0.30,
            cash_buffer=0.05,
        )
        # A, B, C are all equity; D is commodity
        weights = allocate_equal_weight(("A.SH", "B.SH", "C.SH", "D.SH"), u, cfg)
        # equity total (A+B+C) should be <= 0.40
        equity_total = weights.get("A.SH", 0) + weights.get("B.SH", 0) + weights.get("C.SH", 0)
        assert equity_total <= 0.40 + 0.01

    def test_empty_selection(self) -> None:
        u = _make_universe()
        cfg = EtfRotationConfig()
        weights = allocate_equal_weight((), u, cfg)
        assert weights == {}


class TestInverseVolatilityAllocation:
    def test_lower_vol_gets_higher_weight(self) -> None:
        u = _make_universe()
        cfg = EtfRotationConfig(
            allocation_method=AllocationMethod.INVERSE_VOLATILITY,
            vol_lookback=20,
            max_weight_per_etf=0.80,
            max_weight_per_asset_class=1.0,
        )
        # A: low vol (steady uptrend); B: high vol (oscillating)
        import math
        closes = {
            "A.SH": [1.0 + 0.001 * i for i in range(30)],
            "B.SH": [1.0 + 0.001 * i + 0.1 * math.sin(i * 0.5) for i in range(30)],
        }
        weights = allocate_inverse_volatility(("A.SH", "B.SH"), closes, u, cfg)
        # A should have higher weight than B (lower vol)
        assert weights["A.SH"] > 0
        assert weights.get("B.SH", 0) > 0
        assert weights["A.SH"] > weights["B.SH"]

    def test_all_zero_vol_falls_back_to_equal(self) -> None:
        u = _make_universe()
        cfg = EtfRotationConfig(
            allocation_method=AllocationMethod.INVERSE_VOLATILITY,
            vol_lookback=10,
        )
        # Constant prices → zero vol → equal weight fallback
        closes = {"A.SH": [1.0] * 30, "B.SH": [1.0] * 30}
        weights = allocate_inverse_volatility(("A.SH", "B.SH"), closes, u, cfg)
        assert weights["A.SH"] == pytest.approx(weights["B.SH"], abs=0.01)


class TestAllocate:
    def test_normal_allocation(self) -> None:
        u = _make_universe()
        closes = {sym: _uptrend() for sym in ["A.SH", "B.SH", "C.SH"]}
        cfg = EtfRotationConfig(top_n=2, sma_window=10)
        signals = [
            EtfSignal(symbol="A.SH", passes_trend=True, momentum_score=0.5, selected=True),
            EtfSignal(symbol="B.SH", passes_trend=True, momentum_score=0.3, selected=True),
            EtfSignal(symbol="C.SH", passes_trend=True, momentum_score=0.1, selected=False),
        ]
        result = allocate(signals, closes, u, cfg)
        assert not result.flight_to_safety
        assert result.n_risk_holdings == 2
        assert "A.SH" in result.weights
        assert "B.SH" in result.weights
        assert "C.SH" not in result.weights

    def test_flight_to_safety_no_selection(self) -> None:
        u = _make_universe()
        closes = {
            "A.SH": _uptrend(),
            "511010.SH": _uptrend(),
        }
        cfg = EtfRotationConfig(sma_window=10, safe_haven_symbol="511010.SH")
        signals = [
            EtfSignal(symbol="A.SH", passes_trend=False, momentum_score=None, selected=False),
        ]
        result = allocate(signals, closes, u, cfg)
        assert result.flight_to_safety
        # safe haven passes trend → allocated
        assert "511010.SH" in result.weights
        assert result.safe_haven_weight > 0

    def test_flight_to_safety_bond_also_fails(self) -> None:
        u = _make_universe()
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

    def test_flight_to_safety_disabled_trend_check(self) -> None:
        u = _make_universe()
        closes = {"511010.SH": [2.0 - 0.001 * i for i in range(250)]}
        cfg = EtfRotationConfig(
            sma_window=10,
            safe_haven_symbol="511010.SH",
            safe_haven_trend_enabled=False,
        )
        signals: list[EtfSignal] = []
        result = allocate(signals, closes, u, cfg)
        assert result.flight_to_safety
        assert "511010.SH" in result.weights

    def test_flight_to_safety_unknown_symbol(self) -> None:
        u = _make_universe()
        cfg = EtfRotationConfig(safe_haven_symbol="UNKNOWN.SH")
        signals: list[EtfSignal] = []
        result = allocate(signals, {}, u, cfg)
        assert result.flight_to_safety
        assert result.weights == {}
        assert result.cash_weight == 1.0

    def test_asset_class_exposure_populated(self) -> None:
        u = _make_universe()
        closes = {sym: _uptrend() for sym in ["A.SH", "D.SH"]}
        cfg = EtfRotationConfig(top_n=5, sma_window=10)
        signals = [
            EtfSignal(symbol="A.SH", passes_trend=True, momentum_score=0.5, selected=True),
            EtfSignal(symbol="D.SH", passes_trend=True, momentum_score=0.3, selected=True),
        ]
        result = allocate(signals, closes, u, cfg)
        assert "equity" in result.asset_class_exposure
        assert "commodity" in result.asset_class_exposure

    def test_order_independence(self) -> None:
        u = _make_universe()
        closes = {sym: _uptrend() for sym in ["A.SH", "B.SH", "C.SH"]}
        cfg = EtfRotationConfig(top_n=3, sma_window=10)
        signals1 = [
            EtfSignal(symbol="A.SH", passes_trend=True, momentum_score=0.5, selected=True),
            EtfSignal(symbol="B.SH", passes_trend=True, momentum_score=0.3, selected=True),
            EtfSignal(symbol="C.SH", passes_trend=True, momentum_score=0.1, selected=True),
        ]
        signals2 = list(reversed(signals1))
        r1 = allocate(signals1, closes, u, cfg)
        r2 = allocate(signals2, closes, u, cfg)
        assert r1.weights == r2.weights
