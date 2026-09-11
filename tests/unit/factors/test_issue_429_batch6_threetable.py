"""因子批次 6(#429)P2:三表新列消费与其余 15 个的目录不变量与逐值单测。

* 目录不变量(批次 scope,无全局 exact-total 断言):15 个全部注册,
  family / direction / signal_eligible / cross_section / window /
  min_history_bars / data_dependencies 逐项精确,commit 锚互异;
* 同比族(``t-4`` = 公告序往前 4 行 = 去年同期)的行轴比值机制逐值对照:
  独立参考实现按契约直接推「其它科目按 ``asof(该行公告日)`` 对齐 → 逐行
  比值 → 4 行滞后同比」,与实现逐值一致;基期 <= 0 / 公告行不足 5 行 /
  required 科目缺测 → 缺测;
* 变化族(Δ = 行轴比值_t - 行轴比值_{t-252 根 bar PIT})复用 #429 P1 的
  bar 回看机制:独立参考逐值一致 + 前缀不变性(截断挂载下前缀逐值不变)
  + bar 覆盖不足缺测 + 无 bar 日历缺测(而纯公告序同比族照常出值);
* 水平比值族(决策日两端 asof 后比值 + 截面 rank)与其余三个(反转 /
  PEG / ETP5)的手锚值(手算期望)与缺测边界;
* 扩展 helper ``_financial_step_ratio_rank`` 的 ``lag`` 参数:默认 1 的
  存量行为不变(逐值对照 P1 条目)、``lag=4`` = 公告序去年同期。

诚实边界(代理口径)见各因子 title;纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import numpy as np
import pytest

from finboard_backtest.factors.predefined.context import (
    FactorSeriesFrame,
    PredefinedFactorInput,
    SymbolSeries,
    sample_series_frame,
)
from finboard_backtest.factors.predefined.registry import (
    PREDEFINED_FACTORS,
    _financial_step_ratio_rank,
    get_predefined_factor,
    predefined_factor_commit,
    predefined_factor_names,
)
from finboard_data.factor_lab import FactorPreference

#: 行轴比值的一条成分:(kind, field, sign, required)
_Comp = tuple[str, str, float, bool]

YOY_SELF_NAMES = ("income_tax_yoy", "tax_surcharge_yoy")
YOY_RATIO_NAMES = (
    "np_to_inventory_yoy",
    "np_to_total_expenses_yoy",
    "expenses_to_equity_yoy",
    "np_to_fixed_assets_yoy",
    "np_to_salary_yoy",
)
DELTA_NAMES = ("delta_opm", "delta_cash_ratio")
LEVEL_RATIO_NAMES = (
    "market_value_leverage",
    "cash_ratio",
    "earnings_cut_to_market",
)
OTHER_NAMES = ("small_cap_reversal_21d", "peg_252d", "etp5")
BATCH6_P2_NAMES = frozenset(
    set(YOY_SELF_NAMES)
    | set(YOY_RATIO_NAMES)
    | set(DELTA_NAMES)
    | set(LEVEL_RATIO_NAMES)
    | set(OTHER_NAMES)
)

#: 逐因子精确目录字段(与 tushare factor_list 2026-09-11 快照名一致)
FAMILY_MAP: dict[str, str] = {
    **dict.fromkeys(
        (*YOY_SELF_NAMES, *YOY_RATIO_NAMES, *DELTA_NAMES, "market_value_leverage",
         "cash_ratio"),
        "quality",
    ),
    "earnings_cut_to_market": "value",
    "small_cap_reversal_21d": "reversal",
    "peg_252d": "growth",
    "etp5": "value",
}
WINDOW_MAP: dict[str, int | None] = {
    **dict.fromkeys(BATCH6_P2_NAMES),
    "delta_opm": 252,
    "delta_cash_ratio": 252,
    "peg_252d": 252,
    "etp5": 1260,
    "small_cap_reversal_21d": 21,
}
MIN_HISTORY_MAP: dict[str, int | None] = {
    **dict.fromkeys(BATCH6_P2_NAMES),
    "delta_opm": 252,
    "delta_cash_ratio": 252,
    "peg_252d": 252,
    "etp5": 1260,
}
CROSS_SECTION_MAP: dict[str, bool] = {
    **dict.fromkeys(BATCH6_P2_NAMES, True),
    "etp5": False,
}
DEPENDENCIES_MAP: dict[str, tuple[str, ...]] = {
    "income_tax_yoy": ("income_statements.income_tax",),
    "tax_surcharge_yoy": ("income_statements.biz_tax_surchg",),
    "np_to_inventory_yoy": (
        "income_statements.n_income",
        "balance_sheets.inventories",
    ),
    "np_to_total_expenses_yoy": (
        "income_statements.n_income",
        "income_statements.sell_exp",
        "income_statements.admin_exp",
        "income_statements.fin_exp",
    ),
    "expenses_to_equity_yoy": (
        "income_statements.sell_exp",
        "income_statements.admin_exp",
        "income_statements.fin_exp",
        "balance_sheets.total_hldr_eqy_exc_min_int",
    ),
    "np_to_fixed_assets_yoy": (
        "income_statements.n_income",
        "balance_sheets.fix_assets",
    ),
    "np_to_salary_yoy": (
        "income_statements.n_income",
        "cashflow_statements.c_paid_to_for_empl",
    ),
    "delta_opm": (
        "income_statements.operate_profit",
        "income_statements.revenue",
        "bars.close",
    ),
    "delta_cash_ratio": (
        "balance_sheets.money_cap",
        "balance_sheets.trading_fl",
        "balance_sheets.total_cur_liab",
        "bars.close",
    ),
    "market_value_leverage": (
        "daily_metrics.total_market_cap",
        "balance_sheets.total_ncl",
    ),
    "cash_ratio": (
        "balance_sheets.money_cap",
        "balance_sheets.trading_fl",
        "balance_sheets.total_cur_liab",
    ),
    "earnings_cut_to_market": (
        "income_statements.n_income_attr_p",
        "daily_metrics.total_market_cap",
    ),
    "small_cap_reversal_21d": ("bars.close",),
    "peg_252d": ("financial_indicators.eps", "bars.close"),
    "etp5": (
        "income_statements.n_income",
        "daily_metrics.total_market_cap",
        "bars.close",
    ),
}

#: 独立参考口径(与实现解耦):行轴比值成分
YOY_RATIO_REF_SPECS: tuple[
    tuple[str, tuple[str, str], tuple[_Comp, ...], tuple[_Comp, ...]], ...
] = (
    (
        "np_to_inventory_yoy",
        ("income_statements", "n_income"),
        (("income_statements", "n_income", 1.0, True),),
        (("balance_sheets", "inventories", 1.0, True),),
    ),
    (
        "np_to_total_expenses_yoy",
        ("income_statements", "n_income"),
        (("income_statements", "n_income", 1.0, True),),
        (
            ("income_statements", "sell_exp", 1.0, True),
            ("income_statements", "admin_exp", 1.0, True),
            ("income_statements", "fin_exp", 1.0, True),
        ),
    ),
    (
        "expenses_to_equity_yoy",
        ("income_statements", "sell_exp"),
        (
            ("income_statements", "sell_exp", 1.0, True),
            ("income_statements", "admin_exp", 1.0, True),
            ("income_statements", "fin_exp", 1.0, True),
        ),
        (("balance_sheets", "total_hldr_eqy_exc_min_int", 1.0, True),),
    ),
    (
        "np_to_fixed_assets_yoy",
        ("income_statements", "n_income"),
        (("income_statements", "n_income", 1.0, True),),
        (("balance_sheets", "fix_assets", 1.0, True),),
    ),
    (
        "np_to_salary_yoy",
        ("income_statements", "n_income"),
        (("income_statements", "n_income", 1.0, True),),
        (("cashflow_statements", "c_paid_to_for_empl", 1.0, True),),
    ),
)
DELTA_REF_SPECS: tuple[
    tuple[str, tuple[str, str], tuple[_Comp, ...], tuple[_Comp, ...]], ...
] = (
    (
        "delta_opm",
        ("income_statements", "operate_profit"),
        (("income_statements", "operate_profit", 1.0, True),),
        (("income_statements", "revenue", 1.0, True),),
    ),
    (
        "delta_cash_ratio",
        ("balance_sheets", "money_cap"),
        (
            ("balance_sheets", "money_cap", 1.0, True),
            ("balance_sheets", "trading_fl", 1.0, False),
        ),
        (("balance_sheets", "total_cur_liab", 1.0, True),),
    ),
)

_BASE_DAY = date(2020, 1, 1)
_YEAR_BARS = 252
_YOY_ROWS = 4
_ETP5_BARS = 1260


def _day(offset: int) -> date:
    return _BASE_DAY + timedelta(days=offset)


def _bars(days: Sequence[date], close: float = 10.0) -> SymbolSeries:
    """bar 日历序列(dates 为交易日;收盘常数便于手算)。"""
    return _bars_values(days, np.full(len(days), close, dtype=np.float64))


def _bars_values(days: Sequence[date], values: np.ndarray) -> SymbolSeries:
    return SymbolSeries(
        dates=tuple(days),
        values=np.asarray(values, dtype=np.float64),
        available_at=tuple(
            datetime.combine(item, time(23, 59, 59, 999999), tzinfo=UTC)
            for item in days
        ),
    )


def _announcements(rows: Sequence[tuple[date, float | None]]) -> SymbolSeries:
    """公告序列:available_at = 公告日次日 00:00(#401 同契约定式)。"""
    dates = tuple(item for item, _ in rows)
    values = np.array(
        [math.nan if value is None else float(value) for _, value in rows],
        dtype=np.float64,
    )
    available_at = tuple(
        datetime.combine(
            item + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        )
        for item in dates
    )
    return SymbolSeries(dates=dates, values=values, available_at=available_at)


class _Input(PredefinedFactorInput):
    """测试输入面:bar 日历 + 公告类数据集(三表 / financial_indicators)。"""

    def __init__(
        self,
        *,
        bars: dict[str, SymbolSeries],
        decision_dates: tuple[date, ...],
        datasets: dict[str, dict[str, dict[str, SymbolSeries]]] | None = None,
        daily: dict[str, dict[str, SymbolSeries]] | None = None,
        tradable: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(
            factor_name="batch6_threetable",
            decision_dates=decision_dates,
            tradable_symbols=tuple(bars) if tradable is None else tradable,
            benchmark_only_symbols=frozenset(),
        )
        self._bars = dict(bars)
        self._datasets = datasets or {}
        self._daily = daily or {}

    def bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        return dict(self._bars) if field == "close" else {}

    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        return dict(self._daily.get(field, {}))

    def financial_indicators(self, field: str) -> dict[str, SymbolSeries]:
        return self.research_dataset("financial_indicators", field)

    def research_dataset(self, kind: str, field: str) -> dict[str, SymbolSeries]:
        return dict(self._datasets.get(kind, {}).get(field, {}))

    def dividend_events(self) -> dict[str, Any]:
        return {}

    def industry_groups(self) -> dict[str, str | None]:
        return {}

    def sample(
        self,
        series_by_symbol: Mapping[str, SymbolSeries],
        per_symbol_values: Mapping[str, np.ndarray],
    ) -> FactorSeriesFrame:
        return sample_series_frame(
            series_by_symbol,
            per_symbol_values,
            decision_dates=self.decision_dates,
            value_universe=tuple(
                symbol
                for symbol in per_symbol_values
                if symbol in self.tradable_symbols
            ),
        )


def _compute(name: str, inp: PredefinedFactorInput) -> FactorSeriesFrame:
    return get_predefined_factor(name).compute(inp)


def _truncate(
    series_by_symbol: Mapping[str, SymbolSeries], cut: date
) -> dict[str, SymbolSeries]:
    """截断挂载变体:仅保留 ``available_at <= cut`` 日终的行(前缀子序列)。"""
    ceiling = datetime.combine(cut, time(23, 59, 59, 999999), tzinfo=UTC)
    out: dict[str, SymbolSeries] = {}
    for symbol, item in series_by_symbol.items():
        keep = [
            index
            for index, stamp in enumerate(item.available_at)
            if stamp <= ceiling
        ]
        out[symbol] = SymbolSeries(
            dates=tuple(item.dates[index] for index in keep),
            values=item.values[keep],
            available_at=tuple(item.available_at[index] for index in keep),
        )
    return out


def _truncate_datasets(
    datasets: Mapping[str, Mapping[str, Mapping[str, SymbolSeries]]], cut: date
) -> dict[str, dict[str, dict[str, SymbolSeries]]]:
    return {
        kind: {
            field: _truncate(series, cut)
            for field, series in fields.items()
        }
        for kind, fields in datasets.items()
    }


# --------------------------------------------------------------------- #
# 独立参考实现(按契约直接推,不复用 registry 内部函数)
# --------------------------------------------------------------------- #


def _visible_row(ann: SymbolSeries, day: date) -> int:
    """ann 在 day 日终可见的最后一行下标(-1 = 无可见行)。"""
    limit = datetime.combine(day, time(23, 59, 59, 999999), tzinfo=UTC)
    position = -1
    for index, available in enumerate(ann.available_at):
        if available <= limit:
            position = index
    return position


def _bar_position(cal: SymbolSeries, day: date) -> int:
    """bar 日历上 date <= day 的最后一根下标(-1 = 无)。"""
    position = -1
    for index, item in enumerate(cal.dates):
        if item <= day:
            position = index
    return position


def _ref_combine(
    datasets: Mapping[str, Mapping[str, Mapping[str, SymbolSeries]]],
    symbol: str,
    anchor: datetime,
    fields: Sequence[_Comp],
) -> float:
    """按行可见锚 PIT 对齐成分并按符号合成(缺测语义同 ``_NumComponent``)。"""
    total = 0.0
    any_value = False
    blocked = False
    for kind, field, sign, required in fields:
        series = datasets.get(kind, {}).get(field, {}).get(symbol)
        row = -1 if series is None else series.position_asof(anchor)
        value = (
            math.nan if series is None or row < 0 else float(series.values[row])
        )
        if not math.isfinite(value):
            blocked = blocked or required
            continue
        total += sign * value
        any_value = True
    if blocked or not any_value:
        return math.nan
    return total


def _ref_ratio_rows(
    datasets: Mapping[str, Mapping[str, Mapping[str, SymbolSeries]]],
    symbol: str,
    driver: SymbolSeries,
    numerator: Sequence[_Comp],
    denominator: Sequence[_Comp],
) -> list[float]:
    """逐驱动行的参考比值(分母 <= 0 / 任一端缺测 → NaN)。"""
    rows: list[float] = []
    for anchor in driver.available_at:
        num = _ref_combine(datasets, symbol, anchor, numerator)
        den = _ref_combine(datasets, symbol, anchor, denominator)
        if not (math.isfinite(num) and math.isfinite(den)) or den <= 0.0:
            rows.append(math.nan)
        else:
            rows.append(num / den)
    return rows


def _ref_lookback_row(
    ann: SymbolSeries, cal: SymbolSeries, day: date, lookback: int
) -> int:
    """回看基值所在行:「公告日往前 lookback 根 bar」时点 PIT 可见的最后一行。"""
    position = _bar_position(cal, day)
    if position < 0 or position - lookback < 0:
        return -1
    return _visible_row(ann, cal.dates[position - lookback])


def _ref_yoy_sampled(rows: Sequence[float], position: int, lag: int = _YOY_ROWS) -> float:
    """行轴序列在行 position 的同比(基期 <= 0 / 行不足 → NaN)。"""
    if position < 0 or position - lag < 0:
        return math.nan
    current = rows[position]
    base = rows[position - lag]
    if not (math.isfinite(current) and math.isfinite(base)) or base <= 0.0:
        return math.nan
    return current / base - 1.0


def _ref_delta_sampled(
    ann: SymbolSeries, cal: SymbolSeries, rows: Sequence[float], position: int
) -> float:
    """行轴序列在行 position 的 Δ(252 根 bar 回看基值)。"""
    if position < 0:
        return math.nan
    base_row = _ref_lookback_row(ann, cal, ann.dates[position], _YEAR_BARS)
    current = rows[position]
    base = math.nan if base_row < 0 else rows[base_row]
    if not (math.isfinite(current) and math.isfinite(base)):
        return math.nan
    return current - base


def _ref_rank(values: dict[str, float]) -> dict[str, float | None]:
    """截面百分位排名参考 (# finite <= x) / n_finite(与 cs_rank 同口径)。"""
    finite = {symbol: value for symbol, value in values.items() if math.isfinite(value)}
    if not finite:
        return dict.fromkeys(values, None)
    count = len(finite)
    return {
        symbol: (
            sum(1 for other in finite.values() if other <= value) / count
            if math.isfinite(value)
            else None
        )
        for symbol, value in values.items()
    }


def _statement_series(
    symbol_index: int,
    field_index: int,
    *,
    offsets: Sequence[int] = (20, 80, 140, 200, 260, 320, 380, 440, 500, 560, 620),
    nan_rows: frozenset[int] = frozenset(),
) -> SymbolSeries:
    """确定性构造多行公告序列(值随 symbol / field / row 单调变化)。"""
    rows: list[tuple[date, float | None]] = []
    for row_index, offset in enumerate(offsets):
        if row_index in nan_rows:
            rows.append((_day(offset), None))
            continue
        value = 10.0 + 3.0 * symbol_index + 5.0 * field_index + 7.0 * row_index
        rows.append((_day(offset), value))
    return _announcements(rows)


def _datasets_for(
    symbols: Sequence[str],
    fields: Mapping[str, Sequence[str]],
    *,
    nan_rows: tuple[str, str, frozenset[int]] | None = None,
    offsets: Sequence[int] = (20, 80, 140, 200, 260, 320, 380, 440, 500, 560, 620),
) -> dict[str, dict[str, dict[str, SymbolSeries]]]:
    """{kind: {field: {symbol: 公告序列}}} 确定性面板(可注入一处 NaN)。"""
    out: dict[str, dict[str, dict[str, SymbolSeries]]] = {}
    for kind_index, (kind, names) in enumerate(fields.items()):
        out[kind] = {}
        for field_index, name in enumerate(names):
            out[kind][name] = {
                symbol: _statement_series(
                    symbol_index,
                    kind_index * 10 + field_index,
                    offsets=offsets,
                    nan_rows=(
                        nan_rows[2]
                        if nan_rows is not None
                        and nan_rows[0] == kind
                        and nan_rows[1] == name
                        and symbol == symbols[0]
                        else frozenset()
                    ),
                )
                for symbol_index, symbol in enumerate(symbols)
            }
    return out


# --------------------------------------------------------------------- #
# 目录不变量(批次 scope)
# --------------------------------------------------------------------- #


class TestBatch6P2Catalog:
    def test_exactly_15_registered(self) -> None:
        assert len(BATCH6_P2_NAMES) == 15
        assert BATCH6_P2_NAMES.issubset(set(predefined_factor_names()))
        for name in BATCH6_P2_NAMES:
            assert get_predefined_factor(name).name == name

    def test_family_map_exact(self) -> None:
        assert {
            name: PREDEFINED_FACTORS[name].family for name in BATCH6_P2_NAMES
        } == FAMILY_MAP

    def test_direction_map_exact(self) -> None:
        expected = {
            **dict.fromkeys(BATCH6_P2_NAMES, FactorPreference.HIGHER),
            "peg_252d": FactorPreference.LOWER,
        }
        assert {
            name: PREDEFINED_FACTORS[name].direction for name in BATCH6_P2_NAMES
        } == expected

    def test_signal_eligible_and_cross_section_exact(self) -> None:
        assert all(PREDEFINED_FACTORS[name].signal_eligible for name in BATCH6_P2_NAMES)
        assert {
            name: PREDEFINED_FACTORS[name].cross_section for name in BATCH6_P2_NAMES
        } == CROSS_SECTION_MAP

    def test_window_and_min_history_exact(self) -> None:
        assert {
            name: PREDEFINED_FACTORS[name].window for name in BATCH6_P2_NAMES
        } == WINDOW_MAP
        assert {
            name: PREDEFINED_FACTORS[name].min_history_bars
            for name in BATCH6_P2_NAMES
        } == MIN_HISTORY_MAP
        for name in BATCH6_P2_NAMES:
            item = PREDEFINED_FACTORS[name]
            if item.min_history_bars is not None:
                assert item.window is not None, name
                assert item.min_history_bars >= item.window, name

    def test_data_dependencies_exact(self) -> None:
        assert {
            name: PREDEFINED_FACTORS[name].data_dependencies
            for name in BATCH6_P2_NAMES
        } == DEPENDENCIES_MAP

    def test_commit_anchors_distinct(self) -> None:
        commits = {name: predefined_factor_commit(name) for name in BATCH6_P2_NAMES}
        assert len(set(commits.values())) == len(commits)

    def test_titles_bounded_and_single_version(self) -> None:
        for name in BATCH6_P2_NAMES:
            item = PREDEFINED_FACTORS[name]
            assert item.title, name
            assert 0 < len(item.title) <= 120, name
            assert item.implementation_version == "1", name


# --------------------------------------------------------------------- #
# 同比族(行轴比值 + 公告序 4 行滞后)
# --------------------------------------------------------------------- #


class TestRowRatioYoyFamily:
    def test_income_tax_yoy_hand_anchor_lag_four(self) -> None:
        """lag=1 与 lag=4 的排序相反 —— 锁定 4 行滞后(去年同期)口径。"""
        rows = {
            "A.SZ": [100.0, 120.0, 130.0, 140.0, 150.0],  # lag4 = +0.50
            "B.SZ": [100.0, 90.0, 80.0, 70.0, 105.0],  # lag4 = +0.05
            "C.SZ": [100.0, 100.0, 100.0, 100.0, 90.0],  # lag4 = -0.10
        }
        series = {
            symbol: _announcements(
                [(_day(10 * index), value) for index, value in enumerate(values)]
            )
            for symbol, values in rows.items()
        }
        inp = _Input(
            bars={},
            datasets={"income_statements": {"income_tax": series}},
            decision_dates=(_day(41),),
            tradable=tuple(rows),
        )
        # lag=1 的排序为 B(0.50) > A(0.07) > C(-0.10);lag=4 为 A > B > C
        cross = _compute("income_tax_yoy", inp)[_day(41)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(2 / 3)
        assert cross["C.SZ"] == pytest.approx(1 / 3)

    def test_tax_surcharge_yoy_hand_anchor(self) -> None:
        rows = {
            "A.SZ": [200.0, 210.0, 220.0, 230.0, 260.0],
            "B.SZ": [200.0, 210.0, 220.0, 230.0, 300.0],
        }
        series = {
            symbol: _announcements(
                [(_day(10 * index), value) for index, value in enumerate(values)]
            )
            for symbol, values in rows.items()
        }
        inp = _Input(
            bars={},
            datasets={"income_statements": {"biz_tax_surchg": series}},
            decision_dates=(_day(41),),
            tradable=tuple(rows),
        )
        cross = _compute("tax_surcharge_yoy", inp)[_day(41)]
        assert cross["B.SZ"] == pytest.approx(1.0)  # 0.5
        assert cross["A.SZ"] == pytest.approx(0.5)  # 0.3

    def test_np_to_inventory_yoy_hand_anchor(self) -> None:
        """分母(存货)按公告日 PIT 对齐;单标的比值 0.1 → 0.2 = 同比 1.0。"""
        symbols = ("A.SZ", "B.SZ")
        days = [_day(offset) for offset in range(60)]
        bars = {symbol: _bars(days) for symbol in symbols}
        income = {
            "A.SZ": _announcements(
                [
                    (_day(10), 10.0),
                    (_day(20), 12.0),
                    (_day(30), 13.0),
                    (_day(40), 14.0),
                    (_day(50), 20.0),
                ]
            ),
            "B.SZ": _announcements(
                [
                    (_day(10), 20.0),
                    (_day(20), 20.0),
                    (_day(30), 20.0),
                    (_day(40), 20.0),
                    (_day(50), 30.0),
                ]
            ),
        }
        inventories = {
            "A.SZ": _announcements([(_day(0), 100.0)]),
            "B.SZ": _announcements([(_day(0), 200.0)]),
        }
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {"n_income": income},
                "balance_sheets": {"inventories": inventories},
            },
            decision_dates=(_day(51),),
        )
        # A: 0.20 / 0.10 - 1 = 1.0;B: 0.15 / 0.10 - 1 = 0.5
        cross = _compute("np_to_inventory_yoy", inp)[_day(51)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_yoy_base_nonpositive_is_none(self) -> None:
        symbols = ("A.SZ", "B.SZ")
        days = [_day(offset) for offset in range(60)]
        bars = {symbol: _bars(days) for symbol in symbols}
        income = {
            "A.SZ": _announcements(
                [
                    (_day(10), -5.0),
                    (_day(20), 12.0),
                    (_day(30), 13.0),
                    (_day(40), 14.0),
                    (_day(50), 20.0),
                ]
            ),
            "B.SZ": _announcements(
                [
                    (_day(10), 10.0),
                    (_day(20), 12.0),
                    (_day(30), 13.0),
                    (_day(40), 14.0),
                    (_day(50), 20.0),
                ]
            ),
        }
        inventories = {
            "A.SZ": _announcements([(_day(0), 100.0)]),
            "B.SZ": _announcements([(_day(0), 100.0)]),
        }
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {"n_income": income},
                "balance_sheets": {"inventories": inventories},
            },
            decision_dates=(_day(51),),
        )
        cross = _compute("np_to_inventory_yoy", inp)[_day(51)]
        assert cross["A.SZ"] is None  # 基期比值 -0.05 <= 0
        assert cross["B.SZ"] == pytest.approx(1.0)

    def test_yoy_denominator_nonpositive_is_none(self) -> None:
        days = [_day(offset) for offset in range(60)]
        bars = {"A.SZ": _bars(days)}
        income = {
            "A.SZ": _announcements(
                [
                    (_day(10), 10.0),
                    (_day(20), 12.0),
                    (_day(30), 13.0),
                    (_day(40), 14.0),
                    (_day(50), 20.0),
                ]
            )
        }
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {"n_income": income},
                "balance_sheets": {
                    "inventories": {"A.SZ": _announcements([(_day(0), 0.0)])}
                },
            },
            decision_dates=(_day(51),),
        )
        assert _compute("np_to_inventory_yoy", inp)[_day(51)]["A.SZ"] is None

    def test_yoy_fewer_than_five_rows_is_none(self) -> None:
        """公告行不足 5 行(t-4 不可用)→ 缺测;5 行即可出值。"""
        days = [_day(offset) for offset in range(60)]
        bars = {"A.SZ": _bars(days), "B.SZ": _bars(days)}
        four_rows = {
            "A.SZ": _announcements(
                [
                    (_day(10), 10.0),
                    (_day(20), 12.0),
                    (_day(30), 13.0),
                    (_day(40), 14.0),
                ]
            )
        }
        five_rows = {
            "B.SZ": _announcements(
                [
                    (_day(10), 10.0),
                    (_day(20), 12.0),
                    (_day(30), 13.0),
                    (_day(40), 14.0),
                    (_day(50), 20.0),
                ]
            )
        }
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {"n_income": {**four_rows, **five_rows}},
                "balance_sheets": {
                    "inventories": {
                        "A.SZ": _announcements([(_day(0), 100.0)]),
                        "B.SZ": _announcements([(_day(0), 100.0)]),
                    }
                },
            },
            decision_dates=(_day(51),),
        )
        cross = _compute("np_to_inventory_yoy", inp)[_day(51)]
        assert cross["A.SZ"] is None
        assert cross["B.SZ"] == pytest.approx(1.0)

    def test_yoy_missing_required_component_is_none(self) -> None:
        """required=True 的科目缺测 → 整体缺测(不猜 0)。"""
        days = [_day(offset) for offset in range(60)]
        bars = {"A.SZ": _bars(days), "B.SZ": _bars(days)}
        income = {
            symbol: _announcements(
                [
                    (_day(10), 10.0),
                    (_day(20), 12.0),
                    (_day(30), 13.0),
                    (_day(40), 14.0),
                    (_day(50), 20.0),
                ]
            )
            for symbol in ("A.SZ", "B.SZ")
        }
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {"n_income": income},
                "balance_sheets": {
                    "fix_assets": {"A.SZ": _announcements([(_day(0), 50.0)])}
                },
            },
            decision_dates=(_day(51),),
        )
        cross = _compute("np_to_fixed_assets_yoy", inp)[_day(51)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] is None  # 无固定资产行 → required 缺测

    def test_yoy_works_without_bar_calendar(self) -> None:
        """纯公告序同比族不依赖 bar 回看:无 bars 也照常出值。"""
        rows = [(_day(10), 10.0), (_day(20), 12.0), (_day(30), 13.0)]
        rows += [(_day(40), 14.0), (_day(50), 20.0)]
        inp = _Input(
            bars={},
            datasets={
                "income_statements": {"n_income": {"A.SZ": _announcements(rows)}},
                "balance_sheets": {
                    "fix_assets": {"A.SZ": _announcements([(_day(0), 100.0)])}
                },
            },
            decision_dates=(_day(51),),
            tradable=("A.SZ",),
        )
        cross = _compute("np_to_fixed_assets_yoy", inp)[_day(51)]
        assert cross["A.SZ"] == pytest.approx(1.0)

    def test_yoy_values_match_independent_reference(self) -> None:
        """3 标的 x 全决策日:5 个同比因子逐值 vs 独立参考(含 rank)。"""
        symbols = ("A.SZ", "B.SZ", "C.SZ")
        days = [_day(offset) for offset in range(700)]
        bars = {symbol: _bars(days) for symbol in symbols}
        datasets = _datasets_for(
            symbols,
            {
                "income_statements": (
                    "n_income",
                    "sell_exp",
                    "admin_exp",
                    "fin_exp",
                ),
                "balance_sheets": (
                    "inventories",
                    "fix_assets",
                    "total_hldr_eqy_exc_min_int",
                ),
                "cashflow_statements": ("c_paid_to_for_empl",),
            },
            # A 的 admin_exp 第 3 行缺测(required → 该行起比值缺测)
            nan_rows=("income_statements", "admin_exp", frozenset({3})),
        )
        decision_dates = tuple(_day(offset) for offset in range(300, 700, 47))
        inp = _Input(bars=bars, datasets=datasets, decision_dates=decision_dates)
        for name, driver_key, numerator, denominator in YOY_RATIO_REF_SPECS:
            driver_kind, driver_field = driver_key
            drivers = datasets[driver_kind][driver_field]
            frame = _compute(name, inp)
            assert any(
                value is not None
                for cross in frame.values()
                for value in cross.values()
            ), name
            for day in decision_dates:
                values: dict[str, float] = {}
                for symbol in symbols:
                    driver = drivers[symbol]
                    rows = _ref_ratio_rows(
                        datasets, symbol, driver, numerator, denominator
                    )
                    values[symbol] = _ref_yoy_sampled(
                        rows, _visible_row(driver, day)
                    )
                assert frame[day] == pytest.approx(
                    dict(_ref_rank(values)), nan_ok=True
                ), (name, day)

    def test_yoy_prefix_invariance_truncation(self) -> None:
        """截断挂载(available_at <= cut)前缀逐值不变(只消费更早行)。"""
        symbols = ("A.SZ", "B.SZ")
        days = [_day(offset) for offset in range(700)]
        bars = {symbol: _bars(days) for symbol in symbols}
        datasets = _datasets_for(
            symbols,
            {
                "income_statements": ("n_income", "sell_exp", "admin_exp", "fin_exp"),
                "balance_sheets": ("inventories",),
            },
        )
        cut = _day(420)
        decision_dates = tuple(_day(offset) for offset in range(100, 421, 53))
        specs = ("np_to_inventory_yoy", "np_to_total_expenses_yoy")
        for name in specs:
            full_frame = _compute(
                name,
                _Input(bars=bars, datasets=datasets, decision_dates=decision_dates),
            )
            cut_frame = _compute(
                name,
                _Input(
                    bars=bars,
                    datasets=_truncate_datasets(datasets, cut),
                    decision_dates=decision_dates,
                ),
            )
            assert cut_frame == full_frame, name
            assert any(
                value is not None
                for cross in full_frame.values()
                for value in cross.values()
            ), name


