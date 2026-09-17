"""issue #401:批次 3 财务预置因子(Growth / Quality)的目录与逐值单测。

* 目录不变量:40 个 ``fin_*`` 条目全部声明 ``financial_indicators.<field>``
  数据依赖、无参数化窗口、方向登记;LOWER 方向因子名单锁定;
* **逐值对照** —— 水平因子 = 公告步进函数(决策日取「可见的最近一次
  公告」值,修订公告覆盖,公告前缺测);加速度因子 = 同比增速的公告序
  一阶差分,与独立 pandas 参考逐值一致;
* 输入契约:``PredefinedFactorInput.financial_indicators`` 为必实现取数口。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from finboard_backtest.factors.predefined.context import (
    FactorSeriesFrame,
    PredefinedFactorInput,
    SymbolSeries,
    sample_series_frame,
)
from finboard_backtest.factors.predefined.registry import (
    PREDEFINED_FACTORS,
    get_predefined_factor,
    predefined_factor_names,
)
from finboard_data.factor_lab import FactorPreference

ANN_A = date(2024, 4, 26)  # Q1 公告日
ANN_B = date(2024, 8, 20)  # 半年报公告日
ANN_C = date(2024, 10, 25)  # Q3 公告日


def _ann_series(
    announcements: list[tuple[date, float | None]],
) -> SymbolSeries:
    """公告序列:每行一次公告,available_at = 公告日次日 00:00(PIT 锚)。"""
    dates = tuple(day for day, _ in announcements)
    values = np.array(
        [math.nan if v is None else float(v) for _, v in announcements],
        dtype=np.float64,
    )
    available_at = tuple(
        datetime.combine(
            day + timedelta(days=1),
            datetime.min.time(),
            tzinfo=UTC,
        )
        for day in dates
    )
    return SymbolSeries(dates=dates, values=values, available_at=available_at)


class _FakeInput(PredefinedFactorInput):
    """测试输入面:bars 为空、财务公告按字段注入。"""

    def __init__(
        self,
        financial: dict[str, dict[str, SymbolSeries]],
        *,
        decision_dates: tuple[date, ...],
        universe: tuple[str, ...],
    ) -> None:
        super().__init__(
            factor_name="test",
            decision_dates=decision_dates,
            tradable_symbols=universe,
            benchmark_only_symbols=frozenset(),
        )
        self._financial = financial
        self._universe = universe

    def bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        return {}

    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        return {}

    def financial_indicators(self, field: str) -> dict[str, SymbolSeries]:
        return dict(self._financial.get(field, {}))

    def research_dataset(self, kind: str, field: str) -> dict[str, SymbolSeries]:
        if kind == "financial_indicators":
            return self.financial_indicators(field)
        return {}

    def dividend_events(self) -> dict[str, Any]:
        return {}

    def industry_groups(self) -> dict[str, str | None]:
        return {}

    def sample(
        self,
        series_by_symbol,
        per_symbol_values,
    ) -> FactorSeriesFrame:
        return sample_series_frame(
            series_by_symbol,
            per_symbol_values,
            decision_dates=self.decision_dates,
            value_universe=tuple(
                s for s in self._universe if s in per_symbol_values
            ),
        )


class TestFinancialCatalog:
    def test_40_financial_factors_registered(self) -> None:
        names = predefined_factor_names()
        fin = [name for name in names if name.startswith("fin_")]
        assert len(fin) == 40

    def test_dependencies_and_shape(self) -> None:
        for name in (n for n in PREDEFINED_FACTORS if n.startswith("fin_")):
            item = get_predefined_factor(name)
            assert len(item.data_dependencies) == 1
            dep = item.data_dependencies[0]
            assert dep.startswith("financial_indicators."), name
            field = dep.split(".", 1)[1]
            # 依赖字段必须真实存在于领域 dataclass(= 发布白名单口径)
            from finboard_data.research import FinancialIndicator

            assert field in FinancialIndicator.__dataclass_fields__, name
            assert item.window is None, name
            assert item.signal_eligible is True, name
            assert item.implementation_version == "1", name
            assert item.family in {"growth", "quality"}, name

    def test_lower_direction_factors_locked(self) -> None:
        lowered = {
            name
            for name in PREDEFINED_FACTORS
            if name.startswith("fin_")
            and PREDEFINED_FACTORS[name].direction is FactorPreference.LOWER
        }
        assert lowered == {
            "fin_debt_to_assets",
            "fin_debt_to_equity",
            "fin_equity_multiplier",
            "fin_expense_ratio",
        }

    def test_acceleration_factors_use_delta_compute(self) -> None:
        for name in ("fin_np_yoy_accel", "fin_revenue_yoy_accel"):
            item = get_predefined_factor(name)
            plain = get_predefined_factor(
                "fin_netprofit_yoy" if name == "fin_np_yoy_accel"
                else "fin_revenue_yoy"
            )
            assert item.compute is not plain.compute
            assert item.data_dependencies == plain.data_dependencies


class TestLevelFactorValues:
    def test_step_function_with_revision_and_gap(self) -> None:
        """公告步进语义:公告前缺测 / 公告间持有 / 修订覆盖旧值。"""
        # Q1(4/26 公告)= 0.05,修订(6/2 公告)= 0.055,半年报(8/20)= 0.11
        ann = _ann_series(
            [
                (ANN_A, 0.05),
                (date(2024, 6, 2), 0.055),  # Q1 修订
                (ANN_B, 0.11),
            ]
        )
        decisions = (
            date(2024, 4, 26),  # 公告日当天(available_at = 4/27)→ None
            date(2024, 4, 27),  # PIT 锚点日 → 0.05
            date(2024, 6, 5),  # 修订(6/2 公告,6/3 可见)后 → 0.055
            date(2024, 7, 15),  # 公告间 → 仍 0.055
            date(2024, 8, 21),  # 半年报公告次日 → 0.11
        )
        inp = _FakeInput(
            {"return_on_equity": {"600000.SH": ann}},
            decision_dates=decisions,
            universe=("600000.SH",),
        )
        frame = get_predefined_factor("fin_roe").compute(inp)
        assert frame[decisions[0]]["600000.SH"] is None
        assert frame[decisions[1]]["600000.SH"] == pytest.approx(0.05)
        assert frame[decisions[2]]["600000.SH"] == pytest.approx(0.055)
        assert frame[decisions[3]]["600000.SH"] == pytest.approx(0.055)
        assert frame[decisions[4]]["600000.SH"] == pytest.approx(0.11)

    def test_null_announcement_value_is_none(self) -> None:
        ann = _ann_series([(ANN_A, None), (ANN_B, 0.32)])
        decisions = (date(2024, 5, 8), date(2024, 8, 21))
        inp = _FakeInput(
            {"netprofit_qoq": {"600000.SH": ann}},
            decision_dates=decisions,
            universe=("600000.SH",),
        )
        frame = get_predefined_factor("fin_netprofit_qoq").compute(inp)
        assert frame[decisions[0]]["600000.SH"] is None
        assert frame[decisions[1]]["600000.SH"] == pytest.approx(0.32)

    def test_symbols_without_announcements_absent(self) -> None:
        ann = _ann_series([(ANN_A, 0.05)])
        inp = _FakeInput(
            {"return_on_equity": {"600000.SH": ann}},
            decision_dates=(date(2024, 5, 8),),
            universe=("600000.SH", "000001.SZ", "600519.SH"),
        )
        frame = get_predefined_factor("fin_roe").compute(inp)
        # 无公告标的 = 结构性缺测,不进截面(与停牌缺行同语义)
        assert set(frame[date(2024, 5, 8)]) == {"600000.SH"}


class TestAccelerationFactorValues:
    def test_accel_matches_pandas_diff_reference(self) -> None:
        """加速度 = 同比增速的公告序一阶差分;首条公告 → None。"""
        growth = [0.10, 0.08, 0.12, 0.09, None]
        ann = _ann_series([(day, v) for day, v in zip((ANN_A, ANN_B, ANN_C, date(2025, 4, 25), date(2025, 8, 22)), growth, strict=True)])
        decisions = (
            date(2024, 5, 8),
            date(2024, 9, 2),
            date(2024, 11, 1),
            date(2025, 5, 6),
            date(2025, 9, 1),
        )
        inp = _FakeInput(
            {"net_profit_yoy": {"600000.SH": ann}},
            decision_dates=decisions,
            universe=("600000.SH",),
        )
        frame = get_predefined_factor("fin_np_yoy_accel").compute(inp)
        series = pd.Series(
            [math.nan if v is None else v for v in growth], dtype="float64"
        )
        reference = series.diff()
        for day, expected in zip(decisions, reference, strict=True):
            got = frame[day]["600000.SH"]
            if math.isnan(expected):
                assert got is None
            else:
                assert got == pytest.approx(float(expected), rel=1e-12)

    def test_accel_single_announcement_is_none(self) -> None:
        ann = _ann_series([(ANN_A, 0.1)])
        inp = _FakeInput(
            {"revenue_yoy": {"600000.SH": ann}},
            decision_dates=(date(2024, 5, 8),),
            universe=("600000.SH",),
        )
        frame = get_predefined_factor("fin_revenue_yoy_accel").compute(inp)
        assert frame[date(2024, 5, 8)]["600000.SH"] is None
