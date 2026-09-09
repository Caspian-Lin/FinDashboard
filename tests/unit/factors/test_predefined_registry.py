"""平台预置因子目录逐值与不变量单测(issue #398,批次 0)。

* 目录不变量:命名规则 / 字段完整性 / 唯一性 / commit 锚稳定可复算;
* ``return_Nd`` 样板族:合成行情下 compute 产出与**独立 pandas PIT
  参考**逐值对照(含窗口不足 → None / 停牌缺行 / 基准标的采样);
* ``sample_series_frame`` 采样语义(available_at 门控 / 非有限 → None)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

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
    is_registered_predefined_factor,
    predefined_factor_commit,
    predefined_factor_names,
)

D0 = date(2024, 1, 2)


def _series(
    closes: list[float | None],
    *,
    start: date = date(2023, 1, 2),
    available_hour: int = 15,
) -> SymbolSeries:
    """合成单标的序列:逐日一行,available_at = 当日 available_hour:00 UTC。"""
    dates = tuple(start + timedelta(days=i) for i in range(len(closes)))
    available_at = tuple(
        datetime(d.year, d.month, d.day, available_hour, tzinfo=UTC) for d in dates
    )
    values = np.array(
        [math.nan if v is None else float(v) for v in closes], dtype=np.float64
    )
    return SymbolSeries(dates=dates, values=values, available_at=available_at)


class _FakeInput(PredefinedFactorInput):
    """测试用输入面:显式注入序列与采样面。"""

    def __init__(
        self,
        series_by_symbol: dict[str, SymbolSeries],
        *,
        decision_dates: tuple[date, ...],
        value_universe: tuple[str, ...] | None = None,
        tradable: tuple[str, ...] | None = None,
        benchmark: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(
            factor_name="return_21d",
            decision_dates=decision_dates,
            tradable_symbols=tradable
            or tuple(s for s in series_by_symbol if s not in benchmark),
            benchmark_only_symbols=benchmark,
        )
        self._series = series_by_symbol
        self._universe = value_universe

    def bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        assert field == "close"
        return dict(self._series)

    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        return {}

    def industry_groups(self) -> dict[str, str | None]:
        return {}

    def sample(
        self, per_symbol_values: dict[str, np.ndarray]
    ) -> FactorSeriesFrame:
        return sample_series_frame(
            self._series,
            per_symbol_values,
            decision_dates=self.decision_dates,
            value_universe=self._universe,
        )


class TestCatalogInvariants:
    def test_family_registered_with_parameterized_compute(self) -> None:
        names = predefined_factor_names()
        for window in (21, 63, 126, 252):
            assert f"return_{window}d" in names
        # 同族窗口变体共享同一 compute 工厂的不同闭包实例
        definitions = [get_predefined_factor(f"return_{w}d") for w in (21, 63)]
        assert definitions[0].compute is not definitions[1].compute
        assert definitions[0].data_dependencies == ("bars.close",)
        assert definitions[0].family == "momentum"

    def test_name_rules_and_reserved_prefixes(self) -> None:
        for name in PREDEFINED_FACTORS:
            assert not name.startswith(("p_", "u_"))
            assert name == name.lower()
        from finboard_backtest.factors.predefined.registry import (
            PredefinedFactorDefinition,
        )
        from finboard_data.factor_lab import FactorPreference

        with pytest.raises(ValueError, match="不合规则"):
            PredefinedFactorDefinition(
                name="P_Return",
                title="t",
                family="f",
                direction=FactorPreference.HIGHER,
                signal_eligible=True,
                data_dependencies=("bars.close",),
                window=5,
                implementation_version="1",
                compute=lambda inp: {},
            )
        with pytest.raises(ValueError, match="数据依赖"):
            PredefinedFactorDefinition(
                name="bad_deps",
                title="t",
                family="f",
                direction=FactorPreference.HIGHER,
                signal_eligible=True,
                data_dependencies=(),
                window=5,
                implementation_version="1",
                compute=lambda inp: {},
            )

    def test_commit_anchor_stable_and_semantic_only(self) -> None:
        anchor = predefined_factor_commit("return_21d")
        assert anchor == predefined_factor_commit("return_21d")
        assert anchor.startswith("predefined-")
        assert len(anchor) == len("predefined-") + 12
        # 不同窗口 → 不同锚(窗口进语义字段)
        assert predefined_factor_commit("return_63d") != anchor
        # title 文案变化不影响锚(文档性字段不参与)
        from dataclasses import replace

        from finboard_backtest.factors.predefined.registry import (
            PREDEFINED_FACTORS_SCHEMA_VERSION,
        )

        item = get_predefined_factor("return_21d")
        retitled = replace(item, title="new docstring only")
        assert retitled.title != item.title
        # 语义字段(manual 重算)与锚一致:version 变化必须换锚
        payload = {
            "schema_version": PREDEFINED_FACTORS_SCHEMA_VERSION,
            "name": item.name,
            "data_dependencies": sorted(item.data_dependencies),
            "window": item.window,
            "cross_section": item.cross_section,
            "implementation_version": item.implementation_version,
        }
        import hashlib
        import json

        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert anchor == f"predefined-{digest[:12]}"

    def test_lookup_helpers(self) -> None:
        assert is_registered_predefined_factor("return_21d")
        assert is_registered_predefined_factor("p_return_21d")
        assert not is_registered_predefined_factor("no_such_factor")
        with pytest.raises(KeyError, match="未注册的平台预置因子"):
            get_predefined_factor("no_such_factor")


class TestReturn21dCompute:
    """return_21d 样板因子:compute 产出 vs 独立 pandas PIT 参考逐值对照。"""

    def _build_universe(self) -> dict[str, SymbolSeries]:
        rng = np.random.default_rng(99)
        # 60 根 bar 的价格路径(从 2023-01-02 起逐日)
        prices = 100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.02, size=60))
        return {
            "600000.SH": _series(list(prices)),
            "000001.SZ": _series(list(prices * 0.9), start=date(2023, 1, 3)),
        }

    def test_values_match_independent_pandas_reference(self) -> None:
        universe = self._build_universe()
        decision_dates = tuple(date(2023, 2, 1) + timedelta(days=k) for k in range(5))
        inp = _FakeInput(universe, decision_dates=decision_dates)
        frame = get_predefined_factor("return_21d").compute(inp)
        # 独立参考:每标的 pandas shift(21) 区间收益,按决策日 asof 取值
        for symbol, series in universe.items():
            closes = pd.Series(series.values)
            reference = closes / closes.shift(21) - 1.0
            ref_series = pd.Series(reference.to_numpy(), index=list(series.dates))
            for day in decision_dates:
                expected = ref_series.loc[:day].iloc[-1]
                got = frame[day][symbol]
                if math.isnan(expected):
                    assert got is None
                else:
                    assert got == pytest.approx(float(expected), rel=1e-12)

    def test_window_shortage_yields_none(self) -> None:
        # 25 根 bar 的序列(2023-01-02 起):return_252d 全缺测;return_21d
        # 在窗口凑满后(第 22 根 bar 起)有值,决策日取最近可得 bar。
        universe = {"600000.SH": _series([10.0 + i * 0.1 for i in range(25)])}
        day = date(2023, 2, 1)
        frame = get_predefined_factor("return_252d").compute(
            _FakeInput(universe, decision_dates=(day,))
        )
        assert frame[day]["600000.SH"] is None
        frame21 = get_predefined_factor("return_21d").compute(
            _FakeInput(universe, decision_dates=(day,))
        )
        series = universe["600000.SH"]
        expected = series.values[24] / series.values[3] - 1.0
        assert frame21[day]["600000.SH"] == pytest.approx(float(expected))

    def test_suspended_days_extend_to_last_available_bar(self) -> None:
        """停牌缺行 = 序列行缺失:决策日取最近可得 bar 的因子值。"""
        # 30 根 bar(2022-12-01~12-30),决策日 2023-01-13(跨年无 bar)
        # → 取 12-30 收盘:close[29] / close[8] - 1
        closes = [10.0 + i * 0.1 for i in range(30)]
        series = _series(closes, start=date(2022, 12, 1))
        day = date(2023, 1, 13)
        frame = get_predefined_factor("return_21d").compute(
            _FakeInput({"600000.SH": series}, decision_dates=(day,))
        )
        expected = closes[29] / closes[8] - 1.0
        assert frame[day]["600000.SH"] == pytest.approx(expected)

    def test_benchmark_symbols_absent_from_sampled_frame(self) -> None:
        universe = self._build_universe()
        universe["000300.SH"] = _series([4000.0 + i for i in range(60)])
        decision_dates = (date(2023, 2, 1),)
        # 时序因子:value universe = 全挂载标的(基准在列,消费端剔除)
        ts_input = _FakeInput(
            universe,
            decision_dates=decision_dates,
            tradable=("600000.SH", "000001.SZ"),
            benchmark=frozenset({"000300.SH"}),
        )
        frame = get_predefined_factor("return_21d").compute(ts_input)
        assert "000300.SH" in frame[decision_dates[0]]
        # 截面因子:采样面收窄到可交易域(#380)
        cs_input = _FakeInput(
            universe,
            decision_dates=decision_dates,
            value_universe=("600000.SH", "000001.SZ"),
            tradable=("600000.SH", "000001.SZ"),
            benchmark=frozenset({"000300.SH"}),
        )
        cs_factor = get_predefined_factor("return_21d")
        cs_factor_definition = replace_definition(cs_factor, cross_section=True)
        frame_cs = cs_factor_definition.compute(cs_input)
        assert "000300.SH" not in frame_cs[decision_dates[0]]


def replace_definition(item: object, **changes: object) -> object:
    from dataclasses import replace

    return replace(item, **changes)  # type: ignore[arg-type]


class TestSampleSeriesFrame:
    def test_available_at_gate_and_none_normalisation(self) -> None:
        # 两根 bar:1/2 15:00、1/3 15:00;决策日 1/3 早上(无可见行)由
        # end_of_day 门控决定 —— 采样上界是决策日日终,1/3 bar 可见。
        series = _series([10.0, 11.0], start=date(2024, 1, 2))
        day = date(2024, 1, 3)
        frame = sample_series_frame(
            {"s": series},
            {"s": np.array([1.0, math.inf])},
            decision_dates=(day,),
        )
        # inf 归一为 None(缺测语义)
        assert frame[day]["s"] is None
        # 决策日早于全部 bar → 无可见行 → None
        frame_early = sample_series_frame(
            {"s": series},
            {"s": np.array([1.0, 2.0])},
            decision_dates=(date(2024, 1, 1),),
        )
        assert frame_early[date(2024, 1, 1)]["s"] is None

    def test_length_mismatch_fails_fast(self) -> None:
        series = _series([10.0, 11.0])
        with pytest.raises(ValueError, match=r"长度不一致|不一致"):
            sample_series_frame(
                {"s": series},
                {"s": np.array([1.0])},
                decision_dates=(date(2024, 1, 2),),
            )

    def test_symbolseries_validation(self) -> None:
        with pytest.raises(ValueError, match=r"长度不一致"):
            SymbolSeries(
                dates=(D0,),
                values=np.array([1.0, 2.0]),
                available_at=(datetime(2024, 1, 2, 15, tzinfo=UTC),),
            )
