"""因子批次 6 P2 第一半(#429):长窗市场回归族 + 规模非线性的目录与逐值手锚。

* 目录不变量(批次 scope,不做全局总数断言):14 个新因子的 family /
  direction / signal_eligible / cross_section / window / min_history_bars /
  data_dependencies 逐项精确,commit 锚互异;
* **逐值手锚** —— 合成 close 序列上:
  ``alpha`` / ``beta`` = 「日收益 = 2 x 指数日收益」(再加 +0.001 恒定超额)
  的 trailing OLS 截距 / 斜率;``sigma`` = 市场模型残差标准差(残差恒 0 →
  0;周期 4 残差与市场正交 → 手算 ``c x sqrt(660 / 1319)``);
  ``beta_consistency`` = 同窗 ``|beta| x std(残差)``;``volume_alpha`` /
  ``volume_beta`` = 周期 2 成交量模式下的闭式 OLS(两个不同相位点 → 回归
  直线精确穿过两点);``nl_size`` = 4 点截面闭式 cubic 残差(手算到小数位);
* 边界:发布不含基准指数 / 历史不足 / ``nl_size`` 有限点 < 3 或截面方差 0
  一律 fail-visible 缺测(None),不 fallback 到别的指数。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
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

HS300 = "000300.SH"
SSE = "000001.SH"

#: 批次 6 P2 第一半注册的 14 个裸名(与注册清单逐名一致)。
BATCH6_MARKET_NAMES = frozenset(
    {
        "alpha_500d_000300",
        "alpha_528d_000001",
        "alpha_792d_000001",
        "alpha_1000d_000300",
        "alpha_1320d_000001",
        "beta_500d_000300",
        "beta_1000d_000300",
        "sigma_1320d_000001",
        "sigma_1320d_000300",
        "beta_consistency_1320d_000300",
        "volume_alpha_300d_000001",
        "volume_alpha_300d_000300",
        "volume_beta_120d_000300",
        "nl_size",
    }
)

_CLOSE_DEPS = ("bars.close", "index_bars.close")
_VOLUME_DEPS = ("bars.volume", "index_bars.volume")

#: name -> (family, direction, signal_eligible, cross_section, window,
#:          min_history_bars, data_dependencies)
BATCH6_MARKET_SPECS: dict[
    str, tuple[str, str, bool, bool, int | None, int | None, tuple[str, ...]]
] = {
    "alpha_500d_000300": ("momentum", "higher", True, False, 500, 500, _CLOSE_DEPS),
    "alpha_528d_000001": ("momentum", "higher", True, False, 528, 528, _CLOSE_DEPS),
    "alpha_792d_000001": ("momentum", "higher", True, False, 792, 792, _CLOSE_DEPS),
    "alpha_1000d_000300": ("momentum", "higher", True, False, 1000, 1000, _CLOSE_DEPS),
    "alpha_1320d_000001": ("momentum", "higher", True, False, 1320, 1320, _CLOSE_DEPS),
    "beta_500d_000300": ("risk", "lower", False, False, 500, 500, _CLOSE_DEPS),
    "beta_1000d_000300": ("risk", "lower", False, False, 1000, 1000, _CLOSE_DEPS),
    "sigma_1320d_000001": ("risk", "lower", False, False, 1320, 1320, _CLOSE_DEPS),
    "sigma_1320d_000300": ("risk", "lower", False, False, 1320, 1320, _CLOSE_DEPS),
    "beta_consistency_1320d_000300": (
        "risk",
        "lower",
        False,
        False,
        1320,
        1320,
        _CLOSE_DEPS,
    ),
    "volume_alpha_300d_000001": (
        "liquidity",
        "higher",
        False,
        False,
        300,
        306,
        _VOLUME_DEPS,
    ),
    "volume_alpha_300d_000300": (
        "liquidity",
        "higher",
        False,
        False,
        300,
        306,
        _VOLUME_DEPS,
    ),
    "volume_beta_120d_000300": (
        "liquidity",
        "lower",
        False,
        False,
        120,
        126,
        _VOLUME_DEPS,
    ),
    "nl_size": (
        "size",
        "lower",
        False,
        True,
        None,
        None,
        ("daily_metrics.total_market_cap",),
    ),
}

#: 合成宇宙长度:1320d 长窗 + 尾部若干决策日。
_N_BARS = 1336
_DATES = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(_N_BARS))

#: 沪深300 合成日收益幅度(周期 2:偶数指数日 +A,奇数日 -A)。
_HS300_AMP = 0.01
#: 上证指数合成日收益模式(周期 3,与沪深300 非共线 → 可鉴别基准映射)。
_SSE_PATTERN = (0.01, -0.02, 0.005)
#: 残差幅度:周期 4 的 +c / 0 / -c / 0,与周期 2 的市场正交(同窗内和与
#: 与市场的内积均为 0),故该形态下「OLS 残差 == 构造残差」精确成立。
_RESID_C = 0.002


def _hs300_returns() -> np.ndarray:
    idx = np.arange(_N_BARS)
    rets = np.zeros(_N_BARS)
    rets[1:] = np.where(idx[1:] % 2 == 1, _HS300_AMP, -_HS300_AMP)
    return rets


def _sse_returns() -> np.ndarray:
    idx = np.arange(_N_BARS)
    pattern = np.array(_SSE_PATTERN, dtype=np.float64)
    rets = pattern[idx % 3]
    rets[0] = 0.0
    return rets


def _residual_returns() -> np.ndarray:
    idx = np.arange(_N_BARS)
    rets = np.zeros(_N_BARS)
    rets[idx % 4 == 1] = _RESID_C
    rets[idx % 4 == 3] = -_RESID_C
    return rets


def _close_from_returns(rets: np.ndarray, base: float = 100.0) -> np.ndarray:
    """日收益序列 → 收盘价路径(首日 = base,第 i 日 = 前收 x (1 + ret_i))。"""
    out = np.empty(rets.size, dtype=np.float64)
    out[0] = base
    for position in range(1, rets.size):
        out[position] = out[position - 1] * (1.0 + rets[position])
    return out


def _series(
    values: np.ndarray, *, dates: tuple[date, ...] | None = None
) -> SymbolSeries:
    """1-D 值数组 → SymbolSeries(available_at = 当日 15:00 UTC,决策日日终可见)。"""
    array = np.asarray(values, dtype=np.float64)
    days = _DATES[: array.size] if dates is None else dates[: array.size]
    return SymbolSeries(
        dates=days,
        values=array,
        available_at=tuple(
            datetime(d.year, d.month, d.day, 15, tzinfo=UTC) for d in days
        ),
    )


class _Batch6Input(PredefinedFactorInput):
    """批次 6 测试输入面:bars / daily_metrics 显式注入。

    ``sample`` 复刻引擎口径:``cross_section=True`` 的因子采样面收窄到
    可交易域(#380,benchmark-only 不进截面),时序因子保留全挂载标的。
    """

    def __init__(
        self,
        bars_by_field: Mapping[str, Mapping[str, SymbolSeries]],
        *,
        decision_dates: tuple[date, ...],
        daily_by_field: Mapping[str, Mapping[str, SymbolSeries]] | None = None,
        benchmark: frozenset[str] = frozenset({HS300, SSE}),
        cross_section: bool = False,
    ) -> None:
        super().__init__(
            factor_name="batch6_test",
            decision_dates=decision_dates,
            tradable_symbols=tuple(
                symbol
                for symbol in bars_by_field.get("close", {})
                if symbol not in benchmark
            ),
            benchmark_only_symbols=frozenset(benchmark),
        )
        self._bars = {field: dict(series) for field, series in bars_by_field.items()}
        self._daily = {
            field: dict(series) for field, series in (daily_by_field or {}).items()
        }
        self._cross_section = cross_section

    def bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        return dict(self._bars.get(field, {}))

    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        return dict(self._daily.get(field, {}))

    def financial_indicators(self, field: str) -> dict[str, SymbolSeries]:
        return {}

    def research_dataset(self, kind: str, field: str) -> dict[str, SymbolSeries]:
        if kind == "daily_metrics":
            return self.daily_metrics(field)
        return {}

    def dividend_events(self) -> dict[str, Any]:
        return {}

    def industry_groups(self) -> dict[str, str | None]:
        return {}

    def sample(
        self,
        series_by_symbol: Mapping[str, SymbolSeries],
        per_symbol_values: Mapping[str, np.ndarray],
    ) -> FactorSeriesFrame:
        if self._cross_section:
            universe = tuple(
                symbol
                for symbol in self.tradable_symbols
                if symbol in per_symbol_values
            )
        else:
            universe = tuple(per_symbol_values)
        return sample_series_frame(
            series_by_symbol,
            per_symbol_values,
            decision_dates=self.decision_dates,
            value_universe=universe,
        )


def _return_bars() -> dict[str, dict[str, SymbolSeries]]:
    """收益族合成 bars:两个基准指数 + 形态已知的股票。

    * ``PROP.SH`` —— 日收益 = 2 x 沪深300 日收益(beta = 2,alpha = 0);
    * ``EXC.SH`` —— 日收益 = 2 x 沪深300 + 0.001(beta = 2,alpha = 0.001);
    * ``SIG0.SH`` —— 收盘价与沪深300 逐值相同(残差恒 0);
    * ``SIGE.SH`` —— 日收益 = 沪深300 + 周期 4 残差(残差 = 构造残差);
    * ``BETA_C.SH`` —— 日收益 = 2 x 沪深300 + 周期 4 残差;
    * ``SSE_ONLY.SH`` —— 日收益 = 2 x 上证指数(对 000001 残差恒 0,
      对 000300 非共线 → 残差 > 0,用于鉴别基准映射)。
    """
    hs300 = _hs300_returns()
    sse = _sse_returns()
    resid = _residual_returns()
    hs300_close = _close_from_returns(hs300)
    return {
        "close": {
            HS300: _series(hs300_close),
            SSE: _series(_close_from_returns(sse, base=2000.0)),
            "PROP.SH": _series(_close_from_returns(2.0 * hs300)),
            "EXC.SH": _series(_close_from_returns(2.0 * hs300 + 0.001)),
            "SIG0.SH": _series(hs300_close),
            "SIGE.SH": _series(_close_from_returns(hs300 + resid)),
            "BETA_C.SH": _series(_close_from_returns(2.0 * hs300 + resid)),
            "SSE_ONLY.SH": _series(_close_from_returns(2.0 * sse)),
        }
    }


def _return_universe() -> _Batch6Input:
    return _Batch6Input(_return_bars(), decision_dates=_DATES[-3:])


def _decision_dates() -> tuple[date, ...]:
    return _DATES[-3:]


def _compute(name: str, inp: PredefinedFactorInput) -> FactorSeriesFrame:
    return get_predefined_factor(name).compute(inp)


def _value(frame: FactorSeriesFrame, day: date, symbol: str) -> float:
    """帧取值并断言非缺测(手锚断言前的显式收窄,顺带覆盖 None 意外)。"""
    value = frame[day][symbol]
    assert value is not None, (day, symbol)
    return value


# --------------------------------------------------------------------- #
# 目录不变量(批次 scope)
# --------------------------------------------------------------------- #


class TestCatalogBatch6Market:
    def test_all_14_registered_by_name(self) -> None:
        registered = set(predefined_factor_names())
        assert registered >= BATCH6_MARKET_NAMES
        for name in sorted(BATCH6_MARKET_NAMES):
            assert name in PREDEFINED_FACTORS, name
            assert get_predefined_factor(name).name == name, name
        assert len(BATCH6_MARKET_NAMES) == 14

    def test_specs_exact_per_factor(self) -> None:
        """family / direction / eligibility / 截面 / 窗口 / 覆盖起点 / 依赖 逐项。"""
        assert set(BATCH6_MARKET_SPECS) == set(BATCH6_MARKET_NAMES)
        for name, spec in BATCH6_MARKET_SPECS.items():
            item = get_predefined_factor(name)
            family, direction, eligible, cross_section, window, warmup, deps = spec
            assert item.family == family, name
            assert item.direction is (
                FactorPreference.HIGHER if direction == "higher"
                else FactorPreference.LOWER
            ), name
            assert item.signal_eligible is eligible, name
            assert item.cross_section is cross_section, name
            assert item.window == window, name
            assert item.min_history_bars == warmup, name
            assert item.data_dependencies == deps, name
            assert item.implementation_version == "1", name

    def test_commit_anchors_distinct(self) -> None:
        anchors = {
            predefined_factor_commit(name) for name in BATCH6_MARKET_NAMES
        }
        assert len(anchors) == len(BATCH6_MARKET_NAMES)
        assert all(anchor.startswith("predefined-") for anchor in anchors)

    def test_min_history_covers_window(self) -> None:
        """长窗不变量:声明覆盖起点 >= window;成交量族为 window + 6。"""
        for name in BATCH6_MARKET_NAMES:
            item = get_predefined_factor(name)
            if item.min_history_bars is None:
                assert name == "nl_size", name
                assert item.window is None, name
                continue
            assert item.window is not None, name
            assert item.min_history_bars >= item.window, name
        for name in (
            "volume_alpha_300d_000001",
            "volume_alpha_300d_000300",
            "volume_beta_120d_000300",
        ):
            item = get_predefined_factor(name)
            assert item.min_history_bars == (item.window or 0) + 6, name


# --------------------------------------------------------------------- #
# 收益族手锚:alpha / beta / sigma / beta_consistency
# --------------------------------------------------------------------- #


class TestAlphaBetaAnchors:
    def test_beta_two_and_alpha_zero(self) -> None:
        """日收益 = 2 x 沪深300 日收益 → beta == 2,alpha == 0。"""
        inp = _return_universe()
        beta = _compute("beta_500d_000300", inp)
        alpha = _compute("alpha_500d_000300", inp)
        for day in _decision_dates():
            assert beta[day]["PROP.SH"] == pytest.approx(2.0, rel=1e-9)
            assert alpha[day]["PROP.SH"] == pytest.approx(0.0, abs=1e-12)

    def test_alpha_picks_up_constant_excess(self) -> None:
        """再叠 +0.001 恒定日超额 → alpha == 0.001(斜率不受平移影响)。"""
        inp = _return_universe()
        alpha = _compute("alpha_500d_000300", inp)
        beta = _compute("beta_500d_000300", inp)
        for day in _decision_dates():
            assert alpha[day]["EXC.SH"] == pytest.approx(0.001, rel=1e-6)
            assert beta[day]["EXC.SH"] == pytest.approx(2.0, rel=1e-9)

    def test_long_windows_share_the_same_anchors(self) -> None:
        """1000d / 1320d 窗口同一锚(长窗声明覆盖起点,口径与 500d 一致)。"""
        inp = _return_universe()
        day = _decision_dates()[-1]
        assert _compute("beta_1000d_000300", inp)[day]["EXC.SH"] == pytest.approx(
            2.0, rel=1e-9
        )
        assert _compute("alpha_1000d_000300", inp)[day]["PROP.SH"] == pytest.approx(
            0.0, abs=1e-12
        )
        # SSE_ONLY 对 000001 是等比构造 → 截距 0
        assert _compute("alpha_1320d_000001", inp)[day][
            "SSE_ONLY.SH"
        ] == pytest.approx(0.0, abs=1e-9)

    def test_suffix_selects_matching_index(self) -> None:
        """后缀 000001 取上证指数:对 000001 共线 → 截距 / 残差 0;对 000300 不共线。"""
        inp = _return_universe()
        day = _decision_dates()[-1]
        sse_alpha = _compute("alpha_528d_000001", inp)
        sse_sigma = _compute("sigma_1320d_000001", inp)
        hs300_sigma = _compute("sigma_1320d_000300", inp)
        assert sse_alpha[day]["SSE_ONLY.SH"] == pytest.approx(0.0, abs=1e-9)
        # 等比构造 → 残差只剩 fp 噪声级(~4e-10,与对 000300 的 2.6e-2 差 8 个数量级)
        assert sse_sigma[day]["SSE_ONLY.SH"] == pytest.approx(0.0, abs=1e-8)
        # 与沪深300 非共线 → 特质残差显著为正(有值且不接近 0)
        hs300_value = hs300_sigma[day]["SSE_ONLY.SH"]
        assert hs300_value is not None
        assert hs300_value > 1e-3


class TestSigmaAnchors:
    def test_zero_residual_gives_zero_sigma(self) -> None:
        """收盘价与基准逐值相同 → 残差恒 0 → sigma == 0。"""
        inp = _return_universe()
        frame = _compute("sigma_1320d_000300", inp)
        for day in _decision_dates():
            # 构造上残差恒 0 → sigma 恒 0(abs 容差仅覆盖平台级 fp 舍入)
            assert frame[day]["SIG0.SH"] == pytest.approx(0.0, abs=1e-12)

    def test_periodic_residual_matches_hand_std(self) -> None:
        """周期 4 残差与市场正交 → sigma = c x sqrt(660 / 1319)(手算样本标准差)。

        窗口 = 1320 根 bar(周期 4 的 330 倍),残差取 +c / -c 各 330 个、
        其余 660 个为 0,故 ``std(e, ddof=1) = c x sqrt(660 / 1319)``。
        """
        inp = _return_universe()
        expected = _RESID_C * math.sqrt(660 / 1319)
        frame = _compute("sigma_1320d_000300", inp)
        for day in _decision_dates():
            assert frame[day]["SIGE.SH"] == pytest.approx(expected, rel=1e-9)
        # 上证指数基准同构(基准可参数化):SIGE 与 000001 不共线 → 残差为正
        sse_frame = _compute("sigma_1320d_000001", inp)
        value = sse_frame[_decision_dates()[-1]]["SIGE.SH"]
        assert value is not None
        assert value > 0.0


class TestBetaConsistencyAnchor:
    def test_matches_abs_beta_times_resid_std(self) -> None:
        """同窗 ``|beta| x std(残差)``:beta = 2、残差 = 周期 4 形态 → 手算值。"""
        inp = _return_universe()
        expected = 2.0 * _RESID_C * math.sqrt(660 / 1319)
        frame = _compute("beta_consistency_1320d_000300", inp)
        for day in _decision_dates():
            assert frame[day]["BETA_C.SH"] == pytest.approx(expected, rel=1e-9)
            # beta 为常数、残差恒 0(SIG0)→ 一致性值 0
            assert frame[day]["SIG0.SH"] == pytest.approx(0.0, abs=1e-12)


# --------------------------------------------------------------------- #
# 成交量族手锚(周期 2 成交量 → 两点截面 → 回归直线精确穿过两点)
# --------------------------------------------------------------------- #


def _volume_universe() -> _Batch6Input:
    """成交量合成宇宙:指数与股票均为周期 2 模式(手算闭式 OLS)。

    * 指数成交量 ``V, 2V`` 交替(两个基准同形)→ ``VolMom`` 交替
      ``x_even = -1/8`` / ``x_odd = 1/7``(5 日滚动和按相位为 7V / 8V);
    * ``A.SH`` 成交量 ``U, 4U`` → ``y_even = -3/14`` / ``y_odd = 3/11``
      → 斜率 ``20/11``、截距 ``1/77``;
    * ``B.SH`` 成交量 ``U, 2U``(与指数同形)→ 斜率 1、截距 0;
    * ``GEO.SH`` 成交量等比 ``g^i`` → ``VolMom`` 恒 ``g - 1`` → 斜率 0、
      截距 ``g - 1``;
    * ``LEVEL.SH`` 成交量恒定 → ``VolMom`` 恒 0 → 斜率 0、截距 0。
    """
    idx = np.arange(_N_BARS)
    growth = 1.004
    index_volume = np.where(idx % 2 == 0, 1.0e7, 2.0e7)
    return _Batch6Input(
        {
            "close": {
                HS300: _series(np.linspace(3000.0, 3200.0, _N_BARS)),
                SSE: _series(np.linspace(2000.0, 2100.0, _N_BARS)),
                "A.SH": _series(np.linspace(10.0, 12.0, _N_BARS)),
                "B.SH": _series(np.linspace(20.0, 21.0, _N_BARS)),
                "GEO.SH": _series(np.linspace(30.0, 31.0, _N_BARS)),
                "LEVEL.SH": _series(np.linspace(40.0, 41.0, _N_BARS)),
            },
            "volume": {
                HS300: _series(index_volume),
                SSE: _series(index_volume * 3.0),
                "A.SH": _series(np.where(idx % 2 == 0, 1.0e6, 4.0e6)),
                "B.SH": _series(np.where(idx % 2 == 0, 5.0e5, 1.0e6)),
                "GEO.SH": _series(1.0e6 * growth**idx),
                "LEVEL.SH": _series(np.full(_N_BARS, 1.0e7)),
            },
        },
        decision_dates=_DATES[-3:],
    )


class TestVolumeAnchors:
    def test_volume_beta_matches_hand_slope(self) -> None:
        """斜率:两相位点 → 回归直线精确过两点,斜率 = dy / dx。"""
        inp = _volume_universe()
        frame = _compute("volume_beta_120d_000300", inp)
        for day in _decision_dates():
            assert frame[day]["A.SH"] == pytest.approx(20.0 / 11.0, rel=1e-9)
            assert frame[day]["B.SH"] == pytest.approx(1.0, rel=1e-9)
            assert frame[day]["GEO.SH"] == pytest.approx(0.0, abs=1e-12)
            assert frame[day]["LEVEL.SH"] == pytest.approx(0.0, abs=1e-12)

    def test_volume_alpha_matches_hand_intercept(self) -> None:
        """截距:``y -= slope x`` 后取相位均值(手算 1/77 / 0 / g - 1)。"""
        inp = _volume_universe()
        for name in ("volume_alpha_300d_000300", "volume_alpha_300d_000001"):
            frame = _compute(name, inp)
            for day in _decision_dates():
                assert frame[day]["A.SH"] == pytest.approx(1.0 / 77.0, rel=1e-9), name
                assert frame[day]["B.SH"] == pytest.approx(0.0, abs=1e-12), name
                assert frame[day]["GEO.SH"] == pytest.approx(0.004, rel=1e-6), name
                assert frame[day]["LEVEL.SH"] == pytest.approx(0.0, abs=1e-12), name

    def test_constant_volume_momentum_both_sides_gives_none(self) -> None:
        """两侧 VolMom 恒同值(成交量恒定 → VolMom = 0)→ 指数侧零方差 →
        斜率奇异 → 缺测(宁缺毋假,不返回噪声斜率)。"""
        inp = _Batch6Input(
            {
                "close": {
                    HS300: _series(np.linspace(3000.0, 3200.0, _N_BARS)),
                    "LEVEL.SH": _series(np.linspace(30.0, 31.0, _N_BARS)),
                },
                "volume": {
                    HS300: _series(np.full(_N_BARS, 1.0e7)),
                    "LEVEL.SH": _series(np.full(_N_BARS, 2.0e6)),
                },
            },
            decision_dates=_DATES[-3:],
        )
        beta = _compute("volume_beta_120d_000300", inp)
        alpha = _compute("volume_alpha_300d_000300", inp)
        assert all(beta[day]["LEVEL.SH"] is None for day in _decision_dates())
        assert all(alpha[day]["LEVEL.SH"] is None for day in _decision_dates())


# --------------------------------------------------------------------- #
# nl_size 手锚(截面闭式 cubic 回归残差)
# --------------------------------------------------------------------- #


def _nl_size_input(sizes: Mapping[str, float]) -> _Batch6Input:
    days = _DATES[:5]
    axis = {
        symbol: _series(np.full(5, size), dates=days)
        for symbol, size in sizes.items()
    }
    return _Batch6Input(
        {
            "close": {
                symbol: _series(np.linspace(10.0, 11.0, _N_BARS))
                for symbol in sizes
            }
        },
        decision_dates=(days[-1],),
        daily_by_field={"total_market_cap": axis},
        benchmark=frozenset(),
        cross_section=True,
    )


def _fraction_reference(sizes: Mapping[str, float]) -> dict[str, float]:
    """闭式 OLS 残差的无损参照(Fraction 精确算术,与实现独立)。"""
    values = {symbol: Fraction(value) for symbol, value in sizes.items()}
    mean_size = sum(values.values()) / len(values)
    xs = {symbol: value / mean_size for symbol, value in values.items()}
    ys = {symbol: x**3 for symbol, x in xs.items()}
    mean_x = sum(xs.values()) / len(xs)
    mean_y = sum(ys.values()) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs.values())
    sxy = sum(
        (xs[symbol] - mean_x) * (ys[symbol] - mean_y) for symbol in xs
    )
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    return {
        symbol: float(ys[symbol] - (intercept + slope * xs[symbol]))
        for symbol in xs
    }


class TestNlSizeAnchors:
    """``x = size / mean(size)``、``y = x^3`` 的闭式 OLS 残差(不 rank)。"""

    def test_four_point_residuals_match_hand_anchors(self) -> None:
        sizes = {"S1": 20e9, "S2": 40e9, "S3": 60e9, "S4": 80e9}
        inp = _nl_size_input(sizes)
        frame = _compute("nl_size", inp)
        day = _DATES[4]
        hand = {"S1": 0.4608, "S2": -0.4224, "S3": -0.5376, "S4": 0.4992}
        for symbol, expected in hand.items():
            assert frame[day][symbol] == pytest.approx(expected, rel=1e-9), symbol
        # 手判符号:三次曲线对直线拟合 → 两端(小/大市值)残差为正、中段为负
        s1, s2, s3, s4 = (
            _value(frame, day, symbol) for symbol in ("S1", "S2", "S3", "S4")
        )
        assert s1 > 0.0
        assert s4 > 0.0
        assert s2 < 0.0
        assert s3 < 0.0
        assert s2 > s3
        # 带截距 OLS 的残差和恒为 0
        assert sum(_value(frame, day, symbol) for symbol in sizes) == pytest.approx(
            0.0, abs=1e-15
        )

    def test_outlier_reshapes_residuals(self) -> None:
        """最大市值偏离 1:2:3:4 形态(40e9 → 200e9)后残差手锚。"""
        sizes = {"S1": 20e9, "S2": 40e9, "S3": 60e9, "S4": 200e9}
        inp = _nl_size_input(sizes)
        frame = _compute("nl_size", inp)
        hand = {"S1": 1.550625, "S2": -0.200625, "S3": -1.764375, "S4": 0.414375}
        day = _DATES[4]
        for symbol, expected in hand.items():
            assert frame[day][symbol] == pytest.approx(expected, rel=1e-9), symbol
        # 手判符号:偏离者(最大市值)残差仍为正但被拉低,最小市值残差最大
        s1, s2, s3, s4 = (
            _value(frame, day, symbol) for symbol in ("S1", "S2", "S3", "S4")
        )
        assert s1 > s4 > 0.0
        assert s2 < 0.0
        assert s3 < 0.0
        assert s2 > s3

    def test_benchmark_only_excluded_from_cross_section(self) -> None:
        """benchmark-only 不进截面回归(#380)、也不在截面采样面。"""
        days = _DATES[:5]
        stocks = {"S1": 20e9, "S2": 40e9, "S3": 60e9, "S4": 80e9}
        axis = {
            symbol: _series(np.full(5, size), dates=days)
            for symbol, size in stocks.items()
        }
        axis[HS300] = _series(np.full(5, 9e15), dates=days)
        inp = _Batch6Input(
            {"close": {symbol: _series(np.linspace(10.0, 11.0, _N_BARS))
                       for symbol in (*stocks, HS300)}},
            decision_dates=(days[-1],),
            daily_by_field={"total_market_cap": axis},
            benchmark=frozenset({HS300}),
            cross_section=True,
        )
        frame = _compute("nl_size", inp)
        day = days[-1]
        assert HS300 not in frame[day]
        reference = _fraction_reference(stocks)
        for symbol, expected in reference.items():
            assert frame[day][symbol] == pytest.approx(expected, rel=1e-9), symbol

    def test_matches_fraction_reference(self) -> None:
        """与 Fraction 精确算术参照逐值一致(证明残差口径无实现漂移)。"""
        sizes = {"S1": 17e9, "S2": 31e9, "S3": 55e9, "S4": 61e9, "S5": 90e9}
        inp = _nl_size_input(sizes)
        frame = _compute("nl_size", inp)
        day = _DATES[4]
        for symbol, expected in _fraction_reference(sizes).items():
            assert frame[day][symbol] == pytest.approx(expected, rel=1e-9), symbol


# --------------------------------------------------------------------- #
# 边界:指数缺失 / 历史不足 / 截面退化
# --------------------------------------------------------------------- #


class TestBoundaries:
    def test_missing_benchmark_index_gives_none(self) -> None:
        """发布不含基准指数 → 全缺测(不 fallback 到别的指数)。"""
        bars = {
            "close": {"ONLY.SH": _series(np.linspace(10.0, 12.0, _N_BARS))},
            "volume": {"ONLY.SH": _series(np.full(_N_BARS, 1.0e7))},
        }
        inp = _Batch6Input(bars, decision_dates=_DATES[-3:], benchmark=frozenset())
        for name in (
            "alpha_500d_000300",
            "beta_1000d_000300",
            "sigma_1320d_000300",
            "beta_consistency_1320d_000300",
            "volume_alpha_300d_000300",
            "volume_beta_120d_000300",
        ):
            frame = _compute(name, inp)
            assert all(frame[day]["ONLY.SH"] is None for day in _decision_dates()), name

    def test_index_without_requested_field_gives_none(self) -> None:
        """指数有 close 无 volume → 成交量族缺测(不换字段 / 不换指数)。"""
        bars = {
            "close": {
                HS300: _series(np.linspace(3000.0, 3200.0, _N_BARS)),
                "ONLY.SH": _series(np.linspace(10.0, 12.0, _N_BARS)),
            },
            "volume": {"ONLY.SH": _series(np.full(_N_BARS, 1.0e7))},
        }
        inp = _Batch6Input(bars, decision_dates=_DATES[-3:])
        frame = _compute("volume_beta_120d_000300", inp)
        assert all(frame[day]["ONLY.SH"] is None for day in _decision_dates())
        frame_alpha = _compute("alpha_500d_000300", inp)
        assert all(frame_alpha[day]["ONLY.SH"] is not None for day in _decision_dates())

    def test_insufficient_history_gives_none(self) -> None:
        """bar 数 < 窗口(含成交量族的 window + 6)→ 缺测。"""
        short = _DATES[:100]
        bars = {
            "close": {
                HS300: _series(np.linspace(3000.0, 3100.0, 100), dates=short),
                "ONLY.SH": _series(np.linspace(10.0, 12.0, 100), dates=short),
            },
            "volume": {
                HS300: _series(np.linspace(1.0e7, 1.5e7, 100), dates=short),
                "ONLY.SH": _series(np.linspace(2.0e6, 3.0e6, 100), dates=short),
            },
        }
        inp = _Batch6Input(bars, decision_dates=(short[-1],))
        for name in (
            "alpha_500d_000300",
            "sigma_1320d_000300",
            "volume_alpha_300d_000300",
            "volume_beta_120d_000300",
        ):
            frame = _compute(name, inp)
            assert frame[short[-1]]["ONLY.SH"] is None, name

    def test_nl_size_needs_three_finite_points(self) -> None:
        """截面有限点 < 3 → 该日全缺测。"""
        inp = _nl_size_input({"S1": 20e9, "S2": 40e9})
        frame = _compute("nl_size", inp)
        assert frame[_DATES[4]] == {"S1": None, "S2": None}

    def test_nl_size_zero_variance_gives_none(self) -> None:
        """截面方差为 0(市值全同)→ 斜率奇异 → 该日全缺测。"""
        inp = _nl_size_input({"S1": 50e9, "S2": 50e9, "S3": 50e9, "S4": 50e9})
        frame = _compute("nl_size", inp)
        assert set(frame[_DATES[4]].values()) == {None}

    def test_nl_size_suspended_symbol_uses_last_cross_section(self) -> None:
        """停牌(该日无 daily 行)标的取最近可得截面残差,其余同日重算。"""
        days = _DATES[:5]
        sizes = {"S1": 20e9, "S2": 40e9, "S3": 60e9, "S4": 80e9}
        axis: dict[str, SymbolSeries] = {}
        for symbol, size in sizes.items():
            if symbol == "S4":
                # 只到第 4 天 → 第 5 天该标的沿用第 4 天截面残差
                axis[symbol] = _series(np.full(4, size), dates=days[:4])
            else:
                axis[symbol] = _series(np.full(5, size), dates=days)
        inp = _Batch6Input(
            {"close": {symbol: _series(np.linspace(10.0, 11.0, _N_BARS))
                       for symbol in sizes}},
            decision_dates=(days[4],),
            daily_by_field={"total_market_cap": axis},
            benchmark=frozenset(),
            cross_section=True,
        )
        frame = _compute("nl_size", inp)
        day = days[4]
        # S4 停牌:值 = 第 4 天截面残差(与第 4 天同日 4 点口径一致)
        expected_day4 = _fraction_reference(sizes)
        assert frame[day]["S4"] == pytest.approx(expected_day4["S4"], rel=1e-9)
        # 其余三只第 5 天截面只有 3 点 → 独立重算(与 4 点口径不同)
        three_point = _fraction_reference(
            {"S1": 20e9, "S2": 40e9, "S3": 60e9}
        )
        for symbol in ("S1", "S2", "S3"):
            assert frame[day][symbol] == pytest.approx(
                three_point[symbol], rel=1e-9
            ), symbol