# --------------------------------------------------------------------- #
# 变化族(行轴比值 + 252 根 bar PIT 回看)
# --------------------------------------------------------------------- #


class TestRowRatioDeltaFamily:
    def test_delta_opm_hand_anchor_row_anchored_lookback(self) -> None:
        """Δ 基值 = 「公告日往前 252 根 bar」时点可见的最近一次公告行。"""
        symbols = ("A.SZ", "B.SZ")
        days = [_day(offset) for offset in range(700)]
        bars = {symbol: _bars(days) for symbol in symbols}
        revenue = {
            symbol: _announcements(
                [(_day(20), 100.0), (_day(300), 100.0), (_day(520), 100.0)]
            )
            for symbol in symbols
        }
        profit = {
            "A.SZ": _announcements(
                [(_day(20), 20.0), (_day(300), 30.0), (_day(520), 40.0)]
            ),
            "B.SZ": _announcements(
                [(_day(20), 20.0), (_day(300), 25.0), (_day(520), 30.0)]
            ),
        }
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {"operate_profit": profit, "revenue": revenue}
            },
            decision_dates=(_day(521),),
        )
        # 回看日 = bar 520 - 252 = 268 → 两标的基期 OPM 均 0.20
        # A: 0.40 - 0.20 = 0.20;B: 0.30 - 0.20 = 0.10
        cross = _compute("delta_opm", inp)[_day(521)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_delta_cash_ratio_hand_anchor(self) -> None:
        symbols = ("A.SZ", "B.SZ")
        days = [_day(offset) for offset in range(700)]
        bars = {symbol: _bars(days) for symbol in symbols}
        money = {
            "A.SZ": _announcements(
                [(_day(20), 50.0), (_day(300), 50.0), (_day(520), 90.0)]
            ),
            "B.SZ": _announcements(
                [(_day(20), 50.0), (_day(300), 50.0), (_day(520), 60.0)]
            ),
        }
        cur_liab = {
            symbol: _announcements(
                [(_day(20), 100.0), (_day(300), 100.0), (_day(520), 100.0)]
            )
            for symbol in symbols
        }
        inp = _Input(
            bars=bars,
            datasets={
                "balance_sheets": {"money_cap": money, "total_cur_liab": cur_liab}
            },
            decision_dates=(_day(521),),
        )
        # 基期 CR = 0.5;A: 0.9 - 0.5 = 0.4;B: 0.6 - 0.5 = 0.1
        cross = _compute("delta_cash_ratio", inp)[_day(521)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_delta_missing_bar_calendar_is_none(self) -> None:
        rows = [(_day(offset), 10.0 + offset) for offset in (0, 100, 200, 300, 400)]
        inp = _Input(
            bars={},
            datasets={
                "income_statements": {
                    "operate_profit": {"A.SZ": _announcements(rows)},
                    "revenue": {"A.SZ": _announcements([(_day(0), 100.0)])},
                }
            },
            decision_dates=(_day(401),),
            tradable=("A.SZ",),
        )
        assert _compute("delta_opm", inp)[_day(401)]["A.SZ"] is None

    def test_delta_bar_coverage_short_is_none(self) -> None:
        days = [_day(offset) for offset in range(100)]
        bars = {"A.SZ": _bars(days)}
        profit = {
            "A.SZ": _announcements(
                [(_day(0), 10.0), (_day(30), 20.0), (_day(90), 30.0)]
            )
        }
        revenue = {"A.SZ": _announcements([(_day(0), 100.0)])}
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {"operate_profit": profit, "revenue": revenue}
            },
            decision_dates=(_day(91),),
        )
        assert _compute("delta_opm", inp)[_day(91)]["A.SZ"] is None

    def test_delta_values_match_independent_reference(self) -> None:
        """3 标的 x 全决策日:Δ 族逐值 vs 独立参考(含 rank)。"""
        symbols = ("A.SZ", "B.SZ", "C.SZ")
        days = [_day(offset) for offset in range(700)]
        bars = {symbol: _bars(days) for symbol in symbols}
        datasets = _datasets_for(
            symbols,
            {
                "income_statements": ("operate_profit", "revenue"),
                "balance_sheets": ("money_cap", "trading_fl", "total_cur_liab"),
            },
            nan_rows=("balance_sheets", "trading_fl", frozenset({2})),
        )
        decision_dates = tuple(_day(offset) for offset in range(300, 700, 41))
        inp = _Input(bars=bars, datasets=datasets, decision_dates=decision_dates)
        for name, driver_key, numerator, denominator in DELTA_REF_SPECS:
            driver_kind, driver_field = driver_key
            drivers = datasets[driver_kind][driver_field]
            frame = _compute(name, inp)
            assert any(
                value is not None
                for cross in frame.values()
                for value in cross.values()
            ), name
            for day in decision_dates:
                values: dict[str, float] = {}
                for symbol in symbols:
                    driver = drivers[symbol]
                    rows = _ref_ratio_rows(
                        datasets, symbol, driver, numerator, denominator
                    )
                    position = _visible_row(driver, day)
                    values[symbol] = _ref_delta_sampled(
                        driver, bars[symbol], rows, position
                    )
                assert frame[day] == pytest.approx(
                    dict(_ref_rank(values)), nan_ok=True
                ), (name, day)

    def test_delta_prefix_invariance_truncation(self) -> None:
        """截断挂载(available_at <= cut)前缀逐值不变(只消费更早行)。"""
        symbols = ("A.SZ", "B.SZ")
        days = [_day(offset) for offset in range(700)]
        bars = {symbol: _bars(days) for symbol in symbols}
        datasets = _datasets_for(
            symbols,
            {
                "income_statements": ("operate_profit", "revenue"),
                "balance_sheets": ("money_cap", "total_cur_liab"),
            },
        )
        cut = _day(420)
        decision_dates = tuple(_day(offset) for offset in range(300, 421, 29))
        for name in DELTA_NAMES:
            full_frame = _compute(
                name,
                _Input(bars=bars, datasets=datasets, decision_dates=decision_dates),
            )
            cut_frame = _compute(
                name,
                _Input(
                    bars=bars,
                    datasets=_truncate_datasets(datasets, cut),
                    decision_dates=decision_dates,
                ),
            )
            assert cut_frame == full_frame, name
            assert any(
                value is not None
                for cross in full_frame.values()
                for value in cross.values()
            ), name


