"""因子批次 6(#429):P1 财务 29 个的目录不变量与逐值单测。

* 目录不变量(批次 scope,无全局 exact-total 断言):29 个全部注册、
  家族计数(quality 20 / growth 8 / value 1)、全部 ``cross_section=True``
  且 ``signal_eligible=True``、方向(delta_de LOWER 其余 HIGHER)、
  窗口与 ``min_history_bars``(252/315/63 三档精确)、data_dependencies 精确;
* **公告序列 PIT 历史回看机制**(本批次核心)逐值对照:独立参考实现直接
  按契约推「回看日可见的最近一次公告值」,与实现逐值一致 —— 覆盖
  变化(Δ)/同比/环比/加速度/水平五族;不变量:回看只消费更早行、
  截断前缀不变、基期 <= 0 不虚构增速、bar 覆盖不足缺测;
* 三表列族(income_statements / balance_sheets / cashflow_statements)
  的比值口径与分母纪律(分母 <= 0 → None)。

纯离线研究域,不连 broker 不下单。
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
    get_predefined_factor,
    predefined_factor_commit,
    predefined_factor_names,
)
from finboard_data.factor_lab import FactorPreference

#: 批次 6 P1 财务 29 个裸名(与 tushare factor_list 2026-09-11 快照名一致)
DELTA_SPECS = (
    ("delta_roe", "return_on_equity", FactorPreference.HIGHER),
    ("delta_roa", "return_on_assets", FactorPreference.HIGHER),
    ("delta_npm", "net_profit_margin", FactorPreference.HIGHER),
    ("delta_gpm", "gross_profit_margin", FactorPreference.HIGHER),
    ("delta_de", "debt_to_equity", FactorPreference.LOWER),
    ("delta_current_ratio", "current_ratio", FactorPreference.HIGHER),
    ("delta_quick_ratio", "quick_ratio", FactorPreference.HIGHER),
    ("delta_asset_turnover", "total_assets_turnover", FactorPreference.HIGHER),
    ("delta_inventory_turnover", "inventory_turnover", FactorPreference.HIGHER),
)
YOY_SPECS = (
    ("yoy_roa", "financial_indicators", "return_on_assets"),
    ("yoy_roe", "financial_indicators", "return_on_equity"),
    ("yoy_net_asset", "balance_sheets", "total_hldr_eqy_exc_min_int"),
    ("yoy_total_asset", "balance_sheets", "total_assets"),
)
ACCEL_NAMES = ("pa", "eaa", "eap")
QOQ_NAMES = ("asset_growth_qoq", "gpm_qoq", "npm_q_qoq", "npm_ttm_qoq")
LEVEL_NAMES = ("eps_ttm", "eps_q", "eps_y")
RATIO_SPECS = (
    ("opm_y", "income_statements.operate_profit", "income_statements.revenue"),
    ("opm_ttm", "income_statements.operate_profit", "income_statements.revenue"),
    ("opt_tpro", "income_statements.operate_profit", "income_statements.total_profit"),
    (
        "equity_turnover",
        "income_statements.revenue",
        "balance_sheets.total_hldr_eqy_inc_min_int",
    ),
    ("cfcr", "cashflow_statements.n_cashflow_act", "income_statements.int_exp"),
)
BATCH6_FINANCIAL_NAMES = frozenset(
    {name for name, _, _ in DELTA_SPECS}
    | {name for name, _, _ in YOY_SPECS}
    | set(ACCEL_NAMES)
    | set(QOQ_NAMES)
    | set(LEVEL_NAMES)
    | {name for name, _, _ in RATIO_SPECS}
    | {"ncf_to_market"}
)

#: 期望窗口 / min_history_bars(长回看 = 252 / 回看+滞后 = 315 / 季度 ≈ 63)
EXPECTED_WINDOWS: dict[str, int | None] = {
    **{name: 252 for name, _, _ in DELTA_SPECS},
    **{name: 252 for name, _, _ in YOY_SPECS},
    "pa": 315,
    "eaa": 315,
    "eap": 315,
    "gpm_qoq": 63,
    "npm_ttm_qoq": 63,
    "asset_growth_qoq": None,
    "npm_q_qoq": None,
    **dict.fromkeys(LEVEL_NAMES),
    **{name: None for name, _, _ in RATIO_SPECS},
    "ncf_to_market": None,
}

_BASE_DAY = date(2020, 1, 1)
_YEAR_BARS = 252
_QUARTER_BARS = 63


def _day(offset: int) -> date:
    return _BASE_DAY + timedelta(days=offset)


def _bars(days: list[date], close: float = 10.0) -> SymbolSeries:
    """bar 日历序列(dates 为交易日;收盘常数便于价格标准化断言)。"""
    return SymbolSeries(
        dates=tuple(days),
        values=np.full(len(days), close, dtype=np.float64),
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
    """测试输入面:bar 日历 + 公告类数据集(三表与 financial_indicators)。"""

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
            factor_name="batch6_financial",
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


def _visible_value(ann: SymbolSeries, day: date) -> float:
    """ann 在 day 日终可见的最后一行值(无可见行 → NaN)。"""
    position = _visible_row(ann, day)
    return math.nan if position < 0 else float(ann.values[position])


def _bar_position(cal: SymbolSeries, day: date) -> int:
    """bar 日历上 date <= day 的最后一根下标(-1 = 无)。"""
    position = -1
    for index, item in enumerate(cal.dates):
        if item <= day:
            position = index
    return position


def _ref_lookback_base(ann: SymbolSeries, cal: SymbolSeries, day: date, lookback: int) -> float:
    """回看基值:公告日往前 lookback 根 bar 时点 PIT 可见的最近一次公告值。"""
    position = _bar_position(cal, day)
    if position < 0 or position - lookback < 0:
        return math.nan
    return _visible_value(ann, cal.dates[position - lookback])


def _ref_row_change(
    ann: SymbolSeries, cal: SymbolSeries, position: int, lookback: int, *, ratio: bool
) -> float:
    """行级变化值:基值 = 「该行公告日往前 lookback 根 bar」时点可见的公告值。

    与 registry 的公告行口径一致(#401 ``fin_accel`` 公告序差分同一边界:
    对年报型标的不会退化为「当前值减自身」)。
    """
    current = float(ann.values[position])
    base = _ref_lookback_base(ann, cal, ann.dates[position], lookback)
    if not (math.isfinite(current) and math.isfinite(base)):
        return math.nan
    if ratio:
        return current / base - 1.0 if base > 0.0 else math.nan
    return current - base


def _ref_change(
    ann: SymbolSeries, cal: SymbolSeries, day: date, lookback: int, *, ratio: bool
) -> float:
    """决策日采样值:最新可见公告行的行级变化值。"""
    position = _visible_row(ann, day)
    if position < 0:
        return math.nan
    return _ref_row_change(ann, cal, position, lookback, ratio=ratio)


def _ref_accel(
    ann: SymbolSeries,
    cal: SymbolSeries,
    day: date,
    lookback: int,
    lag: int,
    *,
    ratio: bool,
) -> float:
    """行级变化值的 lag 根 bar 前再差分(决策日采样)。"""
    position = _visible_row(ann, day)
    if position < 0:
        return math.nan
    current = _ref_row_change(ann, cal, position, lookback, ratio=ratio)
    bar_position = _bar_position(cal, ann.dates[position])
    if bar_position - lag < 0:
        return math.nan
    past_position = _visible_row(ann, cal.dates[bar_position - lag])
    if past_position < 0:
        return math.nan
    past = _ref_row_change(ann, cal, past_position, lookback, ratio=ratio)
    if not (math.isfinite(current) and math.isfinite(past)):
        return math.nan
    return current - past


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


class TestBatch6FinancialCatalog:
    def test_exactly_29_registered(self) -> None:
        assert len(BATCH6_FINANCIAL_NAMES) == 29
        assert BATCH6_FINANCIAL_NAMES.issubset(set(predefined_factor_names()))
        for name in BATCH6_FINANCIAL_NAMES:
            assert get_predefined_factor(name).name == name

    def test_family_counts(self) -> None:
        families = {
            PREDEFINED_FACTORS[name].family for name in BATCH6_FINANCIAL_NAMES
        }
        assert families == {"quality", "growth", "value"}
        quality = {
            name
            for name in BATCH6_FINANCIAL_NAMES
            if PREDEFINED_FACTORS[name].family == "quality"
        }
        assert len(quality) == 20
        growth = {
            name
            for name in BATCH6_FINANCIAL_NAMES
            if PREDEFINED_FACTORS[name].family == "growth"
        }
        assert len(growth) == 8

    def test_all_cross_section_rank_and_signal_eligible(self) -> None:
        for name in BATCH6_FINANCIAL_NAMES:
            item = PREDEFINED_FACTORS[name]
            assert item.cross_section is True, name
            assert item.signal_eligible is True, name

    def test_direction_map(self) -> None:
        expected = {
            name: direction for name, _, direction in DELTA_SPECS
        } | {
            name: FactorPreference.HIGHER
            for name in BATCH6_FINANCIAL_NAMES
            if name not in {spec[0] for spec in DELTA_SPECS}
        }
        assert {
            name: PREDEFINED_FACTORS[name].direction
            for name in BATCH6_FINANCIAL_NAMES
        } == expected

    def test_window_declarations_exact(self) -> None:
        assert {
            name: PREDEFINED_FACTORS[name].window
            for name in BATCH6_FINANCIAL_NAMES
        } == EXPECTED_WINDOWS

    def test_min_history_bars_exact(self) -> None:
        expected = {
            name: window
            for name, window in EXPECTED_WINDOWS.items()
            if window is not None
        }
        assert {
            name: PREDEFINED_FACTORS[name].min_history_bars
            for name in BATCH6_FINANCIAL_NAMES
            if PREDEFINED_FACTORS[name].min_history_bars is not None
        } == expected
        # 通用不变量:覆盖起点 >= 计算窗口(#361 入队门控)
        for name in BATCH6_FINANCIAL_NAMES:
            item = PREDEFINED_FACTORS[name]
            if item.min_history_bars is not None:
                assert item.window is not None, name
                assert item.min_history_bars >= item.window, name

    def test_data_dependencies_exact(self) -> None:
        expected: dict[str, tuple[str, ...]] = {
            name: (f"financial_indicators.{field}", "bars.close")
            for name, field, _ in DELTA_SPECS
        }
        expected.update(
            {
                name: (f"{kind}.{field}", "bars.close")
                for name, kind, field in YOY_SPECS
            }
        )
        expected.update(
            {
                "pa": ("financial_indicators.return_on_assets", "bars.close"),
                "eaa": ("financial_indicators.eps", "bars.close"),
                "eap": ("financial_indicators.eps", "bars.close"),
                "asset_growth_qoq": ("balance_sheets.total_assets",),
                "gpm_qoq": ("financial_indicators.gross_profit_margin", "bars.close"),
                "npm_q_qoq": ("financial_indicators.netprofit_margin_q",),
                "npm_ttm_qoq": ("financial_indicators.net_profit_margin", "bars.close"),
            }
        )
        expected.update(
            dict.fromkeys(
                ("opm_y", "opm_ttm"),
                ("income_statements.operate_profit", "income_statements.revenue"),
            )
        )
        expected.update(dict.fromkeys(LEVEL_NAMES, ("financial_indicators.eps",)))
        expected.update(
            {
                "opt_tpro": (
                    "income_statements.operate_profit",
                    "income_statements.total_profit",
                ),
                "equity_turnover": (
                    "income_statements.revenue",
                    "balance_sheets.total_hldr_eqy_inc_min_int",
                ),
                "cfcr": (
                    "cashflow_statements.n_cashflow_act",
                    "income_statements.int_exp",
                ),
                "ncf_to_market": (
                    "cashflow_statements.n_cashflow_act",
                    "cashflow_statements.n_cashflow_inv_act",
                    "cashflow_statements.n_cash_flows_fnc_act",
                    "daily_metrics.total_market_cap",
                ),
            }
        )
        assert {
            name: PREDEFINED_FACTORS[name].data_dependencies
            for name in BATCH6_FINANCIAL_NAMES
        } == expected

    def test_commit_anchors_distinct(self) -> None:
        commits = {
            name: predefined_factor_commit(name) for name in BATCH6_FINANCIAL_NAMES
        }
        assert len(set(commits.values())) == len(commits)


# --------------------------------------------------------------------- #
# 变化族(Δ 与同比):PIT 回看基值
# --------------------------------------------------------------------- #


class TestChangeFamily:
    def test_delta_hand_anchor_ranking(self) -> None:
        """两个标的的 ΔROE 截面 rank:高增量 1.0 / 低增量 0.5。"""
        days = [_day(offset) for offset in range(400)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        ann_a = _announcements([(_day(0), 10.0), (_day(300), 15.0)])
        ann_b = _announcements([(_day(0), 10.0), (_day(300), 12.0)])
        inp = _Input(
            bars=bars,
            datasets={
                "financial_indicators": {
                    "return_on_equity": {"A.SZ": ann_a, "B.SZ": ann_b}
                }
            },
            decision_dates=(_day(301),),
        )
        frame = _compute("delta_roe", inp)[_day(301)]
        assert frame["A.SZ"] == pytest.approx(1.0)
        assert frame["B.SZ"] == pytest.approx(0.5)

    def test_delta_base_comes_from_past_visibility(self) -> None:
        """回看基值必须来自 252 根 bar 前可见的公告,而非更近的行。"""
        days = [_day(offset) for offset in range(400)]
        bars = {"A.SZ": _bars(days)}
        ann = _announcements(
            [(_day(0), 1.0), (_day(300), 2.0), (_day(400), 3.0)]
        )
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"return_on_equity": {"A.SZ": ann}}},
            decision_dates=(_day(401),),
        )
        # 决策日 401 可见行 = 公告@400(值 3.0);回看日 = bar 401-252 = 149,
        # 该时点仅公告@0 可见(公告@300 尚不可见)→ Δ = 3.0 - 1.0
        assert _compute("delta_roe", inp)[_day(401)]["A.SZ"] == pytest.approx(1.0)

    def test_delta_insufficient_history_is_none(self) -> None:
        days = [_day(offset) for offset in range(100)]
        bars = {"A.SZ": _bars(days)}
        ann = _announcements([(_day(0), 1.0), (_day(90), 3.0)])
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"return_on_equity": {"A.SZ": ann}}},
            decision_dates=(_day(91),),
        )
        assert _compute("delta_roe", inp)[_day(91)]["A.SZ"] is None

    def test_yoy_base_nonpositive_is_none(self) -> None:
        days = [_day(offset) for offset in range(300)]
        bars = {"A.SZ": _bars(days)}
        ann = _announcements([(_day(0), 0.0), (_day(280), 5.0)])
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"return_on_assets": {"A.SZ": ann}}},
            decision_dates=(_day(281),),
        )
        assert _compute("yoy_roa", inp)[_day(281)]["A.SZ"] is None

    def test_yoy_ratio_uses_three_table_series(self) -> None:
        days = [_day(offset) for offset in range(300)]
        bars = {"A.SZ": _bars(days)}
        ann = _announcements([(_day(0), 100.0), (_day(280), 150.0)])
        inp = _Input(
            bars=bars,
            datasets={"balance_sheets": {"total_assets": {"A.SZ": ann}}},
            decision_dates=(_day(281),),
        )
        # 150 / 100 - 1 = 0.5(单标的截面 → rank 1.0)
        assert _compute("yoy_total_asset", inp)[_day(281)]["A.SZ"] == pytest.approx(1.0)

    def test_missing_calendar_and_missing_announcements(self) -> None:
        """无 bar 日历的标的缺测;无公告的标的结构性不入截面。"""
        days = [_day(offset) for offset in range(300)]
        bars = {"A.SZ": _bars(days), "B.SZ": _bars(days)}
        ann_a = _announcements([(_day(0), 1.0), (_day(280), 4.0)])
        ann_c = _announcements([(_day(0), 1.0), (_day(280), 9.0)])
        inp = _Input(
            bars=bars,
            datasets={
                "financial_indicators": {
                    "return_on_equity": {"A.SZ": ann_a, "C.SZ": ann_c}
                }
            },
            decision_dates=(_day(281),),
        )
        cross = _compute("delta_roe", inp)[_day(281)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        # B 无公告行(#401 结构性缺测)、C 非可交易域(无 bar)→ 均不入截面
        assert cross.get("B.SZ") is None
        assert cross.get("C.SZ") is None

    def test_values_match_independent_reference(self) -> None:
        """合成 4 标的 x 全决策日:Δ 与同比逐值 vs 独立参考(含 rank)。"""
        days = [_day(offset) for offset in range(560)]
        symbols = ("A.SZ", "B.SZ", "C.SZ", "D.SZ")
        bars = {symbol: _bars(days) for symbol in symbols}
        rows: dict[str, list[tuple[date, float | None]]] = {
            "A.SZ": [(_day(0), 1.0), (_day(260), 2.0), (_day(520), 5.0)],
            "B.SZ": [(_day(10), 4.0), (_day(270), 3.0), (_day(530), 6.0)],
            "C.SZ": [(_day(20), None), (_day(280), 7.0), (_day(540), 9.0)],
            "D.SZ": [(_day(30), 2.0), (_day(290), 2.0), (_day(550), 1.0)],
        }
        series = {symbol: _announcements(item) for symbol, item in rows.items()}
        decision_dates = tuple(_day(offset) for offset in range(40, 560, 17))
        for name, key, ratio in (
            ("delta_roe", ("financial_indicators", "return_on_equity"), False),
            ("yoy_roa", ("financial_indicators", "return_on_assets"), True),
            ("yoy_net_asset", ("balance_sheets", "total_hldr_eqy_exc_min_int"), True),
        ):
            kind, field = key
            inp = _Input(
                bars=bars,
                datasets={kind: {field: series}},
                decision_dates=decision_dates,
            )
            frame = _compute(name, inp)
            for day in decision_dates:
                expected = _ref_rank(
                    {
                        symbol: _ref_change(
                            series[symbol], bars[symbol], day, _YEAR_BARS, ratio=ratio
                        )
                        for symbol in symbols
                    }
                )
                assert frame[day] == pytest.approx(
                    dict(expected), nan_ok=True
                ), (name, day)

    def test_prefix_invariance_truncation(self) -> None:
        """截断挂载(available_at <= cut)前缀逐值不变(只消费更早行)。"""
        days = [_day(offset) for offset in range(560)]
        symbols = ("A.SZ", "B.SZ")
        bars = {symbol: _bars(days) for symbol in symbols}
        rows = {
            "A.SZ": [(_day(0), 1.0), (_day(260), 2.0), (_day(520), 5.0)],
            "B.SZ": [(_day(10), 4.0), (_day(270), 3.0), (_day(530), 6.0)],
        }
        full = {symbol: _announcements(item) for symbol, item in rows.items()}
        cut = _day(300)
        truncated = {
            symbol: _announcements(
                [row for row in item if row[0] + timedelta(days=1) <= cut]
            )
            for symbol, item in rows.items()
        }
        decision_dates = tuple(_day(offset) for offset in range(40, 301, 17))
        specs = (
            ("delta_roe", "financial_indicators", "return_on_equity"),
            ("yoy_roa", "financial_indicators", "return_on_assets"),
        )
        for name, kind, field in specs:
            full_frame = _compute(
                name,
                _Input(
                    bars=bars,
                    datasets={kind: {field: full}},
                    decision_dates=decision_dates,
                ),
            )
            cut_frame = _compute(
                name,
                _Input(
                    bars=bars,
                    datasets={kind: {field: truncated}},
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
# 加速度族(pa / eaa / eap):变化值的 lag 根 bar 再差分
# --------------------------------------------------------------------- #

#: 加速度族需要「回看 252 + 滞后 63」双段历史:公告从 bar 50 起(保证每行
#: 的变化值本身有限),决策日取末段
_ACCEL_ROWS: tuple[tuple[int, float], ...] = (
    (50, 1.0),
    (300, 1.0),
    (363, 1.0),
    (552, 2.0),
    (615, 3.0),
)


class TestAccelFamily:
    def _series(self, values: tuple[float, ...]) -> list[tuple[date, float]]:
        return [(_day(offset), value) for (offset, _), value in zip(
            _ACCEL_ROWS, values, strict=True
        )]

    def test_pa_hand_anchor_ranking(self) -> None:
        """PA = ΔROA - ΔROA_{t-63};三标的构造值 2.0 / 1.0 / 0.0 → rank 1 / 2/3 / 1/3。"""
        days = [_day(offset) for offset in range(700)]
        symbols = ("A.SZ", "B.SZ", "C.SZ")
        bars = {symbol: _bars(days) for symbol in symbols}
        series = {
            "A.SZ": _announcements(self._series((1.0, 1.0, 1.0, 2.0, 3.0))),
            "B.SZ": _announcements(self._series((1.0, 1.0, 1.0, 1.5, 2.0))),
            "C.SZ": _announcements(self._series((1.0, 1.0, 1.0, 2.0, 1.0))),
        }
        inp = _Input(
            bars=bars,
            datasets={
                "financial_indicators": {"return_on_assets": series}
            },
            decision_dates=(_day(616),),
        )
        cross = _compute("pa", inp)[_day(616)]
        # A: (3-1) - (1-1) = 2.0 → 1.0;B: (2-1) - 0 = 1.0 → 2/3;C: 0.0 → 1/3
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(2 / 3)
        assert cross["C.SZ"] == pytest.approx(1 / 3)

    def test_eaa_hand_anchor_ranking(self) -> None:
        """EAA = EPS 同比增速的变化量(公告行口径)。"""
        days = [_day(offset) for offset in range(700)]
        symbols = ("A.SZ", "B.SZ")
        bars = {symbol: _bars(days) for symbol in symbols}
        series = {
            "A.SZ": _announcements(self._series((1.0, 1.0, 1.0, 3.0, 5.0))),
            "B.SZ": _announcements(self._series((1.0, 1.0, 1.0, 3.0, 4.0))),
        }
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"eps": series}},
            decision_dates=(_day(616),),
        )
        cross = _compute("eaa", inp)[_day(616)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_eap_price_normalized_accel_values(self) -> None:
        """EAP = EGP - EGP_{t-63},EGP = (EPS_t - EPS_{t-252}) / 公告日 as-of close。"""
        days = [_day(offset) for offset in range(700)]
        bars = {
            "A.SZ": _bars(days, close=10.0),
            "B.SZ": _bars(days, close=100.0),
        }
        rows = {
            "A.SZ": self._series((1.0, 1.0, 1.0, 3.0, 5.0)),
            "B.SZ": self._series((1.0, 1.0, 1.0, 3.0, 5.0)),
        }
        series = {symbol: _announcements(item) for symbol, item in rows.items()}
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"eps": series}},
            decision_dates=(_day(616),),
        )
        cross = _compute("eap", inp)[_day(616)]
        # EGP@615 = (5-1)/close;EGP@552 = (3-1)/close → accel = 2/close
        # A: 0.2;B: 0.02 → A rank 1.0
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_accel_values_match_independent_reference(self) -> None:
        """三标的 x 全决策日:pa / eaa / eap 逐值 vs 独立参考(含 rank)。"""
        days = [_day(offset) for offset in range(700)]
        symbols = ("A.SZ", "B.SZ", "C.SZ")
        bars = {
            "A.SZ": _bars(days, close=10.0),
            "B.SZ": _bars(days, close=25.0),
            "C.SZ": _bars(days, close=40.0),
        }
        value_rows = {
            "A.SZ": (1.0, 1.0, 1.0, 2.0, 3.0),
            "B.SZ": (2.0, 1.0, 1.0, 1.5, 2.0),
            "C.SZ": (3.0, 2.0, 1.0, 1.0, 1.0),
        }
        series = {
            symbol: _announcements(
                [(_day(offset), value) for (offset, _), value in zip(
                    _ACCEL_ROWS, values, strict=True
                )]
            )
            for symbol, values in value_rows.items()
        }
        decision_dates = tuple(_day(offset) for offset in range(560, 700, 11))
        for name, ratio in (("pa", False), ("eaa", True), ("eap", True)):
            inp = _Input(
                bars=bars,
                datasets={
                    "financial_indicators": {
                        "return_on_assets": series,
                        "eps": series,
                    }
                },
                decision_dates=decision_dates,
            )
            frame = _compute(name, inp)
            for day in decision_dates:
                expected = _ref_rank(
                    {
                        symbol: _ref_accel(
                            series[symbol],
                            bars[symbol],
                            day,
                            _YEAR_BARS,
                            _QUARTER_BARS,
                            ratio=ratio,
                        )
                        for symbol in symbols
                    }
                )
                assert frame[day] == pytest.approx(expected, nan_ok=True), (name, day)

    def test_accel_missing_endpoint_is_none(self) -> None:
        days = [_day(offset) for offset in range(400)]
        bars = {"A.SZ": _bars(days)}
        ann = _announcements([(_day(315), 3.0)])
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"return_on_assets": {"A.SZ": ann}}},
            decision_dates=(_day(316),),
        )
        assert _compute("pa", inp)[_day(316)]["A.SZ"] is None


class TestStepRatioFamily:
    def test_asset_growth_qoq_hand_anchor(self) -> None:
        bars = {"A.SZ": _bars([_day(offset) for offset in range(10)])}
        ann = _announcements([(_day(0), 100.0), (_day(2), 150.0)])
        inp = _Input(
            bars=bars,
            datasets={"balance_sheets": {"total_assets": {"A.SZ": ann}}},
            decision_dates=(_day(3),),
        )
        # 150/100 - 1 = 0.5(单标的 rank 1.0);无 bar 回看不声明覆盖起点
        assert PREDEFINED_FACTORS["asset_growth_qoq"].min_history_bars is None
        assert _compute("asset_growth_qoq", inp)[_day(3)]["A.SZ"] == pytest.approx(1.0)

    def test_step_ratio_base_nonpositive_is_none(self) -> None:
        bars = {"A.SZ": _bars([_day(offset) for offset in range(10)])}
        ann = _announcements([(_day(0), 0.0), (_day(2), 5.0)])
        inp = _Input(
            bars=bars,
            datasets={
                "financial_indicators": {"netprofit_margin_q": {"A.SZ": ann}}
            },
            decision_dates=(_day(3),),
        )
        assert _compute("npm_q_qoq", inp)[_day(3)]["A.SZ"] is None

    def test_step_ratio_works_without_bar_calendar(self) -> None:
        """环比族不依赖 bar 回看:无 bars 也照常出值。"""
        ann = _announcements([(_day(0), 100.0), (_day(2), 120.0)])
        inp = _Input(
            bars={},
            datasets={"balance_sheets": {"total_assets": {"A.SZ": ann}}},
            decision_dates=(_day(3),),
            tradable=("A.SZ",),
        )
        assert _compute("asset_growth_qoq", inp)[_day(3)]["A.SZ"] == pytest.approx(1.0)

    def test_gpm_qoq_uses_quarter_lookback(self) -> None:
        """gpm_qoq 为 63 根 bar 回看(与 npm_q_qoq 的公告序差分区分)。"""
        days = [_day(offset) for offset in range(200)]
        bars = {"A.SZ": _bars(days)}
        ann = _announcements([(_day(0), 0.2), (_day(130), 0.3)])
        inp = _Input(
            bars=bars,
            datasets={
                "financial_indicators": {"gross_profit_margin": {"A.SZ": ann}}
            },
            decision_dates=(_day(131),),
        )
        # 回看日 = bar 131-63 = 68 → 可见公告@0(0.2)→ 0.3/0.2 - 1 = 0.5
        assert _compute("gpm_qoq", inp)[_day(131)]["A.SZ"] == pytest.approx(1.0)


# --------------------------------------------------------------------- #
# 水平族与三表比值族
# --------------------------------------------------------------------- #


class TestLevelAndRatioFamily:
    def test_eps_level_rank(self) -> None:
        days = [_day(offset) for offset in range(30)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ", "C.SZ")}
        series = {
            "A.SZ": _announcements([(_day(0), 3.0)]),
            "B.SZ": _announcements([(_day(0), 1.0)]),
            "C.SZ": _announcements([(_day(0), 2.0)]),
        }
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"eps": series}},
            decision_dates=(_day(1),),
        )
        for name in LEVEL_NAMES:
            cross = _compute(name, inp)[_day(1)]
            assert cross["A.SZ"] == pytest.approx(1.0), name
            assert cross["C.SZ"] == pytest.approx(2 / 3), name
            assert cross["B.SZ"] == pytest.approx(1 / 3), name

    def test_opm_ratio_rank(self) -> None:
        days = [_day(offset) for offset in range(30)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {
                    "operate_profit": {
                        "A.SZ": _announcements([(_day(0), 30.0)]),
                        "B.SZ": _announcements([(_day(0), 10.0)]),
                    },
                    "revenue": {
                        "A.SZ": _announcements([(_day(0), 100.0)]),
                        "B.SZ": _announcements([(_day(0), 100.0)]),
                    },
                }
            },
            decision_dates=(_day(1),),
        )
        cross = _compute("opm_y", inp)[_day(1)]
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_ratio_denominator_nonpositive_is_none(self) -> None:
        days = [_day(offset) for offset in range(30)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        inp = _Input(
            bars=bars,
            datasets={
                "cashflow_statements": {
                    "n_cashflow_act": {
                        "A.SZ": _announcements([(_day(0), 50.0)]),
                        "B.SZ": _announcements([(_day(0), 50.0)]),
                    }
                },
                "income_statements": {
                    "int_exp": {
                        "A.SZ": _announcements([(_day(0), 10.0)]),
                        "B.SZ": _announcements([(_day(0), 0.0)]),
                    }
                },
            },
            decision_dates=(_day(1),),
        )
        cross = _compute("cfcr", inp)[_day(1)]
        # B 利息费用 0 → 分母 <= 0 → None,不入 rank 分母
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] is None

    def test_opt_tpro_and_equity_turnover(self) -> None:
        days = [_day(offset) for offset in range(30)]
        bars = {"A.SZ": _bars(days)}
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {
                    "operate_profit": {
                        "A.SZ": _announcements([(_day(0), 40.0)])
                    },
                    "total_profit": {"A.SZ": _announcements([(_day(0), 50.0)])},
                    "revenue": {"A.SZ": _announcements([(_day(0), 200.0)])},
                },
                "balance_sheets": {
                    "total_hldr_eqy_inc_min_int": {
                        "A.SZ": _announcements([(_day(0), 100.0)])
                    }
                },
            },
            decision_dates=(_day(1),),
        )
        # 40/50 = 0.8 与 200/100 = 2.0(单标的 rank 均为 1.0;此处锁定
        # 取数不炸 + 值域;数值口径由 ratio 参考测试覆盖)
        assert _compute("opt_tpro", inp)[_day(1)]["A.SZ"] == pytest.approx(1.0)
        assert _compute("equity_turnover", inp)[_day(1)]["A.SZ"] == pytest.approx(1.0)

    def test_ratio_values_match_independent_reference(self) -> None:
        days = [_day(offset) for offset in range(30)]
        symbols = ("A.SZ", "B.SZ", "C.SZ")
        bars = {symbol: _bars(days) for symbol in symbols}
        profit = {
            "A.SZ": _announcements([(_day(0), 30.0)]),
            "B.SZ": _announcements([(_day(0), 10.0)]),
            "C.SZ": _announcements([(_day(0), 20.0)]),
        }
        revenue = {
            "A.SZ": _announcements([(_day(0), 100.0)]),
            "B.SZ": _announcements([(_day(0), 50.0)]),
            "C.SZ": _announcements([(_day(0), 200.0)]),
        }
        inp = _Input(
            bars=bars,
            datasets={
                "income_statements": {
                    "operate_profit": profit,
                    "revenue": revenue,
                }
            },
            decision_dates=(_day(1),),
        )
        expected = _ref_rank(
            {
                symbol: _visible_value(profit[symbol], _day(1))
                / _visible_value(revenue[symbol], _day(1))
                for symbol in symbols
            }
        )
        assert _compute("opm_ttm", inp)[_day(1)] == pytest.approx(
            expected, nan_ok=True
        )

    def test_ncf_to_market_three_flows_and_market_cap(self) -> None:
        days = [_day(offset) for offset in range(30)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        flows = {
            "n_cashflow_act": {
                "A.SZ": _announcements([(_day(0), 10.0)]),
                "B.SZ": _announcements([(_day(0), 1.0)]),
            },
            "n_cashflow_inv_act": {
                "A.SZ": _announcements([(_day(0), -5.0)]),
                "B.SZ": _announcements([(_day(0), 1.0)]),
            },
            "n_cash_flows_fnc_act": {
                "A.SZ": _announcements([(_day(0), 3.0)]),
                "B.SZ": _announcements([(_day(0), 1.0)]),
            },
        }
        market_cap = {
            "A.SZ": _bars(days, close=100.0),
            "B.SZ": _bars(days, close=1000.0),
        }
        inp = _Input(
            bars=bars,
            datasets={"cashflow_statements": flows},
            daily={"total_market_cap": market_cap},
            decision_dates=(_day(1),),
        )
        cross = _compute("ncf_to_market", inp)[_day(1)]
        # A: (10 - 5 + 3)/100 = 0.08;B: 3/1000 = 0.003 → A 排名更高
        assert cross["A.SZ"] == pytest.approx(1.0)
        assert cross["B.SZ"] == pytest.approx(0.5)

    def test_ncf_to_market_missing_flow_is_none(self) -> None:
        """三项净现金流缺一不可(required=True)分子整体缺测。"""
        days = [_day(offset) for offset in range(30)]
        bars = {"A.SZ": _bars(days)}
        inp = _Input(
            bars=bars,
            datasets={
                "cashflow_statements": {
                    "n_cashflow_act": {
                        "A.SZ": _announcements([(_day(0), 10.0)])
                    },
                    "n_cash_flows_fnc_act": {
                        "A.SZ": _announcements([(_day(0), 3.0)])
                    },
                }
            },
            daily={"total_market_cap": {"A.SZ": _bars(days, close=100.0)}},
            decision_dates=(_day(1),),
        )
        assert _compute("ncf_to_market", inp)[_day(1)]["A.SZ"] is None

    def test_decision_day_before_first_announcement_is_none(self) -> None:
        days = [_day(offset) for offset in range(30)]
        bars = {"A.SZ": _bars(days)}
        ann = _announcements([(_day(10), 5.0)])
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"eps": {"A.SZ": ann}}},
            decision_dates=(_day(5),),
        )
        assert _compute("eps_ttm", inp)[_day(5)]["A.SZ"] is None


# --------------------------------------------------------------------- #
# 通用不变量
# --------------------------------------------------------------------- #


class TestBatchWideInvariants:
    def test_every_batch6_factor_has_title_and_single_version(self) -> None:
        for name in BATCH6_FINANCIAL_NAMES:
            item = PREDEFINED_FACTORS[name]
            assert item.title, name
            assert item.implementation_version == "1", name

    def test_rank_is_bounded_zero_one(self) -> None:
        days = [_day(offset) for offset in range(30)]
        bars = {symbol: _bars(days) for symbol in ("A.SZ", "B.SZ")}
        ann = {
            "A.SZ": _announcements([(_day(0), 9.0)]),
            "B.SZ": _announcements([(_day(0), 1.0)]),
        }
        inp = _Input(
            bars=bars,
            datasets={"financial_indicators": {"eps": ann}},
            decision_dates=(_day(1),),
        )
        for name in ("eps_q", "eps_y"):
            for value in _compute(name, inp)[_day(1)].values():
                assert value is None or 0.0 < value <= 1.0, name
