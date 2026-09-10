"""issue #402:批次 4 价值 / 质量预置因子(val_* / qlt_* / qmj_*)的目录与逐值单测。

* 目录不变量:11 个 ``val_*`` / 9 个 ``qlt_*`` / 5 个 ``qmj_*``;数据依赖
  字段必须真实存在于领域 dataclass(= 发布白名单口径);方向锁定;
  ``qmj_*`` 全部 ``cross_section=True``(采样面收窄,#380);
* **逐值对照** —— 比值因子 = 公告步进分子 / 决策日可见市值分母(公告前
  缺测、修订覆盖、公告间持有);精确股息率 = 除权除息日归属的滚动 12 月
  每股分红(同分红年度取最新进展行,半开窗口边界锁定);QMJ 支柱与综合
  逐值手算锚点(含 LOWER 反转、缺测支柱均值合成、benchmark-only 剔除)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from finboard_backtest.factors.predefined.context import (
    DividendEventHistory,
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


def _anchor(announcement: date) -> datetime:
    """PIT 锚 = 公告日次日零点(UTC,与领域 _parse_* 同口径)。"""
    return datetime.combine(
        announcement + timedelta(days=1), datetime.min.time(), tzinfo=UTC
    )


def _ann_series(
    announcements: list[tuple[date, float | None]],
) -> SymbolSeries:
    """公告序列:每行一次公告,available_at = 公告日次日 00:00。"""
    dates = tuple(day for day, _ in announcements)
    values = np.array(
        [math.nan if v is None else float(v) for _, v in announcements],
        dtype=np.float64,
    )
    return SymbolSeries(
        dates=dates,
        values=values,
        available_at=tuple(_anchor(day) for day in dates),
    )


def _daily_series(rows: list[tuple[date, float | None]]) -> SymbolSeries:
    """daily_metrics 序列:逐交易日一行,available_at = 当日 16:00 UTC。"""
    dates = tuple(day for day, _ in rows)
    values = np.array(
        [math.nan if v is None else float(v) for _, v in rows], dtype=np.float64
    )
    return SymbolSeries(
        dates=dates,
        values=values,
        available_at=tuple(
            datetime.combine(day, datetime.min.time()).replace(
                hour=16, tzinfo=UTC
            )
            for day in dates
        ),
    )


def _event_history(
    rows: list[tuple[date, date | None, date | None, float | None]],
) -> DividendEventHistory:
    """分红事件史:(announcement_date, report_period, ex_date, cash_div)。"""
    dates = tuple(day for day, _, _, _ in rows)
    return DividendEventHistory(
        announcement_dates=dates,
        available_at=tuple(_anchor(day) for day in dates),
        report_periods=tuple(period for _, period, _, _ in rows),
        ex_dates=tuple(ex for _, _, ex, _ in rows),
        cash_div=np.array(
            [math.nan if v is None else float(v) for *_, v in rows],
            dtype=np.float64,
        ),
    )


class _FakeInput(PredefinedFactorInput):
    """测试输入面:公告类数据集 / 日频指标 / 分红事件史显式注入。"""

    def __init__(
        self,
        datasets: dict[str, dict[str, dict[str, SymbolSeries]]],
        *,
        events: dict[str, DividendEventHistory] | None = None,
        decision_dates: tuple[date, ...] = (),
        universe: tuple[str, ...] = (),
        tradable: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(
            factor_name="test",
            decision_dates=decision_dates,
            tradable_symbols=tradable or universe,
            benchmark_only_symbols=frozenset(),
        )
        self._datasets = datasets
        self._events = events or {}
        self._universe = universe

    def bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        return {}

    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        return dict(self._datasets.get("daily_metrics", {}).get(field, {}))

    def financial_indicators(self, field: str) -> dict[str, SymbolSeries]:
        return dict(
            self._datasets.get("financial_indicators", {}).get(field, {})
        )

    def research_dataset(self, kind: str, field: str) -> dict[str, SymbolSeries]:
        if kind == "daily_metrics":
            return self.daily_metrics(field)
        return dict(self._datasets.get(kind, {}).get(field, {}))

    def dividend_events(self) -> dict[str, DividendEventHistory]:
        return dict(self._events)

    def industry_groups(self) -> dict[str, str | None]:
        return {}

    def sample(
        self,
        series_by_symbol: Any,
        per_symbol_values: Any,
    ) -> FactorSeriesFrame:
        return sample_series_frame(
            series_by_symbol,
            per_symbol_values,
            decision_dates=self.decision_dates,
            value_universe=tuple(
                s for s in self._universe if s in per_symbol_values
            ),
        )


def _datasets(
    kind: str, field: str, series_by_symbol: dict[str, SymbolSeries]
) -> dict[str, dict[str, dict[str, SymbolSeries]]]:
    return {kind: {field: series_by_symbol}}


def _one(symbol: str, series: SymbolSeries) -> dict[str, SymbolSeries]:
    return {symbol: series}


# --------------------------------------------------------------------- #
# 目录不变量
# --------------------------------------------------------------------- #


class TestCatalog402:
    def test_batch4_factor_counts(self) -> None:
        names = predefined_factor_names()
        assert [n for n in names if n.startswith("val_")] == [
            "val_bm",
            "val_dps_ttm",
            "val_ebit_to_market",
            "val_ebitda_to_market",
            "val_earnings_to_price",
            "val_fcf_to_market",
            "val_ocf_to_market",
            "val_ocf_to_price",
            "val_sales_to_price",
            "val_tangible_bm",
            "val_dividend_yield",
        ] or [
            n for n in names if n.startswith("val_")
        ] == sorted(n for n in names if n.startswith("val_"))
        assert len([n for n in names if n.startswith("val_")]) == 11
        assert len([n for n in names if n.startswith("qlt_")]) == 9
        assert {n for n in names if n.startswith("qmj")} == {
            "qmj",
            "qmj_growth",
            "qmj_payout",
            "qmj_profitability",
            "qmj_safety",
        }

    def test_family_and_shape(self) -> None:
        for name in (
            n for n in PREDEFINED_FACTORS
            if n.startswith(("val_", "qlt_", "qmj"))
        ):
            item = get_predefined_factor(name)
            expected_family = (
                "value" if name.startswith("val_") else "quality"
            )
            assert item.family == expected_family, name
            assert item.window is None, name
            assert item.signal_eligible is True, name
            assert item.implementation_version == "1", name
            # 截面产物只有 QMJ 族(qmj_ 前缀含综合 qmj 本身)
            assert item.cross_section is name.startswith("qmj"), name

    def test_dependency_fields_exist_in_domain_records(self) -> None:
        import dataclasses

        from finboard_data.research import (
            BalanceSheet,
            CashflowStatement,
            DailySecurityMetrics,
            DividendRecord,
            FinancialIndicator,
            IncomeStatement,
        )

        domain: dict[str, Any] = {
            "financial_indicators": FinancialIndicator,
            "income_statements": IncomeStatement,
            "balance_sheets": BalanceSheet,
            "cashflow_statements": CashflowStatement,
            "dividends": DividendRecord,
            "daily_metrics": DailySecurityMetrics,
        }
        for name in (
            n for n in PREDEFINED_FACTORS
            if n.startswith(("val_", "qlt_", "qmj"))
        ):
            for dep in get_predefined_factor(name).data_dependencies:
                kind, field = dep.split(".", 1)
                assert kind in domain, (name, dep)
                field_names = {
                    f.name for f in dataclasses.fields(domain[kind])
                }
                assert field in field_names, (name, dep)

    def test_lower_direction_factors_locked(self) -> None:
        lowered = {
            name
            for name in PREDEFINED_FACTORS
            if name.startswith(("val_", "qlt_"))
            and PREDEFINED_FACTORS[name].direction is FactorPreference.LOWER
        }
        assert lowered == {
            "qlt_prepayment_ratio",
            "qlt_payables_turnover_detail",
            "qlt_accrual_ratio",
        }

    def test_qmj_deps_are_union_of_pillars(self) -> None:
        pillar_deps = {
            dep
            for name in ("qmj_profitability", "qmj_growth", "qmj_safety", "qmj_payout")
            for dep in get_predefined_factor(name).data_dependencies
        }
        assert set(get_predefined_factor("qmj").data_dependencies) == pillar_deps


# --------------------------------------------------------------------- #
# 比值因子(公告步进分子 / 日频分母)
# --------------------------------------------------------------------- #


class TestRatioFactorValues:
    def test_bm_announcement_step_with_revision(self) -> None:
        """账面市值比:公告前缺测 / 修订覆盖 / 公告间持有,分母逐日更新。"""
        equity = _ann_series(
            [(ANN_A, 100.0), (date(2024, 6, 2), 105.0), (ANN_B, 110.0)]
        )
        caps = _daily_series(
            [(date(2024, 4, d), 1000.0) for d in range(25, 31)]
        )
        decisions = (
            date(2024, 4, 26),  # 公告日当天(锚 4/27)→ None
            date(2024, 4, 27),  # 锚点日 → 100/1000
            date(2024, 4, 29),  # 修订(6/2)前 → 仍 100/1000
        )
        inp = _FakeInput(
            {
                "balance_sheets": {
                    "total_hldr_eqy_exc_min_int": _one("600000.SH", equity)
                },
                "daily_metrics": {"total_market_cap": _one("600000.SH", caps)},
            },
            decision_dates=decisions,
            universe=("600000.SH",),
        )
        frame = get_predefined_factor("val_bm").compute(inp)
        assert frame[decisions[0]]["600000.SH"] is None
        assert frame[decisions[1]]["600000.SH"] == pytest.approx(0.10)
        assert frame[decisions[2]]["600000.SH"] == pytest.approx(0.10)

    def test_missing_denominator_and_zero_denominator(self) -> None:
        equity = _ann_series([(ANN_A, 100.0)])
        inp = _FakeInput(
            {
                "balance_sheets": {
                    "total_hldr_eqy_exc_min_int": _one("600000.SH", equity)
                },
                "daily_metrics": {
                    "total_market_cap": _one(
                        "600000.SH", _daily_series([(ANN_A, None)])
                    )
                },
            },
            decision_dates=(date(2024, 5, 8),),
            universe=("600000.SH",),
        )
        frame = get_predefined_factor("val_bm").compute(inp)
        assert frame[date(2024, 5, 8)]["600000.SH"] is None

        zero = _FakeInput(
            {
                "balance_sheets": {
                    "total_hldr_eqy_exc_min_int": _one("600000.SH", equity)
                },
                "daily_metrics": {
                    "total_market_cap": _one(
                        "600000.SH", _daily_series([(ANN_A, 0.0)])
                    )
                },
            },
            decision_dates=(date(2024, 5, 8),),
            universe=("600000.SH",),
        )
        frame_zero = get_predefined_factor("val_bm").compute(zero)
        assert frame_zero[date(2024, 5, 8)]["600000.SH"] is None

    def test_tangible_bm_optional_components_zero_filled(self) -> None:
        """有形账面市值比:商誉缺项按 0 计;核心权益缺测 → None。"""
        days = [date(2024, 4, 29)]
        equity = _one("S", _ann_series([(ANN_A, 100.0)]))
        caps = _one("S", _daily_series([(date(2024, 4, 29), 1000.0)]))

        def build(goodwill: float | None, intan: float | None) -> Any:
            return _FakeInput(
                {
                    "balance_sheets": {
                        "total_hldr_eqy_exc_min_int": equity,
                        "goodwill": _one(
                            "S", _ann_series([(ANN_A, goodwill)])
                        ),
                        "intan_assets": _one(
                            "S", _ann_series([(ANN_A, intan)])
                        ),
                    },
                    "daily_metrics": {"total_market_cap": caps},
                },
                decision_dates=tuple(days),
                universe=("S",),
            )

        frame = get_predefined_factor("val_tangible_bm").compute(
            build(10.0, 5.0)
        )
        assert frame[days[0]]["S"] == pytest.approx(85.0 / 1000.0)
        frame_no_gw = get_predefined_factor("val_tangible_bm").compute(
            build(None, 5.0)
        )
        assert frame_no_gw[days[0]]["S"] == pytest.approx(95.0 / 1000.0)
        frame_no_equity = get_predefined_factor("val_tangible_bm").compute(
            _FakeInput(
                {
                    "balance_sheets": {
                        "total_hldr_eqy_exc_min_int": _one(
                            "S", _ann_series([(ANN_A, None)])
                        ),
                        "goodwill": _one("S", _ann_series([(ANN_A, 10.0)])),
                        "intan_assets": _one("S", _ann_series([(ANN_A, 5.0)])),
                    },
                    "daily_metrics": {"total_market_cap": caps},
                },
                decision_dates=tuple(days),
                universe=("S",),
            )
        )
        assert frame_no_equity[days[0]]["S"] is None

    def test_symbols_without_statements_absent(self) -> None:
        """无分子数据的标的 = 结构性缺测,不入截面(#401 同语义)。"""
        inp = _FakeInput(
            {
                "income_statements": {
                    "revenue": _one("A", _ann_series([(ANN_A, 500.0)]))
                },
                "daily_metrics": {
                    "total_market_cap": _one(
                        "B", _daily_series([(date(2024, 4, 29), 900.0)])
                    )
                },
            },
            decision_dates=(date(2024, 4, 29),),
            universe=("A", "B"),
        )
        frame = get_predefined_factor("val_sales_to_price").compute(inp)
        assert set(frame[date(2024, 4, 29)]) == {"A"}

    def test_ocf_to_price_uses_circulating_cap(self) -> None:
        ocf = _one("S", _ann_series([(ANN_A, 60.0)]))
        inp = _FakeInput(
            {
                "cashflow_statements": {"n_cashflow_act": ocf},
                "daily_metrics": {
                    "circulating_market_cap": _one(
                        "S", _daily_series([(date(2024, 4, 29), 800.0)])
                    )
                },
            },
            decision_dates=(date(2024, 4, 29),),
            universe=("S",),
        )
        frame = get_predefined_factor("val_ocf_to_price").compute(inp)
        assert frame[date(2024, 4, 29)]["S"] == pytest.approx(0.075)


# --------------------------------------------------------------------- #
# 精确股息率 / DPS(dividend 明细,除权除息日归属)
# --------------------------------------------------------------------- #

# 2023 年度:预案 3/25(0.5)、实施 5/10(ex 5/20)
# 2024 中期:预案 8/20(0.3)、实施 9/15(ex 9/25)
# 2024 年度:预案 3/20(0.6)、实施 5/15(ex 5/25)
_EVENTS = _event_history(
    [
        (date(2024, 3, 25), date(2023, 12, 31), None, 0.5),
        (date(2024, 5, 10), date(2023, 12, 31), date(2024, 5, 20), 0.5),
        (date(2024, 8, 20), date(2024, 6, 30), None, 0.3),
        (date(2024, 9, 15), date(2024, 6, 30), date(2024, 9, 25), 0.3),
        (date(2025, 3, 20), date(2024, 12, 31), None, 0.6),
        (date(2025, 5, 15), date(2024, 12, 31), date(2025, 5, 25), 0.6),
    ]
)


class TestDividendYieldPrecise:
    def _inp(
        self,
        decisions: tuple[date, ...],
        close: float | None = 10.0,
    ) -> _FakeInput:
        daily: dict[str, Any] = {}
        if close is not None:
            daily["close"] = _one(
                "S", _daily_series([(decisions[0], close)])
            )
        return _FakeInput(
            (daily and {"daily_metrics": daily}) or {},
            events={"S": _EVENTS},
            decision_dates=decisions,
            universe=("S",),
        )

    def test_dps_ttm_window_and_progression_dedup(self) -> None:
        """同年度进展行去重;实施除息日进窗;预案(ex None)不计。"""
        decisions = (
            date(2024, 3, 25),  # 无可见行 → None(未知 ≠ 零分红)
            date(2024, 4, 1),  # 仅预案可见(ex None)→ 0(已知近 12 月无除息)
            date(2024, 5, 19),  # 实施已公告、除息日未到 → 0
            date(2024, 5, 20),  # 除息日当天 → 0.5
            date(2024, 9, 26),  # 中期 9/25 入窗 → 0.5 + 0.3 = 0.8
            date(2025, 5, 20),  # 半开窗:2024-05-20 恰出窗 → 仅 0.3
            date(2025, 5, 26),  # 2025 年度 5/25 入窗 → 0.3 + 0.6 = 0.9
        )
        frame = get_predefined_factor("val_dps_ttm").compute(
            self._inp(decisions)
        )
        assert frame[decisions[0]]["S"] is None
        assert frame[decisions[1]]["S"] == pytest.approx(0.0)
        assert frame[decisions[2]]["S"] == pytest.approx(0.0)
        assert frame[decisions[3]]["S"] == pytest.approx(0.5)
        assert frame[decisions[4]]["S"] == pytest.approx(0.8)
        assert frame[decisions[5]]["S"] == pytest.approx(0.3)
        assert frame[decisions[6]]["S"] == pytest.approx(0.9)

    def test_yield_is_dps_over_visible_close(self) -> None:
        decisions = (date(2024, 9, 26),)
        frame = get_predefined_factor("val_dividend_yield").compute(
            self._inp(decisions, close=10.0)
        )
        assert frame[decisions[0]]["S"] == pytest.approx(0.08)
        frame_high = get_predefined_factor("val_dividend_yield").compute(
            self._inp(decisions, close=40.0)
        )
        assert frame_high[decisions[0]]["S"] == pytest.approx(0.02)

    def test_yield_without_close_is_none(self) -> None:
        decisions = (date(2024, 9, 26),)
        inp = _FakeInput(
            {},
            events={"S": _EVENTS},
            decision_dates=decisions,
            universe=("S",),
        )
        frame = get_predefined_factor("val_dividend_yield").compute(inp)
        assert frame[decisions[0]]["S"] is None

    def test_late_visible_event_respects_pit(self) -> None:
        """迟到公告:除息日已过但进展行尚未可见 → 不计(可见后补进窗口)。"""
        history = _event_history(
            [(date(2024, 5, 30), date(2023, 12, 31), date(2024, 5, 20), 0.5)]
        )
        inp = _FakeInput(
            {},
            events={"S": history},
            decision_dates=(date(2024, 5, 25), date(2024, 5, 31)),
            universe=("S",),
        )
        frame = get_predefined_factor("val_dps_ttm").compute(inp)
        # 5/25:行不可见(公告 5/30)→ None;5/31:可见且 ex 5/20 在窗 → 0.5
        assert frame[date(2024, 5, 25)]["S"] is None
        assert frame[date(2024, 5, 31)]["S"] == pytest.approx(0.5)


# --------------------------------------------------------------------- #
# 质量补全族
# --------------------------------------------------------------------- #


class TestQualityDetailValues:
    def test_advance_receipts_ratio_old_and_new_standards(self) -> None:
        """预收 + 合同负债:新旧准则科目并存,缺项按 0;全缺 → None。"""
        revenue = _one("S", _ann_series([(ANN_A, 400.0)]))

        def build(adv: float | None, contract: float | None) -> Any:
            return _FakeInput(
                {
                    "balance_sheets": {
                        "adv_receipts": _one("S", _ann_series([(ANN_A, adv)])),
                        "contract_liab": _one(
                            "S", _ann_series([(ANN_A, contract)])
                        ),
                    },
                    "income_statements": {"revenue": revenue},
                },
                decision_dates=(date(2024, 5, 8),),
                universe=("S",),
            )

        both = get_predefined_factor("qlt_advance_receipts_ratio").compute(
            build(10.0, 30.0)
        )
        assert both[date(2024, 5, 8)]["S"] == pytest.approx(40.0 / 400.0)
        only_new = get_predefined_factor("qlt_advance_receipts_ratio").compute(
            build(None, 30.0)
        )
        assert only_new[date(2024, 5, 8)]["S"] == pytest.approx(30.0 / 400.0)
        neither = get_predefined_factor("qlt_advance_receipts_ratio").compute(
            build(None, None)
        )
        assert neither[date(2024, 5, 8)]["S"] is None

    def test_turnover_details_and_zero_denominator(self) -> None:
        cost = _one("S", _ann_series([(ANN_A, 300.0)]))
        revenue = _one("S", _ann_series([(ANN_A, 500.0)]))

        def build(field: str, value: float | None) -> Any:
            return _FakeInput(
                {
                    "income_statements": {
                        "oper_cost": cost,
                        "revenue": revenue,
                    },
                    "balance_sheets": {
                        field: _one("S", _ann_series([(ANN_A, value)]))
                    },
                },
                decision_dates=(date(2024, 5, 8),),
                universe=("S",),
            )

        inv = get_predefined_factor("qlt_inventory_turnover_detail").compute(
            build("inventories", 50.0)
        )
        assert inv[date(2024, 5, 8)]["S"] == pytest.approx(6.0)
        inv_zero = get_predefined_factor("qlt_inventory_turnover_detail").compute(
            build("inventories", 0.0)
        )
        assert inv_zero[date(2024, 5, 8)]["S"] is None
        recv = get_predefined_factor("qlt_receivables_turnover_detail").compute(
            build("accounts_receiv", 100.0)
        )
        assert recv[date(2024, 5, 8)]["S"] == pytest.approx(5.0)
        pay = get_predefined_factor("qlt_payables_turnover_detail").compute(
            build("acct_payable", 150.0)
        )
        assert pay[date(2024, 5, 8)]["S"] == pytest.approx(2.0)

    def test_accrual_and_cash_quality(self) -> None:
        decisions = (date(2024, 5, 8),)
        accrual_inp = _FakeInput(
            {
                "cashflow_statements": {
                    "net_profit": _one("S", _ann_series([(ANN_A, 80.0)])),
                    "n_cashflow_act": _one("S", _ann_series([(ANN_A, 100.0)])),
                },
                "balance_sheets": {
                    "total_assets": _one("S", _ann_series([(ANN_A, 1000.0)]))
                },
            },
            decision_dates=decisions,
            universe=("S",),
        )
        frame = get_predefined_factor("qlt_accrual_ratio").compute(accrual_inp)
        assert frame[decisions[0]]["S"] == pytest.approx(-0.02)

        ocf_profit_inp = _FakeInput(
            {
                "cashflow_statements": {
                    "n_cashflow_act": _one("S", _ann_series([(ANN_A, 100.0)])),
                    "net_profit": _one("S", _ann_series([(ANN_A, 80.0)])),
                },
            },
            decision_dates=decisions,
            universe=("S",),
        )
        frame_ocf = get_predefined_factor("qlt_ocf_to_profit").compute(
            ocf_profit_inp
        )
        assert frame_ocf[decisions[0]]["S"] == pytest.approx(1.25)

        # 亏损(net_profit <= 0)→ 缺测,不虚构符号
        loss_inp = _FakeInput(
            {
                "cashflow_statements": {
                    "n_cashflow_act": _one("S", _ann_series([(ANN_A, 100.0)])),
                    "net_profit": _one("S", _ann_series([(ANN_A, -5.0)])),
                },
            },
            decision_dates=decisions,
            universe=("S",),
        )
        frame_loss = get_predefined_factor("qlt_ocf_to_profit").compute(loss_inp)
        assert frame_loss[decisions[0]]["S"] is None

        sales_cash_inp = _FakeInput(
            {
                "cashflow_statements": {
                    "c_fr_sale_sg": _one("S", _ann_series([(ANN_A, 550.0)]))
                },
                "income_statements": {
                    "revenue": _one("S", _ann_series([(ANN_A, 500.0)]))
                },
            },
            decision_dates=decisions,
            universe=("S",),
        )
        frame_sales = get_predefined_factor("qlt_sales_cash_ratio").compute(
            sales_cash_inp
        )
        assert frame_sales[decisions[0]]["S"] == pytest.approx(1.1)

    def test_payout_ratio_eps_guard(self) -> None:
        decisions = (date(2024, 9, 26),)

        def build(eps: float | None) -> Any:
            return _FakeInput(
                {
                    "financial_indicators": {
                        "eps": (
                            {}
                            if eps is None
                            else _one("S", _ann_series([(ANN_A, eps)]))
                        )
                    },
                },
                events={"S": _EVENTS},
                decision_dates=decisions,
                universe=("S",),
            )

        frame = get_predefined_factor("qlt_payout_ratio").compute(build(2.0))
        # dps 0.8 / eps 2.0
        assert frame[decisions[0]]["S"] == pytest.approx(0.4)
        loss = get_predefined_factor("qlt_payout_ratio").compute(build(-0.5))
        assert loss[decisions[0]]["S"] is None
        missing = get_predefined_factor("qlt_payout_ratio").compute(build(None))
        assert missing[decisions[0]]["S"] is None


# --------------------------------------------------------------------- #
# QMJ 支柱与综合(手算锚点)
# --------------------------------------------------------------------- #


class TestQmjPillars:
    def _profitability_input(self) -> _FakeInput:
        def series(values: dict[str, float | None]) -> dict[str, SymbolSeries]:
            return {
                symbol: _ann_series([(ANN_A, value)])
                for symbol, value in values.items()
                if value is not None
            }

        return _FakeInput(
            {
                "financial_indicators": {
                    "return_on_equity": series({"A": 0.10, "B": 0.05}),
                    "return_on_assets": series(
                        {"A": 0.05, "B": 0.06, "C": 0.01}
                    ),
                    "gross_profit_margin": series(
                        {"A": 0.40, "B": 0.20, "C": 0.30}
                    ),
                    "ocf_to_revenue": series({"A": 0.10, "B": 0.10}),
                },
            },
            decision_dates=(date(2024, 5, 8),),
            universe=("A", "B", "C"),
        )

    def test_profitability_pillar_hand_anchor(self) -> None:
        """成分 rank 手算:缺测不入分母;支柱 = 可用成分 rank 均值。"""
        frame = get_predefined_factor("qmj_profitability").compute(
            self._profitability_input()
        )
        cross = frame[date(2024, 5, 8)]
        # A: roe 2/2, roa 2/3, gm 3/3, ocf 2/2 → (1 + 2/3 + 1 + 1)/4
        assert cross["A"] == pytest.approx((1.0 + 2 / 3 + 1.0 + 1.0) / 4.0)
        # B: roe 1/2, roa 3/3, gm 1/3, ocf 2/2 → (0.5 + 1 + 1/3 + 1)/4
        assert cross["B"] == pytest.approx((0.5 + 1.0 + 1 / 3 + 1.0) / 4.0)
        # C: roe/ocf 缺测 → (roa 1/3 + gm 2/3)/2
        assert cross["C"] == pytest.approx((1 / 3 + 2 / 3) / 2.0)

    def test_safety_pillar_inverts_lower_components(self) -> None:
        inp = _FakeInput(
            {
                "financial_indicators": {
                    "debt_to_assets": {
                        s: _ann_series([(ANN_A, v)])
                        for s, v in {"A": 0.8, "B": 0.2, "C": 0.5}.items()
                    },
                    "debt_to_equity": {
                        s: _ann_series([(ANN_A, v)])
                        for s, v in {"A": 2.0, "B": 0.5, "C": 1.0}.items()
                    },
                    "equity_multiplier": {
                        s: _ann_series([(ANN_A, v)])
                        for s, v in {"A": 3.0, "B": 1.5, "C": 2.0}.items()
                    },
                },
            },
            decision_dates=(date(2024, 5, 8),),
            universe=("A", "B", "C"),
        )
        cross = get_predefined_factor("qmj_safety").compute(inp)[
            date(2024, 5, 8)
        ]
        # 各成分 rank:A=1.0/B=1/3/C=2/3,反转 → A=0/B=2/3/C=1/3
        assert cross["A"] == pytest.approx(0.0)
        assert cross["B"] == pytest.approx(2 / 3)
        assert cross["C"] == pytest.approx(1 / 3)

    def test_qmj_composite_mean_of_pillars_with_missing_rule(self) -> None:
        """综合 = 可用支柱均值;缺 payout 数据的标的按三支柱均值;全缺 → 不入截面。"""
        def series(values: dict[str, float | None]) -> dict[str, SymbolSeries]:
            return {
                symbol: _ann_series([(ANN_A, value)])
                for symbol, value in values.items()
                if value is not None
            }

        financial = {
            "return_on_equity": series({"A": 0.10, "B": 0.05, "D": None}),
            "return_on_assets": series({"A": 0.05, "B": 0.06, "D": None}),
            "gross_profit_margin": series({"A": 0.40, "B": 0.20, "D": None}),
            "ocf_to_revenue": series({"A": 0.10, "B": 0.10, "D": None}),
            "revenue_yoy": series({"A": 0.20, "B": -0.10, "D": None}),
            "net_profit_yoy": series({"A": 0.15, "B": -0.05, "D": None}),
            "debt_to_assets": series({"A": 0.30, "B": 0.60, "D": None}),
            "debt_to_equity": series({"A": 0.50, "B": 1.50, "D": None}),
            "equity_multiplier": series({"A": 1.5, "B": 2.5, "D": None}),
            "eps": series({"A": 1.0, "B": 1.0, "D": None}),
        }
        close = {
            "A": _one("A", _daily_series([(ANN_A, 10.0)])),
            "B": _one("B", _daily_series([(ANN_A, 10.0)])),
        }
        # 仅 A 有分红实施(ex 在窗);B/D 无 payout 数据
        events = {
            "A": _event_history(
                [(date(2024, 4, 1), date(2023, 12, 31), date(2024, 4, 20), 0.5)]
            )
        }
        inp = _FakeInput(
            {"financial_indicators": financial, "daily_metrics": close},
            events=events,
            decision_dates=(date(2024, 5, 8),),
            universe=("A", "B", "D"),
        )
        day = date(2024, 5, 8)
        qmj = get_predefined_factor("qmj").compute(inp)[day]
        prof = get_predefined_factor("qmj_profitability").compute(inp)[day]
        growth = get_predefined_factor("qmj_growth").compute(inp)[day]
        safety = get_predefined_factor("qmj_safety").compute(inp)[day]
        payout = get_predefined_factor("qmj_payout").compute(inp)[day]
        # 支柱与综合同源:综合 = 可用支柱均值(B 缺 payout → 三支柱均值)
        prof_a = prof["A"]
        growth_a = growth["A"]
        safety_a = safety["A"]
        payout_a = payout["A"]
        assert prof_a is not None
        assert growth_a is not None
        assert safety_a is not None
        assert payout_a is not None
        assert qmj["A"] == pytest.approx(
            (prof_a + growth_a + safety_a + payout_a) / 4.0
        )
        assert payout.get("B") is None
        prof_b = prof["B"]
        growth_b = growth["B"]
        safety_b = safety["B"]
        assert prof_b is not None
        assert growth_b is not None
        assert safety_b is not None
        assert qmj["B"] == pytest.approx(
            (prof_b + growth_b + safety_b) / 3.0
        )
        # D 无任何数据 → 不入截面
        assert "D" not in qmj

    def test_qmj_excludes_benchmark_only_from_rank_denominator(self) -> None:
        """benchmark-only 标的有数据也不进截面 rank(#380 契约)。"""
        financial = {
            "return_on_equity": {
                "A": _ann_series([(ANN_A, 0.10)]),
                "IDX": _ann_series([(ANN_A, 0.99)]),
            },
            "return_on_assets": {
                "A": _ann_series([(ANN_A, 0.05)]),
                "IDX": _ann_series([(ANN_A, 0.30)]),
            },
            "gross_profit_margin": {
                "A": _ann_series([(ANN_A, 0.40)]),
                "IDX": _ann_series([(ANN_A, 0.90)]),
            },
            "ocf_to_revenue": {
                "A": _ann_series([(ANN_A, 0.10)]),
                "IDX": _ann_series([(ANN_A, 0.50)]),
            },
        }
        inp = _FakeInput(
            {"financial_indicators": financial},
            decision_dates=(date(2024, 5, 8),),
            universe=("A", "IDX"),
            tradable=("A",),
        )
        frame = get_predefined_factor("qmj_profitability").compute(inp)
        cross = frame[date(2024, 5, 8)]
        assert set(cross) == {"A"}
        assert cross["A"] == pytest.approx(1.0)
