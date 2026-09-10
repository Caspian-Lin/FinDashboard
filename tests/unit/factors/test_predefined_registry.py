"""平台预置因子目录逐值与不变量单测(issue #398,批次 0)。

* 目录不变量:命名规则 / 字段完整性 / 唯一性 / commit 锚稳定可复算;
* ``return_Nd`` 样板族:合成行情下 compute 产出与**独立 pandas PIT
  参考**逐值对照(含窗口不足 → None / 停牌缺行 / 基准标的采样);
* ``sample_series_frame`` 采样语义(available_at 门控 / 非有限 → None)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
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
    PredefinedFactorDefinition,
    get_predefined_factor,
    is_registered_predefined_factor,
    predefined_factor_commit,
    predefined_factor_names,
)

D0 = date(2024, 1, 2)


def _series(
    closes: Sequence[float | None],
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
        self,
        series_by_symbol: Mapping[str, SymbolSeries],
        per_symbol_values: Mapping[str, np.ndarray],
    ) -> FactorSeriesFrame:
        return sample_series_frame(
            series_by_symbol,
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


def replace_definition(
    item: PredefinedFactorDefinition, **changes: Any
) -> PredefinedFactorDefinition:
    from dataclasses import replace

    return replace(item, **changes)


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


# --------------------------------------------------------------------- #
# Alpha101 批次(issue #400):目录不变量 + 结构全覆盖 + 独立参照逐值
# --------------------------------------------------------------------- #

from finboard_backtest.factors.predefined.registry import (  # noqa: E402
    industry_neutralize,
)
from finboard_data.factor_lab import FactorPreference  # noqa: E402

ALPHA101_NAMES = (
    "alpha101_1", "alpha101_2", "alpha101_3", "alpha101_4", "alpha101_5",
    "alpha101_6", "alpha101_7", "alpha101_8", "alpha101_9", "alpha101_10",
    "alpha101_11", "alpha101_12", "alpha101_13", "alpha101_14", "alpha101_15",
    "alpha101_16", "alpha101_17", "alpha101_18", "alpha101_19", "alpha101_20",
    "alpha101_22", "alpha101_23", "alpha101_25", "alpha101_33", "alpha101_34",
    "alpha101_41", "alpha101_52", "alpha101_53", "alpha101_54", "alpha101_57",
    "alpha101_101",
)
ALPHA101_CROSS_SECTION_TRUE = frozenset(
    {
        "alpha101_1", "alpha101_2", "alpha101_3", "alpha101_4", "alpha101_5",
        "alpha101_8", "alpha101_10", "alpha101_11", "alpha101_13",
        "alpha101_14", "alpha101_15", "alpha101_16", "alpha101_17",
        "alpha101_18", "alpha101_19", "alpha101_20", "alpha101_22",
        "alpha101_25", "alpha101_33", "alpha101_34", "alpha101_52",
        "alpha101_57",
    }
)

ALPHA101_FIELDS = ("open", "high", "low", "close", "volume", "amount")


class _AlphaInput(PredefinedFactorInput):
    """Alpha101 测试输入面:多字段序列 + 可选行业分组/基准。"""

    def __init__(
        self,
        fields: dict[str, dict[str, SymbolSeries]],
        *,
        decision_dates: tuple[date, ...],
        tradable: tuple[str, ...],
        benchmark: frozenset[str] = frozenset(),
        industry: dict[str, str | None] | None = None,
        value_universe: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(
            factor_name="alpha101_1",
            decision_dates=decision_dates,
            tradable_symbols=tradable,
            benchmark_only_symbols=benchmark,
        )
        self._fields = fields
        self._industry = industry or {}
        self._universe = value_universe

    def bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        return dict(self._fields[field])

    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        return {}

    def industry_groups(self) -> dict[str, str | None]:
        return dict(self._industry)

    def sample(
        self,
        series_by_symbol: Mapping[str, SymbolSeries],
        per_symbol_values: Mapping[str, np.ndarray],
    ) -> FactorSeriesFrame:
        return sample_series_frame(
            series_by_symbol,
            per_symbol_values,
            decision_dates=self.decision_dates,
            value_universe=self._universe,
        )


def _alpha_universe(
    *,
    n_days: int = 260,
    n_symbols: int = 4,
    seed: int = 11,
) -> tuple[dict[str, dict[str, SymbolSeries]], tuple[date, ...]]:
    """确定性多标的 OHLCV 合成(有市场共同因子,量价相关非退化)。"""
    rng = np.random.default_rng(seed)
    dates = tuple(date(2022, 6, 1) + timedelta(days=i) for i in range(n_days))
    fields: dict[str, dict[str, SymbolSeries]] = {f: {} for f in ALPHA101_FIELDS}
    market = rng.normal(0.0003, 0.01, size=n_days)
    for k in range(n_symbols):
        beta = 0.6 + 0.3 * k
        ret = market * beta + rng.normal(0.0002, 0.012, size=n_days)
        close = 50.0 * np.cumprod(1.0 + ret)
        open_ = close * (1.0 + rng.normal(0.0, 0.004, size=n_days))
        high = np.maximum(open_, close) * (
            1.0 + np.abs(rng.normal(0.0, 0.006, size=n_days))
        )
        low = np.minimum(open_, close) * (
            1.0 - np.abs(rng.normal(0.0, 0.006, size=n_days))
        )
        volume = 1_000_000.0 * np.exp(rng.normal(0.0, 0.3, size=n_days))
        typical = (high + low + close) / 3.0
        amount = typical * volume
        for field, values in (
            ("open", open_),
            ("high", high),
            ("low", low),
            ("close", close),
            ("volume", volume),
            ("amount", amount),
        ):
            fields[field][f"S{k}"] = _series(list(values), start=dates[0])
    return fields, dates


def _pd_field(fields: dict[str, dict[str, SymbolSeries]], field: str) -> pd.DataFrame:
    """单字段 → DataFrame(index=dates, columns=symbols)(独立参照轴)。"""
    return pd.DataFrame(
        {
            symbol: pd.Series(
                np.asarray(series.values, dtype=np.float64),
                index=pd.Index(list(series.dates)),
            )
            for symbol, series in fields[field].items()
        }
    )


def _cs_rank_ref(frame: pd.DataFrame) -> pd.DataFrame:
    """截面百分位参照:pct rank method='max' = (# <= x)/n(与 cs_rank 同口径)。"""
    return frame.rank(axis=1, pct=True, method="max")


def _ts_rank_pct(arr: np.ndarray) -> float:
    if np.isnan(arr).any():
        return math.nan
    return float((arr <= arr[-1]).sum()) / float(arr.size)


def _assert_matches_reference(
    name: str,
    fields: dict[str, dict[str, SymbolSeries]],
    decision_dates: tuple[date, ...],
    reference: pd.DataFrame,
    *,
    tradable: tuple[str, ...] | None = None,
) -> None:
    item = get_predefined_factor(name)
    universe = tradable or tuple(str(c) for c in reference.columns)
    inp = _AlphaInput(
        fields, decision_dates=decision_dates, tradable=universe, value_universe=universe
    )
    frame = item.compute(inp)
    for day in decision_dates:
        for symbol in universe:
            expected = reference[symbol].loc[:day].iloc[-1]
            got = frame[day][symbol]
            if pd.isna(expected):
                assert got is None, (name, symbol, day, expected, got)
            else:
                assert got is not None, (name, symbol, day)
                assert got == pytest.approx(
                    float(expected), rel=1e-9, abs=1e-12
                ), (name, symbol, day, expected, got)


class TestAlpha101Catalog:
    def test_batch_exactly_31_registered(self) -> None:
        names = set(predefined_factor_names())
        assert set(ALPHA101_NAMES) <= names
        assert {n for n in names if n.startswith("alpha101_")} == set(ALPHA101_NAMES)
        assert len(ALPHA101_NAMES) == 31

    def test_entries_share_batch_contract(self) -> None:
        for name in ALPHA101_NAMES:
            item = get_predefined_factor(name)
            assert item.family == "alpha101", name
            assert item.direction is FactorPreference.HIGHER, name
            assert item.signal_eligible is True, name
            assert item.implementation_version == "1", name
            assert item.compute is not None, name
            assert item.title.startswith(f"Alpha101#{name.removeprefix('alpha101_')}"), name
            for dep in item.data_dependencies:
                dataset, _, _field = dep.partition(".")
                assert dataset == "bars", name
                assert _field in ALPHA101_FIELDS, name

    def test_cross_section_flags_follow_formula(self) -> None:
        for name in ALPHA101_NAMES:
            item = get_predefined_factor(name)
            expected = name in ALPHA101_CROSS_SECTION_TRUE
            assert item.cross_section is expected, name

    def test_commit_anchors_all_distinct(self) -> None:
        anchors = {predefined_factor_commit(name) for name in ALPHA101_NAMES}
        assert len(anchors) == 31


class TestAlpha101Structure:
    def test_all_31_factors_cover_full_decision_frame(self) -> None:
        """31 因子全覆盖:合成截面小样本上产出契约完整(决策日齐备、值域合法)。"""
        fields, dates = _alpha_universe()
        decision_dates = (dates[259], dates[255], dates[130])
        for name in ALPHA101_NAMES:
            item = get_predefined_factor(name)
            inp = _AlphaInput(
                fields,
                decision_dates=decision_dates,
                tradable=("S0", "S1", "S2", "S3"),
            )
            frame = item.compute(inp)
            assert set(frame) == set(decision_dates), name
            for day in decision_dates:
                cross = frame[day]
                assert set(cross) == {"S0", "S1", "S2", "S3"}, name
                for value in cross.values():
                    assert value is None or (
                        isinstance(value, float) and math.isfinite(value)
                    ), (name, day, value)

    def test_alpha19_long_window_shortage_yields_none(self) -> None:
        """长窗口(250 日)不足 → 缺测而非报错(宁缺毋假)。"""
        fields, dates = _alpha_universe(n_days=120, seed=5)
        day = dates[119]
        frame = get_predefined_factor("alpha101_19").compute(
            _AlphaInput(
                fields, decision_dates=(day,), tradable=("S0", "S1", "S2", "S3")
            )
        )
        assert all(value is None for value in frame[day].values())

    def test_rank_denominator_excludes_benchmark(self) -> None:
        """#380 契约:Rank 类 Alpha101 的截面分母不混入 benchmark-only。"""
        fields, dates = _alpha_universe(seed=23)
        day = dates[200]
        # 加入指数(全字段):若进截面分母,排名必变(其值落在截面中段)
        for field in ALPHA101_FIELDS:
            base = np.asarray(fields[field]["S0"].values, dtype=np.float64).copy()
            scale = 1.003 if field == "close" else 1.0
            fields[field]["IDX"] = _series(list(base * scale), start=dates[0])
        name = "alpha101_33"
        with_idx = get_predefined_factor(name).compute(
            _AlphaInput(
                fields,
                decision_dates=(day,),
                tradable=("S0", "S1", "S2", "S3"),
                benchmark=frozenset({"IDX"}),
                value_universe=("S0", "S1", "S2", "S3"),
            )
        )
        assert "IDX" not in with_idx[day]
        fields_without = {
            field: {s: sr for s, sr in series.items() if s != "IDX"}
            for field, series in fields.items()
        }
        without_idx = get_predefined_factor(name).compute(
            _AlphaInput(
                fields_without,
                decision_dates=(day,),
                tradable=("S0", "S1", "S2", "S3"),
            )
        )
        for symbol in ("S0", "S1", "S2", "S3"):
            assert with_idx[day][symbol] == without_idx[day][symbol], symbol


class TestAlpha101HandComputed:
    """小网格字面手算锚点(公式字面语义,不依赖 pandas 参照)。"""

    def _grid(self) -> tuple[dict[str, dict[str, SymbolSeries]], tuple[date, ...]]:
        """3 标的 x 8 根 bar 的手工 OHLCV 网格。"""
        dates = tuple(date(2024, 3, 1) + timedelta(days=i) for i in range(8))
        open_ = {
            "A": [10.0, 10.5, 11.0, 10.8, 11.2, 11.0, 11.5, 11.8],
            "B": [20.0, 19.5, 19.0, 19.8, 20.2, 20.5, 20.3, 20.8],
            "C": [5.0, 5.2, 5.1, 5.4, 5.3, 5.6, 5.8, 5.7],
        }
        close = {
            "A": [10.5, 11.0, 10.8, 11.2, 11.0, 11.5, 11.8, 12.0],
            "B": [19.5, 19.0, 19.8, 20.2, 20.5, 20.3, 20.8, 21.0],
            "C": [5.2, 5.1, 5.4, 5.3, 5.6, 5.8, 5.7, 5.9],
        }
        high = {k: [v + 0.2 for v in row] for k, row in close.items()}
        low = {k: [v - 0.2 for v in row] for k, row in close.items()}
        volume = {
            "A": [100.0, 110.0, 90.0, 120.0, 80.0, 130.0, 140.0, 150.0],
            "B": [200.0, 190.0, 210.0, 180.0, 220.0, 170.0, 160.0, 150.0],
            "C": [50.0, 55.0, 45.0, 60.0, 40.0, 65.0, 70.0, 75.0],
        }
        amount = {
            k: [
                (close[k][i] + open_[k][i]) / 2.0 * volume[k][i]
                for i in range(8)
            ]
            for k in close
        }
        fields: dict[str, dict[str, SymbolSeries]] = {}
        for field, rows in (
            ("open", open_),
            ("high", high),
            ("low", low),
            ("close", close),
            ("volume", volume),
            ("amount", amount),
        ):
            fields[field] = {
                symbol: _series(values, start=dates[0])
                for symbol, values in rows.items()
            }
        return fields, dates

    def test_alpha101_101_literal(self) -> None:
        fields, dates = self._grid()
        day = dates[7]
        frame = get_predefined_factor("alpha101_101").compute(
            _AlphaInput(fields, decision_dates=(day,), tradable=("A", "B", "C"))
        )
        expected = {
            "A": (12.0 - 11.8) / ((12.2 - 11.8) + 0.001),
            "B": (21.0 - 20.8) / ((21.2 - 20.8) + 0.001),
            "C": (5.9 - 5.7) / ((6.1 - 5.7) + 0.001),
        }
        for symbol, value in expected.items():
            assert frame[day][symbol] == pytest.approx(value, rel=1e-12)

    def test_alpha101_12_literal(self) -> None:
        fields, dates = self._grid()
        day = dates[7]
        frame = get_predefined_factor("alpha101_12").compute(
            _AlphaInput(fields, decision_dates=(day,), tradable=("A", "B", "C"))
        )
        # Sign(Delta(Volume,1)) * (-Delta(Close,1)):A/B/C 末日量升、价升
        assert frame[day]["A"] == pytest.approx(1.0 * -(12.0 - 11.8))
        assert frame[day]["B"] == pytest.approx(-1.0 * -(21.0 - 20.8))
        assert frame[day]["C"] == pytest.approx(1.0 * -(5.9 - 5.7))

    def test_alpha101_41_vwap_approximation(self) -> None:
        """VWAP = amount/volume 逐值近似 + volume=0 → 缺测。"""
        fields, dates = self._grid()
        day = dates[7]
        frame = get_predefined_factor("alpha101_41").compute(
            _AlphaInput(fields, decision_dates=(day,), tradable=("A", "B", "C"))
        )
        vwap_a = ((12.0 + 11.8) / 2.0 * 150.0) / 150.0
        expected_a = (12.2 * 11.8) ** 0.5 - vwap_a
        assert frame[day]["A"] == pytest.approx(expected_a, rel=1e-12)
        zero_volume = np.asarray(fields["volume"]["A"].values).copy()
        zero_volume[-1] = 0.0
        fields["volume"]["A"] = _series(list(zero_volume), start=dates[0])
        frame_zero = get_predefined_factor("alpha101_41").compute(
            _AlphaInput(fields, decision_dates=(day,), tradable=("A", "B", "C"))
        )
        assert frame_zero[day]["A"] is None

    def test_alpha101_33_rank_hand_computed(self) -> None:
        fields, dates = self._grid()
        day = dates[7]
        frame = get_predefined_factor("alpha101_33").compute(
            _AlphaInput(fields, decision_dates=(day,), tradable=("A", "B", "C"))
        )
        raw = {
            "A": -(1.0 - 11.8 / 12.0),
            "B": -(1.0 - 20.8 / 21.0),
            "C": -(1.0 - 5.7 / 5.9),
        }
        ordered = sorted(raw, key=lambda s: raw[s])
        ranks = {s: (i + 1) / 3.0 for i, s in enumerate(ordered)}
        for symbol, value in ranks.items():
            assert frame[day][symbol] == pytest.approx(value), symbol

    def test_alpha101_7_conditional_branches(self) -> None:
        """alpha7 两分支:放量日走 body,不放量日取 -1;分支值手算锚定。"""
        # 70 根 bar:ts_rank(|delta7|, 60) 在末行需要 [10..69] 无 NaN
        # (delta7 前 7 行 NaN),故序列须 >= 7 + 60 根。
        n = 70
        start = date(2024, 3, 1)
        dates = tuple(start + timedelta(days=i) for i in range(n))
        close = [10.0 + 0.01 * i for i in range(n)]
        volume = [100.0] * (n - 1) + [200.0]  # 末日放量
        fields = {
            "close": {"A": _series(close, start=start)},
            "volume": {"A": _series(volume, start=start)},
        }
        day = dates[-1]
        frame = get_predefined_factor("alpha101_7").compute(
            _AlphaInput(fields, decision_dates=(day,), tradable=("A",))
        )
        # 放量分支:volume=200 > adv20=100 → -ts_rank(|delta7|,60) * sign(delta7)
        arr = np.array(close)
        delta7 = np.full(n, np.nan)
        delta7[7:] = np.abs(arr[7:] - arr[:-7])
        last60 = delta7[n - 60 :]  # 末行 60 窗 = 行 [10..69],全部有效
        assert not np.isnan(last60).any()
        rank_val = (last60 <= last60[-1]).sum() / 60.0
        d7 = arr[-1] - arr[-8]
        sign_val = 1.0 if d7 > 0 else (-1.0 if d7 < 0 else 0.0)
        assert frame[day]["A"] == pytest.approx(-rank_val * sign_val, rel=1e-12)
        # 不放量日:volume=100 不 > adv20=100 → -1
        day_before = dates[-2]
        frame2 = get_predefined_factor("alpha101_7").compute(
            _AlphaInput(fields, decision_dates=(day_before,), tradable=("A",))
        )
        assert frame2[day_before]["A"] == pytest.approx(-1.0)


class TestAlpha101ReferenceEquivalence:
    """独立 pandas 参照逐值对照(公式按论文/tushare 文本另写,不复用算子)。"""

    def _decision_dates(self, dates: tuple[date, ...]) -> tuple[date, ...]:
        return (dates[259], dates[255], dates[252])

    def test_alpha001(self) -> None:
        fields, dates = _alpha_universe()
        c = _pd_field(fields, "close")
        ret = c / c.shift(1) - 1.0
        std20 = ret.rolling(20).std(ddof=1)
        cond = ret < 0
        # 论文/tushare:IF(returns < 0, stddev(returns, 20), close) —— 真
        # 分支 = std20(pandas where 保留 cond 为 True 的值)。#400 收尾审计
        # 修正:初版参照与实现同为 (ret<0)→close 的颠倒读法,互相印证不出。
        inner = std20.where(cond, c)
        inner = inner.where(cond.notna(), np.nan)
        sp = inner * inner.abs()
        arg = sp.rolling(5).apply(
            lambda a: math.nan if np.isnan(a).any() else float(np.argmax(a[::-1])),
            raw=True,
        )
        reference = _cs_rank_ref(arg) - 0.5
        _assert_matches_reference(
            "alpha101_1", fields, self._decision_dates(dates), reference
        )

    def test_alpha002(self) -> None:
        fields, dates = _alpha_universe()
        c, o, v = (
            _pd_field(fields, "close"),
            _pd_field(fields, "open"),
            _pd_field(fields, "volume"),
        )
        log_v = pd.DataFrame(np.log(v.to_numpy()), index=v.index, columns=v.columns)
        left = _cs_rank_ref(log_v.diff(2))
        right = _cs_rank_ref((c - o) / o)
        reference = -left.rolling(6).corr(right)
        _assert_matches_reference(
            "alpha101_2", fields, self._decision_dates(dates), reference
        )

    def test_alpha004(self) -> None:
        fields, dates = _alpha_universe()
        low = _pd_field(fields, "low")
        reference = -_cs_rank_ref(low).rolling(9).apply(_ts_rank_pct, raw=True)
        _assert_matches_reference(
            "alpha101_4", fields, self._decision_dates(dates), reference
        )

    def test_alpha005(self) -> None:
        fields, dates = _alpha_universe()
        c, o = _pd_field(fields, "close"), _pd_field(fields, "open")
        vwap = _pd_field(fields, "amount") / _pd_field(fields, "volume")
        left = _cs_rank_ref(o - vwap.rolling(10).mean())
        right = _cs_rank_ref(c - vwap).abs()
        reference = left * -right
        _assert_matches_reference(
            "alpha101_5", fields, self._decision_dates(dates), reference
        )

    def test_alpha007(self) -> None:
        fields, dates = _alpha_universe()
        c, v = _pd_field(fields, "close"), _pd_field(fields, "volume")
        adv = v.rolling(20).mean()
        d7 = c.diff(7)
        body = -d7.abs().rolling(60).apply(_ts_rank_pct, raw=True) * np.sign(d7)
        cond = v > adv
        reference = body.where(cond, -1.0).where(cond.notna(), np.nan)
        _assert_matches_reference(
            "alpha101_7", fields, self._decision_dates(dates), reference
        )

    def test_alpha009(self) -> None:
        fields, dates = _alpha_universe()
        c = _pd_field(fields, "close")
        d1 = c.diff(1)
        mn, mx = d1.rolling(5).min(), d1.rolling(5).max()
        inner = d1.where(mx < 0, -d1).where((mx < 0).notna(), np.nan)
        reference = d1.where(mn > 0, inner).where((mn > 0).notna(), np.nan)
        _assert_matches_reference(
            "alpha101_9", fields, self._decision_dates(dates), reference
        )

    def test_alpha011(self) -> None:
        fields, dates = _alpha_universe()
        c, v = _pd_field(fields, "close"), _pd_field(fields, "volume")
        vwap = _pd_field(fields, "amount") / _pd_field(fields, "volume")
        vc = vwap - c
        reference = (
            _cs_rank_ref(vc.rolling(3).max()) + _cs_rank_ref(vc.rolling(3).min())
        ) * _cs_rank_ref(v.diff(3))
        _assert_matches_reference(
            "alpha101_11", fields, self._decision_dates(dates), reference
        )

    def test_alpha013(self) -> None:
        # seed 31:rank 序列的平台化使 rolling cov 在个别 (symbol, day) 取
        # ±1e-16 的数值零(两套浮点累加顺序的最后一位差),外层截面排名对
        # 这种噪声敏感;seed 31 的对照窗口无此退化。
        fields, dates = _alpha_universe(seed=31)
        c, v = _pd_field(fields, "close"), _pd_field(fields, "volume")
        reference = -_cs_rank_ref(_cs_rank_ref(c).rolling(5).cov(_cs_rank_ref(v)))
        _assert_matches_reference(
            "alpha101_13", fields, self._decision_dates(dates), reference
        )

    def test_alpha019(self) -> None:
        fields, dates = _alpha_universe()
        c, v = _pd_field(fields, "close"), _pd_field(fields, "volume")
        ret = c / c.shift(1) - 1.0
        vwap = _pd_field(fields, "amount") / _pd_field(fields, "volume")
        adv = v.rolling(20).mean()
        sign_frame = pd.DataFrame(
            np.sign(((c - c.shift(7)) + c.diff(7)).to_numpy()),
            index=c.index,
            columns=c.columns,
        )
        t1 = -sign_frame * (1.0 + _cs_rank_ref(1.0 + ret.rolling(250).sum()))
        a_corr = _cs_rank_ref(vwap - c).rolling(12).corr(_cs_rank_ref(v))
        b_corr = _cs_rank_ref(c).rolling(12).corr(_cs_rank_ref(adv))
        # tushare 文本:X = Corr1 * Rank(Corr2) —— 第二个 Correlation 外层
        # 有 Rank(...)(#400 收尾审计修正,与「各自 Rank 后相乘」的论文
        # 写法不同)。
        a_rank = _cs_rank_ref(a_corr * _cs_rank_ref(b_corr))
        reference = t1 + (-a_rank * a_rank)
        # 250 日长窗:只取窗口末端决策日
        _assert_matches_reference("alpha101_19", fields, (dates[259],), reference)

    def test_alpha020(self) -> None:
        fields, dates = _alpha_universe()
        c, o = _pd_field(fields, "close"), _pd_field(fields, "open")
        h, low = _pd_field(fields, "high"), _pd_field(fields, "low")
        reference = (
            -_cs_rank_ref(o - h.shift(1))
            * _cs_rank_ref(o - c.shift(1))
            * _cs_rank_ref(o - low.shift(1))
        )
        _assert_matches_reference(
            "alpha101_20", fields, self._decision_dates(dates), reference
        )

    def test_alpha023(self) -> None:
        fields, dates = _alpha_universe()
        h = _pd_field(fields, "high")
        mean20 = h.rolling(20).mean()
        cond = mean20 < h
        reference = pd.DataFrame(
            np.where(cond, -h.diff(2), 0.0), index=h.index, columns=h.columns
        ).where(cond.notna(), np.nan)
        _assert_matches_reference(
            "alpha101_23", fields, self._decision_dates(dates), reference
        )

    def test_alpha025(self) -> None:
        fields, dates = _alpha_universe()
        c, h = _pd_field(fields, "close"), _pd_field(fields, "high")
        v = _pd_field(fields, "volume")
        ret = c / c.shift(1) - 1.0
        adv = v.rolling(20).mean()
        vwap = _pd_field(fields, "amount") / _pd_field(fields, "volume")
        reference = _cs_rank_ref(-ret * adv * vwap * (h - c))
        _assert_matches_reference(
            "alpha101_25", fields, self._decision_dates(dates), reference
        )

    def test_alpha034(self) -> None:
        fields, dates = _alpha_universe()
        c = _pd_field(fields, "close")
        ret = c / c.shift(1) - 1.0
        ratio = ret.rolling(2).std(ddof=1) / ret.rolling(5).std(ddof=1)
        reference = _cs_rank_ref(
            (1.0 - _cs_rank_ref(ratio)) + (1.0 - _cs_rank_ref(c.diff(1)))
        )
        _assert_matches_reference(
            "alpha101_34", fields, self._decision_dates(dates), reference
        )

    def test_alpha052(self) -> None:
        fields, dates = _alpha_universe()
        c = _pd_field(fields, "close")
        low, v = _pd_field(fields, "low"), _pd_field(fields, "volume")
        ret = c / c.shift(1) - 1.0
        mn5 = low.rolling(5).min()
        head = -mn5 + mn5.shift(5)
        spread = (ret.rolling(240).sum() - ret.rolling(20).sum()) / 220.0
        reference = (head * _cs_rank_ref(spread)) * v.rolling(5).apply(
            _ts_rank_pct, raw=True
        )
        # 240 日长窗:只取窗口末端决策日
        _assert_matches_reference("alpha101_52", fields, (dates[259],), reference)

    def test_alpha057(self) -> None:
        fields, dates = _alpha_universe()
        c = _pd_field(fields, "close")
        vwap = _pd_field(fields, "amount") / _pd_field(fields, "volume")
        arg = c.rolling(30).apply(
            lambda a: math.nan if np.isnan(a).any() else float(np.argmax(a[::-1])),
            raw=True,
        )
        denom = _cs_rank_ref(arg).rolling(2).apply(
            lambda a: (
                math.nan
                if np.isnan(a).any()
                else (a[0] * 1.0 + a[1] * 2.0) / 3.0
            ),
            raw=True,
        )
        reference = (vwap - c) / denom
        _assert_matches_reference(
            "alpha101_57", fields, self._decision_dates(dates), reference
        )


class TestIndustryNeutralize:
    """industry_neutralize(#400 口径):组内去均值 + 缺组缺测可计数。"""

    def test_group_demean_hand_computed(self) -> None:
        values: dict[str, float | None] = {
            "A": 10.0,
            "B": 20.0,
            "C": 30.0,
            "D": None,
        }
        groups = {"A": "bank", "B": "bank", "C": "tech", "D": "tech"}
        out, missing = industry_neutralize(values, groups)
        assert out["A"] == pytest.approx(-5.0)
        assert out["B"] == pytest.approx(5.0)
        assert out["C"] == pytest.approx(0.0)  # 单标的组去均值 = 0
        assert out["D"] is None  # 值缺测保持缺测
        assert missing == 0  # 缺值不计入缺组

    def test_missing_group_outputs_none_and_counted(self) -> None:
        values: dict[str, float | None] = {"A": 1.0, "B": 3.0, "X": 5.0}
        groups: dict[str, str | None] = {"A": "bank", "B": "bank", "X": None}
        out, missing = industry_neutralize(values, groups)
        assert out["X"] is None  # 缺组 → 缺测(不静默归组)
        assert out["A"] == pytest.approx(-1.0)
        assert out["B"] == pytest.approx(1.0)
        assert missing == 1

    def test_all_groups_missing_degrades_to_all_none(self) -> None:
        out, missing = industry_neutralize({"A": 1.0, "B": 2.0}, {})
        assert out == {"A": None, "B": None}
        assert missing == 2
