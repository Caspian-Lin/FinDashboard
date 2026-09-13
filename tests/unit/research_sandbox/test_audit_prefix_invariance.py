"""前缀不变性审计引擎 —— 阳性/阴性对照(issue #359)。

核心命题:``value[t] 只许依赖 available_at <= t 的数据``。mock build_fn
对平台语义建模:

* **数据可见域跟随请求窗口** —— truncation 模式下 ``build([t0, cut])``
  只能读到窗口末端(cut)之前的数据,与真实执行「每窗口独立挂载」一致;
* **perturb_from = cut 表示 cut 之后(严格大于 cut 日)的数据被扰动** ——
  cut 当日及之前的可用数据不变。

对照物:

* **阴性对照**(纯 trailing 因子:value[t] 只读 <= t 的数据)—— 截断与
  扰动双模式都必须通过;
* **阳性对照**(故意读未来:value[t] 用 t+1 的价格)—— 双模式都必须
  检出,并报告首个分歧日期与该日因子值对照(截断在 cut 日:未来行被
  截掉;扰动在 cut 日:t+1 行被扰动)。

另覆盖:审计输入校验(空窗口 / 乱序 / 窗口外 cut)、检出即短路
(checks_run 只计到分歧为止)、None 与 NaN 视为同一缺测、kit
``FactorSeries`` 直接作为被审计对象。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest

from finboard_backtest.research_sandbox.audit import (
    PrefixInvarianceReport,
    run_prefix_invariance_audit,
)
from finboard_research_kit.result import FactorSeries

_DATES = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5), date(2024, 6, 6)]
_SYMBOLS = ("600000.SH",)
_CUTS = [date(2024, 6, 4), date(2024, 6, 5)]

#: 「真实」日线表(available_at = 各业务日;trailing 与 lookahead 共用)
_PRICES = {
    date(2024, 6, 1): 10.0,
    date(2024, 6, 2): 10.5,
    date(2024, 6, 3): 11.0,
    date(2024, 6, 4): 10.8,
    date(2024, 6, 5): 11.4,
    date(2024, 6, 6): 11.9,
}


def _visible_table(dates: Sequence[date], *, perturb_from: date | None) -> dict[date, float]:
    """build_fn 可见的数据面 = 请求窗口末端之前的价格 + 扰动规则。

    - 可见域上界 = max(dates)(truncation:窗口截断 → 未来行不存在);
    - perturb_from=cut:严格晚于 cut 的行翻倍(扰动),cut 当日及之前不变。
    """
    horizon = max(dates)
    table = {d: v for d, v in _PRICES.items() if d <= horizon}
    if perturb_from is not None:
        table = {
            d: (v * 2.0 if d > perturb_from else v) for d, v in table.items()
        }
    return table


def _series(values: dict[date, dict[str, float | None]], dates: Sequence[date]) -> FactorSeries:
    return FactorSeries(
        dates=tuple(dates),
        values={d: values.get(d, {"600000.SH": None}) for d in dates},
    )


async def _trailing_build_fn(dates: Sequence[date], perturb_from: date | None = None) -> FactorSeries:
    """阴性对照:trailing 动量 —— value[t] 只依赖 <= t 的可见数据。"""
    table = _visible_table(dates, perturb_from=perturb_from)
    ordered = sorted(table)
    values: dict[date, dict[str, float | None]] = {}
    for day in dates:
        history = [table[d] for d in ordered if d <= day]
        values[day] = {"600000.SH": history[-1] / history[0] - 1.0}
    return _series(values, dates)


async def _lookahead_build_fn(dates: Sequence[date], perturb_from: date | None = None) -> FactorSeries:
    """阳性对照:故意读未来 —— value[t] 用 t+1(可见域内)的价格。

    窗口末端无次日数据时产出 None(缺测,不构成契约违规本身)。
    """
    table = _visible_table(dates, perturb_from=perturb_from)
    ordered = sorted(table)
    values: dict[date, dict[str, float | None]] = {}
    for day in dates:
        idx = ordered.index(day)
        if idx + 1 >= len(ordered):
            values[day] = {"600000.SH": None}
            continue
        next_day = ordered[idx + 1]
        values[day] = {"600000.SH": table[next_day] / table[day] - 1.0}
    return _series(values, dates)


class TestNegativeControl:
    async def test_trailing_passes_truncation(self) -> None:
        report = await run_prefix_invariance_audit(
            _trailing_build_fn, mode="truncation", cut_points=_CUTS, dates=_DATES
        )
        assert isinstance(report, PrefixInvarianceReport)
        assert report.passed
        assert report.first_divergence_date is None
        assert report.checks_run == len(_CUTS)

    async def test_trailing_passes_perturbation(self) -> None:
        report = await run_prefix_invariance_audit(
            _trailing_build_fn, mode="perturbation", cut_points=_CUTS, dates=_DATES
        )
        assert report.passed
        assert report.first_divergence_date is None
        assert report.checks_run == len(_CUTS)


class TestPositiveControl:
    async def test_lookahead_detected_by_truncation(self) -> None:
        """截断检出:cut=6/4 的重算里 6/5/6/6 行不存在,6/4 截面从
        「用 6/5 价格」塌成缺测 → 与全窗口基线在 6/4 分歧。"""
        report = await run_prefix_invariance_audit(
            _lookahead_build_fn, mode="truncation", cut_points=_CUTS, dates=_DATES
        )
        assert not report.passed
        assert report.first_divergence_date == date(2024, 6, 4)
        assert report.divergent_cut == date(2024, 6, 4)
        assert "600000.SH" in report.divergent_symbols
        assert report.baseline_values["600000.SH"] == pytest.approx(
            _PRICES[date(2024, 6, 5)] / _PRICES[date(2024, 6, 4)] - 1.0
        )
        assert report.variant_values["600000.SH"] is None
        assert report.failure is not None
        assert "PIT 契约" in report.failure

    async def test_lookahead_detected_by_perturbation(self) -> None:
        """扰动检出:cut=6/4 之后翻倍,6/4 截面(读了 6/5)随之改变。"""
        report = await run_prefix_invariance_audit(
            _lookahead_build_fn, mode="perturbation", cut_points=_CUTS, dates=_DATES
        )
        assert not report.passed
        assert report.first_divergence_date == date(2024, 6, 4)
        assert report.divergent_cut == date(2024, 6, 4)
        assert report.baseline_values["600000.SH"] != report.variant_values["600000.SH"]
        assert report.checks_run == 1  # 检出即短路,不再跑剩余 cut

    async def test_failure_message_carries_value_pair(self) -> None:
        report = await run_prefix_invariance_audit(
            _lookahead_build_fn, mode="perturbation", cut_points=_CUTS, dates=_DATES
        )
        assert report.failure is not None
        assert "首个分歧日期 2024-06-04" in report.failure
        assert repr(report.baseline_values["600000.SH"]) in report.failure


class TestInputValidation:
    async def test_empty_dates_rejected(self) -> None:
        async def build(dates: Any, perturb_from: Any = None) -> FactorSeries:
            return _series({}, list(dates))

        with pytest.raises(ValueError, match="dates 不能为空"):
            await run_prefix_invariance_audit(
                build, mode="truncation", cut_points=[], dates=[]
            )

    async def test_unsorted_dates_rejected(self) -> None:
        async def build(dates: Any, perturb_from: Any = None) -> FactorSeries:
            return _series({}, list(dates))

        with pytest.raises(ValueError, match="升序"):
            await run_prefix_invariance_audit(
                build,
                mode="truncation",
                cut_points=_CUTS,
                dates=[_DATES[1], _DATES[0]],
            )

    async def test_cut_outside_window_rejected(self) -> None:
        async def build(dates: Any, perturb_from: Any = None) -> FactorSeries:
            return _series({}, list(dates))

        with pytest.raises(ValueError, match="窗口外"):
            await run_prefix_invariance_audit(
                build,
                mode="truncation",
                cut_points=[date(2024, 1, 1)],
                dates=_DATES,
            )

    async def test_default_dates_from_cut_points(self) -> None:
        """dates 缺省 = cut_points(最后一个 cut 为空操作对照)。"""
        report = await run_prefix_invariance_audit(
            _trailing_build_fn, mode="truncation", cut_points=_CUTS
        )
        assert report.passed
        assert report.audited_dates == tuple(_CUTS)

    async def test_nan_and_none_treated_as_missing(self) -> None:
        """None 与 NaN 视为同一缺测,不误报分歧。"""

        async def build(dates: Sequence[date], perturb_from: date | None = None) -> FactorSeries:
            probe = float("nan") if perturb_from is None else None
            values = {d: {"600000.SH": probe} for d in dates}
            return _series(values, dates)

        report = await run_prefix_invariance_audit(
            build, mode="perturbation", cut_points=_CUTS, dates=_DATES
        )
        assert report.passed