# --------------------------------------------------------------------- #
# 水平 / 比值族(决策日两端 asof 后比值 → 截面 rank)
# --------------------------------------------------------------------- #


class TestLevelRatioFamily:
    def test_cash_ratio_hand_anchor(self) -> None:
        days = [_day(offset) for offset in range(10)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        inp = _Input(
            bars=bars,
            datasets={
                "balance_sheets": {
                    "money_cap": {
                        "A.SZ": _announcements([(_day(0), 50.0)]),
                        "B.SZ": _announcements([(_day(0), 20.0)]),
                    },
                    "trading_fl": {
                        "A.SZ": _announcements([(_day(0), 30.0)]),
                        "B.SZ": _announcements([(_day(0), 10.0)]),
                    },
                    "total_cur_liab": {
                        "A.SZ": _announcements([(_day(0), 100.0)]),
                        "B.SZ": _announcements([(_day(0), 200.0)]),
                    },
                }
            },
            decision_dates=(_day(1),),
        )
        # A: (50 + 30)/100 = 0.8;B: (20 + 10)/200 = 0.15
        cross = _compute("cash_ratio", inp)[_day(1)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_cash_ratio_optional_component_counts_zero(self) -> None:
        """交易性金融资产缺测按 0(required=False),货币资金缺测整体缺测。"""
        days = [_day(offset) for offset in range(10)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        inp = _Input(
            bars=bars,
            datasets={
                "balance_sheets": {
                    "money_cap": {
                        "A.SZ": _announcements([(_day(0), 50.0)]),
                        "B.SZ": _announcements([(_day(0), 20.0)]),
                    },
                    "trading_fl": {
                        "B.SZ": _announcements([(_day(0), 10.0)])
                    },
                    "total_cur_liab": {
                        "A.SZ": _announcements([(_day(0), 100.0)]),
                        "B.SZ": _announcements([(_day(0), 200.0)]),
                    },
                }
            },
            decision_dates=(_day(1),),
        )
        # A: 50/100 = 0.5(交易性金融资产缺测按 0);B: 30/200 = 0.15
        cross = _compute("cash_ratio", inp)[_day(1)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_market_value_leverage_hand_anchor(self) -> None:
        days = [_day(offset) for offset in range(10)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        inp = _Input(
            bars=bars,
            datasets={
                "balance_sheets": {
                    "total_ncl": {
                        "A.SZ": _announcements([(_day(0), 40.0)]),
                        "B.SZ": _announcements([(_day(0), 80.0)]),
                    }
                }
            },
            daily={
                "total_market_cap": {
                    "A.SZ": _bars(days, close=100.0),
                    "B.SZ": _bars(days, close=100.0),
                }
            },
            decision_dates=(_day(1),),
        )
        # A: (100 - 40)/100 = 0.6;B: (100 - 80)/100 = 0.2
        cross = _compute("market_value_leverage", inp)[_day(1)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_market_value_leverage_requires_noncurrent_liab(self) -> None:
        days = [_day(offset) for offset in range(10)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        inp = _Input(
            bars=bars,
            datasets={
                "balance_sheets": {
                    "total_ncl": {"B.SZ": _announcements([(_day(0), 80.0)])}
                }
            },
            daily={
                "total_market_cap": {
                    "A.SZ": _bars(days, close=100.0),
                    "B.SZ": _bars(days, close=100.0),
                }
            },
            decision_dates=(_day(1),),
        )
        cross = _compute("market_value_leverage", inp)[_day(1)]
        assert cross["A.SZ"] is None  # required 科目缺测
        assert cross["B.SZ"] == pytest.approx(1.0)

    def test_earnings_cut_to_market_hand_anchor(self) -> None:
        days = [_day(offset) for offset in range(10)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {
                    "n_income_attr_p": {
                        "A.SZ": _announcements([(_day(0), 10.0)]),
                        "B.SZ": _announcements([(_day(0), 5.0)]),
                    }
                }
            },
            daily={
                "total_market_cap": {
                    "A.SZ": _bars(days, close=100.0),
                    "B.SZ": _bars(days, close=100.0),
                }
            },
            decision_dates=(_day(1),),
        )
        # A: 10/100 = 0.1;B: 5/100 = 0.05
        cross = _compute("earnings_cut_to_market", inp)[_day(1)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_denominator_nonpositive_is_none(self) -> None:
        days = [_day(offset) for offset in range(10)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        zero_cap = {symbol: _bars(days, close=0.0) for symbol in ("A.SZ", "B.SZ")}
        inp = _Input(
            bars=bars,
            datasets={
                "balance_sheets": {
                    "money_cap": {
                        "A.SZ": _announcements([(_day(0), 50.0)]),
                        "B.SZ": _announcements([(_day(0), 50.0)]),
                    },
                    "total_cur_liab": {
                        "A.SZ": _announcements([(_day(0), 100.0)]),
                        "B.SZ": _announcements([(_day(0), 0.0)]),
                    },
                },
                "income_statements": {
                    "n_income_attr_p": {
                        "A.SZ": _announcements([(_day(0), 10.0)]),
                        "B.SZ": _announcements([(_day(0), 10.0)]),
                    }
                },
            },
            daily={"total_market_cap": zero_cap},
            decision_dates=(_day(1),),
        )
        assert _compute("cash_ratio", inp)[_day(1)]["A.SZ"] == pytest.approx(1.0)
        assert _compute("cash_ratio", inp)[_day(1)]["B.SZ"] is None
        assert _compute("earnings_cut_to_market", inp)[_day(1)]["A.SZ"] is None

    def test_missing_visible_announcement_is_none(self) -> None:
        days = [_day(offset) for offset in range(30)]
        bars = {"A.SZ": _bars(days)}
        inp = _Input(
            bars=bars,
            datasets={
                "balance_sheets": {
                    "money_cap": {"A.SZ": _announcements([(_day(20), 50.0)])},
                    "total_cur_liab": {
                        "A.SZ": _announcements([(_day(20), 100.0)])
                    },
                }
            },
            decision_dates=(_day(10),),
        )
        assert _compute("cash_ratio", inp)[_day(10)]["A.SZ"] is None


# --------------------------------------------------------------------- #
# 其余 3(反转 / PEG / ETP5)
# --------------------------------------------------------------------- #


class TestReversalPegEtp5:
    def test_small_cap_reversal_hand_anchor(self) -> None:
        """单调上涨 → Reversal 最负 → rank 最低(小盘池由 universe 决定)。"""
        days = [_day(offset) for offset in range(30)]
        bars = {
            "A.SZ": _bars_values(days, np.array([10.0 + i for i in range(30)])),
            "B.SZ": _bars_values(days, np.array([40.0 - i for i in range(30)])),
            "C.SZ": _bars_values(days, np.full(30, 10.0)),
        }
        inp = _Input(bars=bars, decision_dates=(days[-1],))
        cross = _compute("small_cap_reversal_21d", inp)[days[-1]]
        assert cross["A.SZ"] == pytest.approx(1 / 3)
        assert cross["C.SZ"] == pytest.approx(2 / 3)
        assert cross["B.SZ"] == pytest.approx(1.0)

    def test_small_cap_reversal_insufficient_bars_is_none(self) -> None:
        days = [_day(offset) for offset in range(15)]
        bars = {
            "A.SZ": _bars_values(days, np.array([10.0 + i for i in range(15)])),
            "B.SZ": _bars_values(days, np.array([40.0 - i for i in range(15)])),
        }
        inp = _Input(bars=bars, decision_dates=(days[-1],))
        cross = _compute("small_cap_reversal_21d", inp)[days[-1]]
        assert cross["A.SZ"] is None
        assert cross["B.SZ"] is None

    def test_peg_252d_hand_anchor(self) -> None:
        """PEG = PE / (EPS 增速 x 100):手算排序。"""
        days = [_day(offset) for offset in range(700)]
        bars = {
            "A.SZ": _bars(days, close=10.0),
            "B.SZ": _bars(days, close=20.0),
        }
        eps = {
            "A.SZ": _announcements(
                [(_day(20), 1.0), (_day(300), 1.0), (_day(520), 1.5)]
            ),
            "B.SZ": _announcements(
                [(_day(20), 1.0), (_day(300), 1.0), (_day(520), 2.0)]
            ),
        }
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"eps": eps}},
            decision_dates=(_day(521),),
        )
        # A: 增速 0.5 → PE = 10/1.5 → PEG = 6.667/50 = 0.1333
        # B: 增速 1.0 → PE = 20/2.0 → PEG = 10/100 = 0.1
        cross = _compute("peg_252d", inp)[_day(521)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_peg_252d_nonpositive_growth_or_eps_is_none(self) -> None:
        days = [_day(offset) for offset in range(700)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "G.SZ", "N.SZ", "P.SZ")}
        eps = {
            "A.SZ": _announcements(
                [(_day(20), 1.0), (_day(300), 1.0), (_day(520), 1.5)]
            ),
            "G.SZ": _announcements(  # 零增长
                [(_day(20), 1.0), (_day(300), 1.0), (_day(520), 1.0)]
            ),
            "N.SZ": _announcements(  # 负增长
                [(_day(20), 1.0), (_day(300), 1.0), (_day(520), 0.5)]
            ),
            "P.SZ": _announcements(  # EPS <= 0
                [(_day(20), 1.0), (_day(300), 1.0), (_day(520), -1.0)]
            ),
        }
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"eps": eps}},
            decision_dates=(_day(521),),
        )
        cross = _compute("peg_252d", inp)[_day(521)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["G.SZ"] is None
        assert cross["N.SZ"] is None
        assert cross["P.SZ"] is None

    def test_peg_252d_no_visible_eps_is_none(self) -> None:
        days = [_day(offset) for offset in range(700)]
        bars = {"A.SZ": _bars(days)}
        eps = {"A.SZ": _announcements([(_day(600), 1.0)])}
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"eps": eps}},
            decision_dates=(_day(521),),
        )
        assert _compute("peg_252d", inp)[_day(521)]["A.SZ"] is None

    def test_peg_252d_excludes_non_tradable_symbols(self) -> None:
        """截面域 = 可交易域(#380):基准标的只进挂载不入 rank 分母。"""
        days = [_day(offset) for offset in range(700)]
        bars = {
            "A.SZ": _bars(days, close=10.0),
            "000300.SH": _bars(days, close=10.0),
        }
        eps = {
            "A.SZ": _announcements(
                [(_day(20), 1.0), (_day(300), 1.0), (_day(520), 1.5)]
            ),
            "000300.SH": _announcements(
                [(_day(20), 1.0), (_day(300), 1.0), (_day(520), 1.5)]
            ),
        }
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"eps": eps}},
            decision_dates=(_day(521),),
            tradable=("A.SZ",),
        )
        cross = _compute("peg_252d", inp)[_day(521)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross.get("000300.SH") is None

    def test_etp5_hand_anchor(self) -> None:
        """净利润(公告步进)与市值恒定 → ETP5 = 100 / 1000 = 0.1。"""
        days = [_day(offset) for offset in range(1301)]
        bars = {"A.SZ": _bars(days)}
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {
                    "n_income": {"A.SZ": _announcements([(_day(0), 100.0)])}
                }
            },
            daily={"total_market_cap": {"A.SZ": _bars(days, close=1000.0)}},
            decision_dates=(_day(1300),),
        )
        value = _compute("etp5", inp)[_day(1300)]["A.SZ"]
        assert value == pytest.approx(0.1)

    def test_etp5_window_not_covered_is_none(self) -> None:
        """窗口内首根 bar 时点公告尚不可见(步进 NaN)→ 均值缺测。"""
        days = [_day(offset) for offset in range(1301)]
        bars = {"A.SZ": _bars(days)}
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {
                    "n_income": {"A.SZ": _announcements([(_day(0), 100.0)])}
                }
            },
            daily={"total_market_cap": {"A.SZ": _bars(days, close=1000.0)}},
            decision_dates=(_day(1259),),
        )
        assert _compute("etp5", inp)[_day(1259)]["A.SZ"] is None

    def test_etp5_missing_inputs_and_nonpositive_cap_is_none(self) -> None:
        days = [_day(offset) for offset in range(1301)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ", "C.SZ")}
        income = {
            "A.SZ": _announcements([(_day(0), 100.0)]),
            "B.SZ": _announcements([(_day(0), 100.0)]),
        }
        cap = {
            "A.SZ": _bars(days, close=1000.0),
            "B.SZ": _bars(days, close=0.0),
        }
        inp = _Input(
            bars=bars,
            datasets={"income_statements": {"n_income": income}},
            daily={"total_market_cap": cap},
            decision_dates=(_day(1300),),
        )
        cross = _compute("etp5", inp)[_day(1300)]
        assert cross["A.SZ"] == pytest.approx(0.1)
        assert cross["B.SZ"] is None  # 分母 <= 0
        assert cross.get("C.SZ") is None  # 无市值序列 → 结构性缺测


# --------------------------------------------------------------------- #
# 扩展 helper:公告序滞后参数(lag 默认 1 的存量行为 / lag=4 = 去年同期)
# --------------------------------------------------------------------- #


class TestStepRatioLagHelper:
    def test_default_lag_one_unchanged(self) -> None:
        """默认 lag=1(存量 asset_growth_qoq 条目):t / t-1 - 1。"""
        days = [_day(offset) for offset in range(10)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ", "C.SZ")}
        total_assets = {
            "A.SZ": _announcements(
                [(_day(0), 100.0), (_day(3), 150.0), (_day(6), 225.0)]
            ),
            "B.SZ": _announcements(
                [(_day(0), 100.0), (_day(3), 200.0), (_day(6), 250.0)]
            ),
            "C.SZ": _announcements(
                [(_day(0), 100.0), (_day(3), 50.0), (_day(6), 100.0)]
            ),
        }
        inp = _Input(
            bars=bars,
            datasets={"balance_sheets": {"total_assets": total_assets}},
            decision_dates=(_day(7),),
        )
        # C: 100/50 - 1 = 1.0;A: 225/150 - 1 = 0.5;B: 250/200 - 1 = 0.25
        cross = _compute("asset_growth_qoq", inp)[_day(7)]
        assert cross["C.SZ"] == pytest.approx(1.0)
        assert cross["A.SZ"] == pytest.approx(2 / 3)
        assert cross["B.SZ"] == pytest.approx(1 / 3)

    def test_lag_parameter_row_lag(self) -> None:
        """直接调用 helper:lag=1 与 lag=4 在同一序列上给出不同行滞后。"""
        days = [_day(offset) for offset in range(10)]
        bars = {"A.SZ": _bars(days), "B.SZ": _bars(days)}
        series = {
            "A.SZ": _announcements(
                [(_day(0), 100.0), (_day(3), 120.0), (_day(4), 130.0),
                 (_day(5), 140.0), (_day(6), 150.0)]
            ),
            "B.SZ": _announcements(
                [(_day(0), 100.0), (_day(3), 90.0), (_day(4), 80.0),
                 (_day(5), 70.0), (_day(6), 105.0)]
            ),
        }
        inp = _Input(
            bars=bars,
            datasets={"income_statements": {"income_tax": series}},
            decision_dates=(_day(7),),
        )
        lag_one = _financial_step_ratio_rank(
            "income_statements", "income_tax"
        )(inp)[_day(7)]
        lag_four = _financial_step_ratio_rank(
            "income_statements", "income_tax", lag=4
        )(inp)[_day(7)]
        # lag=1:B(105/70 = +0.5) > A(150/140 ≈ +0.07)
        assert lag_one["B.SZ"] == pytest.approx(1.0)
        assert lag_one["A.SZ"] == pytest.approx(0.5)
        # lag=4:A(150/100 = +0.5) > B(105/100 = +0.05)
        assert lag_four["A.SZ"] == pytest.approx(1.0)
        assert lag_four["B.SZ"] == pytest.approx(0.5)


# --------------------------------------------------------------------- #
# 通用不变量
# --------------------------------------------------------------------- #


class TestBatchWideInvariants:
    def test_rank_outputs_bounded_zero_one(self) -> None:
        days = [_day(offset) for offset in range(30)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {
                    "income_tax": {
                        "A.SZ": _announcements(
                            [(_day(offset), 10.0 + offset) for offset in range(5)]
                        ),
                        "B.SZ": _announcements(
                            [(_day(offset), 20.0 + offset) for offset in range(5)]
                        ),
                    }
                }
            },
            decision_dates=(_day(6),),
        )
        for value in _compute("income_tax_yoy", inp)[_day(6)].values():
            assert value is None or 0.0 < value <= 1.0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
