"""调仓调度与漂移检查测试。"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_backtest.etf_rotation import (
    EtfRotationConfig,
    RebalanceFrequency,
    should_rebalance,
)


class TestShouldRebalance:
    def test_scheduled_rebalance(self) -> None:
        cfg = EtfRotationConfig(rebalance_frequency=RebalanceFrequency.MONTHLY)
        current = {"A": 0.3, "B": 0.3}
        target = {"A": 0.3, "B": 0.3}
        result = should_rebalance(
            current, target,
            last_rebalance_date=date(2024, 1, 1),
            current_date=date(2024, 2, 15),  # > 21 trading days
            config=cfg,
        )
        assert result.should_rebalance
        assert result.reason == "scheduled"

    def test_no_change_within_interval(self) -> None:
        cfg = EtfRotationConfig()
        current = {"A": 0.5, "B": 0.5}
        target = {"A": 0.5, "B": 0.5}
        result = should_rebalance(
            current, target,
            last_rebalance_date=date(2024, 1, 1),
            current_date=date(2024, 1, 5),  # only 4 days
            config=cfg,
        )
        assert not result.should_rebalance
        assert result.reason == "no_change"

    def test_drift_exceeded(self) -> None:
        cfg = EtfRotationConfig(rebalance_threshold=0.05)
        current = {"A": 0.5, "B": 0.5}
        target = {"A": 0.3, "B": 0.7}
        result = should_rebalance(
            current, target,
            last_rebalance_date=date(2024, 1, 1),
            current_date=date(2024, 1, 3),
            config=cfg,
        )
        # drift = |0.3-0.5| = 0.2 > 0.05
        assert result.should_rebalance
        assert result.reason == "drift_exceeded"
        assert result.max_drift == pytest.approx(0.2)

    def test_drift_within_threshold(self) -> None:
        cfg = EtfRotationConfig(rebalance_threshold=0.10)
        current = {"A": 0.50, "B": 0.50}
        target = {"A": 0.47, "B": 0.53}
        result = should_rebalance(
            current, target,
            last_rebalance_date=date(2024, 1, 1),
            current_date=date(2024, 1, 3),
            config=cfg,
        )
        # drift = 0.03 < 0.10
        assert not result.should_rebalance

    def test_new_symbol_drift(self) -> None:
        cfg = EtfRotationConfig(rebalance_threshold=0.05)
        current = {"A": 0.50, "B": 0.50}
        target = {"A": 0.40, "B": 0.40, "C": 0.20}
        result = should_rebalance(
            current, target,
            last_rebalance_date=date(2024, 1, 1),
            current_date=date(2024, 1, 3),
            config=cfg,
        )
        assert result.should_rebalance
        assert result.max_drift == pytest.approx(0.20)  # C: 0->0.20

    def test_removed_symbol_drift(self) -> None:
        cfg = EtfRotationConfig(rebalance_threshold=0.05)
        current = {"A": 0.33, "B": 0.33, "C": 0.34}
        target = {"A": 0.50, "B": 0.50}
        result = should_rebalance(
            current, target,
            last_rebalance_date=date(2024, 1, 1),
            current_date=date(2024, 1, 3),
            config=cfg,
        )
        assert result.should_rebalance

    def test_biweekly_interval(self) -> None:
        cfg = EtfRotationConfig(rebalance_frequency=RebalanceFrequency.BIWEEKLY)
        result = should_rebalance(
            {"A": 0.5}, {"A": 0.5},
            last_rebalance_date=date(2024, 1, 1),
            current_date=date(2024, 1, 12),
            config=cfg,
        )
        assert result.should_rebalance
        assert result.reason == "scheduled"

    def test_trading_days_between_override(self) -> None:
        cfg = EtfRotationConfig()
        result = should_rebalance(
            {"A": 0.5}, {"A": 0.5},
            last_rebalance_date=date(2024, 1, 1),
            current_date=date(2024, 1, 2),
            config=cfg,
            trading_days_between=25,
        )
        assert result.should_rebalance
        assert result.reason == "scheduled"

    def test_empty_weights_no_drift(self) -> None:
        cfg = EtfRotationConfig()
        result = should_rebalance(
            {}, {},
            last_rebalance_date=date(2024, 1, 1),
            current_date=date(2024, 1, 2),
            config=cfg,
        )
        assert not result.should_rebalance
        assert result.max_drift == 0.0
