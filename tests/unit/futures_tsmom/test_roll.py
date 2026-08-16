"""换月 / 展期 / 连续序列测试。"""

from __future__ import annotations

import pytest

from finboard_backtest.futures_tsmom import (
    build_active_series,
    build_continuous_series,
)
from finboard_shared.types import AdjustmentMethod, RollMethod


def _make_front_next(
    n: int,
    crossover_idx: int,
    front_price: float = 3500.0,
    next_price: float = 3510.0,
) -> tuple[list[float], list[float], list[float], list[float]]:
    front_closes = [front_price + i * 0.5 for i in range(n)]
    next_closes = [next_price + i * 0.5 for i in range(n)]
    front_vols = [1000.0] * n
    next_vols = [500.0] * n
    for i in range(crossover_idx, n):
        next_vols[i] = 2000.0
        front_vols[i] = 300.0
    return front_closes, next_closes, front_vols, next_vols


class TestBuildActiveSeries:
    def test_no_roll_without_next_data(self) -> None:
        series = build_active_series(
            symbol="IF",
            front_closes=[3500.0, 3501.0, 3502.0],
            front_opens=[3499.0, 3500.0, 3501.0],
            front_volumes=[1000.0, 1200.0, 1100.0],
        )
        assert series.roll_count == 0
        assert len(series.closes) == 3
        assert all(cid == "FRONT" for cid in series.contract_ids)

    def test_volume_crossover_roll(self) -> None:
        front_closes, next_closes, front_vols, next_vols = _make_front_next(50, crossover_idx=30)
        series = build_active_series(
            symbol="IF",
            front_closes=front_closes,
            front_opens=front_closes,
            front_volumes=front_vols,
            next_closes=next_closes,
            next_volumes=next_vols,
            roll_lookback=5,
        )
        assert series.roll_count >= 1
        ev = series.roll_events[0]
        assert ev.from_contract == "FRONT"
        assert ev.to_contract == "NEXT"

    def test_roll_yield_calculation(self) -> None:
        series = build_active_series(
            symbol="IF",
            front_closes=[3500.0] * 20,
            front_opens=[3500.0] * 20,
            front_volumes=[100.0] * 20,
            next_closes=[3510.0] * 20,
            next_volumes=[200.0] * 20,
            roll_lookback=3,
        )
        assert series.roll_count >= 1
        ev = series.roll_events[0]
        expected_ry = (3500.0 - 3510.0) / 3510.0
        assert ev.roll_yield == pytest.approx(expected_ry, rel=1e-4)
        assert ev.is_contango  # front < next → contango

    def test_backwardation(self) -> None:
        series = build_active_series(
            symbol="T",
            front_closes=[101.0] * 20,
            front_opens=[101.0] * 20,
            front_volumes=[100.0] * 20,
            next_closes=[100.0] * 20,
            next_volumes=[200.0] * 20,
            roll_lookback=3,
        )
        assert series.roll_count >= 1
        ev = series.roll_events[0]
        assert ev.roll_yield > 0
        assert ev.is_backwardation

    def test_scheduled_roll(self) -> None:
        front_closes = [3500.0 + i for i in range(30)]
        series = build_active_series(
            symbol="IF",
            front_closes=front_closes,
            front_opens=front_closes,
            front_volumes=[1000.0] * 30,
            next_closes=[3510.0 + i for i in range(30)],
            next_volumes=[500.0] * 30,
            roll_method=RollMethod.SCHEDULED,
            forced_roll_indices=[15],
        )
        assert series.roll_count == 1
        assert series.roll_events[0].bar_index == 15
        assert series.roll_events[0].reason == "scheduled"

    def test_empty_closes_raises(self) -> None:
        with pytest.raises(ValueError, match="front_closes 不能为空"):
            build_active_series(
                symbol="IF",
                front_closes=[],
                front_opens=[],
                front_volumes=[],
            )

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="front_opones 长度"):
            build_active_series(
                symbol="IF",
                front_closes=[1.0, 2.0, 3.0],
                front_opens=[1.0],
                front_volumes=[1.0, 2.0, 3.0],
            )

    def test_roll_yield_total(self) -> None:
        front_closes = [100.0] * 30
        next_closes = [99.0] * 30
        front_vols = [100.0] * 30
        next_vols = [50.0] * 30
        for i in range(10, 30):
            next_vols[i] = 200.0
            front_vols[i] = 30.0
        series = build_active_series(
            symbol="X",
            front_closes=front_closes,
            front_opens=front_closes,
            front_volumes=front_vols,
            next_closes=next_closes,
            next_volumes=next_vols,
            roll_lookback=3,
        )
        assert series.roll_yield_total > 0


class TestContinuousSeries:
    def test_ratio_adjustment(self) -> None:
        front_closes, next_closes, front_vols, next_vols = _make_front_next(50, crossover_idx=25)
        active = build_active_series(
            symbol="IF",
            front_closes=front_closes,
            front_opens=front_closes,
            front_volumes=front_vols,
            next_closes=next_closes,
            next_volumes=next_vols,
            roll_lookback=5,
        )
        cont = build_continuous_series(active, method=AdjustmentMethod.RATIO)
        assert len(cont.adjusted_closes) == len(active.closes)
        assert cont.adjustment_method == "ratio"

    def test_none_adjustment(self) -> None:
        active = build_active_series(
            symbol="IF",
            front_closes=[100.0, 101.0, 102.0],
            front_opens=[100.0, 101.0, 102.0],
            front_volumes=[100.0, 200.0, 300.0],
        )
        cont = build_continuous_series(active, method=AdjustmentMethod.NONE)
        assert cont.adjusted_closes == active.closes

    def test_difference_adjustment(self) -> None:
        front_closes, next_closes, front_vols, next_vols = _make_front_next(30, crossover_idx=15)
        active = build_active_series(
            symbol="IF",
            front_closes=front_closes,
            front_opens=front_closes,
            front_volumes=front_vols,
            next_closes=next_closes,
            next_volumes=next_vols,
            roll_lookback=3,
        )
        cont = build_continuous_series(active, method=AdjustmentMethod.DIFFERENCE)
        assert len(cont.adjusted_closes) == len(active.closes)

    def test_continuous_not_equal_to_raw_after_roll(self) -> None:
        front_closes, next_closes, front_vols, next_vols = _make_front_next(50, crossover_idx=25)
        active = build_active_series(
            symbol="IF",
            front_closes=front_closes,
            front_opens=front_closes,
            front_volumes=front_vols,
            next_closes=next_closes,
            next_volumes=next_vols,
            roll_lookback=5,
        )
        cont = build_continuous_series(active, method=AdjustmentMethod.RATIO)
        if active.roll_count > 0:
            last_idx = active.roll_events[-1].bar_index
            assert cont.adjusted_closes[last_idx + 1] != active.closes[last_idx + 1]
