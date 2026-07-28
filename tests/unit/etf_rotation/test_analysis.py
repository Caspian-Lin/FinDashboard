"""ETF 轮动策略绩效分析测试。"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_backtest.etf_rotation import (
    EtfRotationConfig,
    EtfSector,
    EtfUniverse,
    EtfUniverseMember,
    analyze_etf_rotation,
    check_capital_tier_feasibility,
    compute_asset_class_exposure,
    compute_max_drawdown_duration,
    compute_risk_contribution,
    compute_turnover,
)
from finboard_shared.types import EtfCategory


def _universe() -> EtfUniverse:
    return EtfUniverse(members=(
        EtfUniverseMember("A.SH", "A", EtfCategory.INDEX, EtfSector.BROAD_INDEX, date(2010, 1, 1)),
        EtfUniverseMember("B.SH", "B", EtfCategory.INDEX, EtfSector.SECTOR, date(2010, 1, 1)),
        EtfUniverseMember("D.SH", "D", EtfCategory.COMMODITY, EtfSector.GOLD, date(2010, 1, 1)),
        EtfUniverseMember("511010.SH", "国债", EtfCategory.BOND, EtfSector.GOVERNMENT_BOND, date(2010, 1, 1)),
    ))


class TestComputeTurnover:
    def test_no_change(self) -> None:
        assert compute_turnover({"A": 0.5, "B": 0.5}, {"A": 0.5, "B": 0.5}) == 0.0

    def test_full_rotation(self) -> None:
        t = compute_turnover({"A": 1.0}, {"B": 1.0})
        assert t == pytest.approx(1.0)

    def test_partial_change(self) -> None:
        t = compute_turnover({"A": 0.6, "B": 0.4}, {"A": 0.4, "B": 0.6})
        assert t == pytest.approx(0.2)

    def test_new_position(self) -> None:
        t = compute_turnover({"A": 1.0}, {"A": 0.8, "B": 0.2})
        assert t == pytest.approx(0.2)

    def test_removed_position(self) -> None:
        t = compute_turnover({"A": 0.5, "B": 0.5}, {"A": 1.0})
        assert t == pytest.approx(0.5)


class TestAssetClassExposure:
    def test_basic(self) -> None:
        u = _universe()
        history = [{"A.SH": 0.3, "D.SH": 0.2}]
        exp = compute_asset_class_exposure(history, u)
        assert exp["equity"] == pytest.approx(0.3)
        assert exp["commodity"] == pytest.approx(0.2)

    def test_averaged_over_history(self) -> None:
        u = _universe()
        history = [
            {"A.SH": 0.4, "D.SH": 0.1},
            {"A.SH": 0.2, "D.SH": 0.3},
        ]
        exp = compute_asset_class_exposure(history, u)
        assert exp["equity"] == pytest.approx(0.3)
        assert exp["commodity"] == pytest.approx(0.2)

    def test_empty_history(self) -> None:
        u = _universe()
        assert compute_asset_class_exposure([], u) == {}


class TestRiskContribution:
    def test_returns_normalized(self) -> None:
        u = _universe()
        closes = {
            "A.SH": [1.0 + 0.001 * i for i in range(80)],
            "D.SH": [1.0 + (-1) ** i * 0.01 for i in range(80)],
        }
        weights = {"A.SH": 0.5, "D.SH": 0.5}
        rc = compute_risk_contribution(weights, closes, u, vol_lookback=60)
        assert len(rc) >= 1
        total = sum(rc.values())
        assert total == pytest.approx(1.0, abs=0.01)

    def test_empty_weights(self) -> None:
        u = _universe()
        assert compute_risk_contribution({}, {}, u) == {}


class TestCapitalTierFeasibility:
    def test_feasible_large_capital(self) -> None:
        u = _universe()
        weights = {"A.SH": 0.3, "D.SH": 0.2}
        assert check_capital_tier_feasibility(weights, u, 5e5) is True

    def test_infeasible_small_capital(self) -> None:
        u = _universe()
        weights = {"A.SH": 0.001}
        # 100k * 0.001 = 100, lot_size=100, min_price=1 → min_value=100
        # Exactly at boundary
        assert check_capital_tier_feasibility(weights, u, 1e5) is True

    def test_default_lot_size(self) -> None:
        u = _universe()
        member = u.get("511010.SH")
        assert member is not None
        assert member.lot_size == 100  # default lot_size

    def test_empty_weights(self) -> None:
        u = _universe()
        assert check_capital_tier_feasibility({}, u, 1e5) is True


class TestMaxDrawdownDuration:
    def test_no_drawdown(self) -> None:
        curve = [1.0, 1.1, 1.2, 1.3]
        assert compute_max_drawdown_duration(curve) == 0

    def test_simple_drawdown(self) -> None:
        curve = [1.0, 0.9, 0.8, 0.9, 1.0]
        dd = compute_max_drawdown_duration(curve)
        assert dd == 3  # days 1-3 below peak

    def test_still_in_drawdown(self) -> None:
        curve = [1.0, 0.9, 0.8]
        dd = compute_max_drawdown_duration(curve)
        assert dd == 2

    def test_multiple_drawdowns(self) -> None:
        curve = [1.0, 0.95, 1.0, 0.90, 0.85, 0.95, 1.0]
        dd = compute_max_drawdown_duration(curve)
        assert dd >= 3

    def test_short_curve(self) -> None:
        assert compute_max_drawdown_duration([1.0]) == 0
        assert compute_max_drawdown_duration([]) == 0


class TestAnalyzeEtfRotation:
    def test_full_analysis(self) -> None:
        u = _universe()
        closes = {
            "A.SH": [1.0 + 0.001 * i for i in range(80)],
            "D.SH": [1.0 + (-1) ** i * 0.01 for i in range(80)],
        }
        weight_history = [
            {"A.SH": 0.4, "D.SH": 0.1},
            {"A.SH": 0.3, "D.SH": 0.2},
        ]
        equity = [1.0, 1.01, 1.02, 1.01, 1.03]
        cfg = EtfRotationConfig()
        report = analyze_etf_rotation(
            weight_history, closes, equity, u, cfg,
            flight_to_safety_count=1,
        )
        assert report.avg_n_holdings == 2.0
        assert report.n_rebalances == 1
        assert report.n_flight_to_safety == 1
        assert "equity" in report.asset_class_exposure
        assert report.avg_turnover >= 0
        assert "100k" in report.capital_tier_feasibility

    def test_empty_history(self) -> None:
        u = _universe()
        cfg = EtfRotationConfig()
        report = analyze_etf_rotation([], {}, [1.0, 1.0], u, cfg)
        assert report.avg_n_holdings == 0.0
        assert report.avg_turnover == 0.0
