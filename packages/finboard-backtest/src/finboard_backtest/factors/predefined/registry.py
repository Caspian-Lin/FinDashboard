"""平台预置因子目录(issue #398,批次 0;批次 #399-#402 的注册地基)。

目录条目 = 「公式即代码」:name / 公式描述 / 数据依赖 / 方向 /
signal_eligible / 参数化窗口,``compute`` 是平台可信代码 —— 构建走
**进程内** factor_series 通道(免用户因子的容器税,审计 / 内容寻址 /
覆盖检查全套同构,见 ``research_sandbox.predefined_runner``)。

**引用命名** ``p_<name>``(``PREDEFINED_FACTOR_PREFIX``,与用户因子
``u_`` 对称,见 ``finboard_data.factor_lab``);目录内部只存裸名。

**批次 0 样板族** ``return_{21,63,126,252}d``:同一参数化实现按 tushare
命名展开注册(动量族 return_N)。

**批次 2:Alpha101 量价因子族** ``alpha101_{N}``(issue #400,31 个):
WorldQuant《101 Formulaic Alphas》(Kakushadze 2015)经 tushare
``factor_list`` 口径圈定的 31 个纯 OHLCV 截面因子,公式逐条直译为 C0
算子组合。批次级共享口径(逐因子 docstring 只写差异):

* ``Returns`` = ``close / ts_delay(close, 1) - 1``(qfq 收盘日收益);
* ``VWAP`` = ``amount / volume``(issue 验收口径的近似;量纲 = 缓存
  两列原生单位比,非保证「元/股」——akshare 股票(元/手)下为
  100x VWAP、tushare(千元/手)下为 10x;排名类公式对恒定缩放不敏感,
  与价格做差比较的因子(#5/#11/#19/#25/#41/#57)受量纲影响,属已知
  近似,跨源量纲归一留后续);
* ``ADV20`` = ``ts_mean(volume, 20)`(与主流复现一致取成交量而非成交额);
* ``Rank(x)`` = 逐日截面百分位 ``cs_rank``,分母 = 可交易域
  (#380:benchmark-only 不进截面分母,``_cs_rank_series`` 收窄);
* ``Ts_*`` = operators 时序算子(因果 trailing 窗口,前缀不变性审计
  兜底);条件 ``IF / ?:`` 经 ``ew_where``(条件不可判 → NaN);
* **行业中性化**:论文与 tushare 口径下本批次 31 条公式**均不含**
  ``indneutralize`` 项(论文仅 Alpha#47/#99 使用,不在本批次);行业
  分组装配(``inp.industry_groups()``,冻结发布 instruments.industry
  近似 #185/#212 分组)与 :func:`industry_neutralize` 助手(缺组 →
  缺测 + 计数)随本批次交付,供分组 sanity 与后续批次消费。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from finboard_backtest.factors.predefined.context import (
    FactorSeriesFrame,
    PredefinedFactorInput,
    SymbolSeries,
)
from finboard_backtest.factors.predefined.operators import (
    CrossSection,
    cs_neutralize,
    cs_rank,
    ew_div,
    ew_gt,
    ew_log,
    ew_lt,
    ew_sign,
    ew_signed_power,
    ew_where,
    ts_argmax,
    ts_corr,
    ts_cov,
    ts_decay,
    ts_delay,
    ts_delta,
    ts_max,
    ts_mean,
    ts_min,
    ts_rank,
    ts_std,
    ts_sum,
)
from finboard_data.factor_lab import FactorPreference

#: 因子裸名规则(目录内部不带 p_/u_ 前缀)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: 目录级 schema 版本(参与 commit 锚;调整采样/依赖语义等跨因子规则时递增)
PREDEFINED_FACTORS_SCHEMA_VERSION = "v1"

#: compute 的函数签名:(PredefinedFactorInput) -> FactorSeriesFrame
PredefinedFactorCompute = Callable[[PredefinedFactorInput], FactorSeriesFrame]


@dataclass(frozen=True)
class PredefinedFactorDefinition:
    """一个平台预置因子的目录条目。

    * ``name`` —— 裸名(如 ``return_21d``),引用名 = ``p_return_21d``;
    * ``data_dependencies`` —— 数据依赖声明,条目形如
      ``"bars.<field>"`` / ``"daily_metrics.<field>"`` /
      ``"financial_indicators.<field>"`` / ``"index_bars.close"``(指数
      基准行情,引擎按发布 instruments 识别);构建引擎据此决定加载哪些
      挂载数据集,#401/#402 基本面因子由此声明研究发布依赖;
    * ``direction`` —— 研究语义方向(``FactorPreference``;HIGHER =
      值越大越看多),只做研究偏好,信号方向仍由策略规格决定;
    * ``implementation_version`` —— **实现版本**:公式 / 数值语义任何
      变化必须递增;它是内容寻址 commit 锚的原料(版本不变 → 同参数
      重建命中缓存,版本变化 → 新序列);
    * ``cross_section`` —— 最终值是截面算子产物:构建采样面收窄到
      可交易域(#380:截面分母不得混入 benchmark-only);时序因子
      False(全挂载标的,消费端统一剔除基准)。
    """

    name: str
    title: str
    family: str
    direction: FactorPreference
    signal_eligible: bool
    data_dependencies: tuple[str, ...]
    window: int | None
    implementation_version: str
    compute: PredefinedFactorCompute
    cross_section: bool = False

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(
                f"预置因子名 {self.name!r} 不合规则(小写 [a-z][a-z0-9_]*,"
                "目录内部不带 p_/u_ 前缀)"
            )
        if not self.title or not self.family or not self.implementation_version:
            raise ValueError(f"{self.name}: title/family/implementation_version 必填")
        if self.window is not None and self.window < 1:
            raise ValueError(f"{self.name}: window 须 >= 1 或 None")
        if not self.data_dependencies:
            raise ValueError(f"{self.name}: 必须声明数据依赖")
        for item in self.data_dependencies:
            if "." not in item:
                raise ValueError(
                    f"{self.name}: 数据依赖 {item!r} 须为 '<dataset>.<field>' 形态"
                )


def _momentum_return(window: int) -> PredefinedFactorCompute:
    """参数化的 ``return_Nd`` 实现(同族窗口变体一份代码,#398)。

    ``return_Nd = close[t] / close[t-N] - 1``——N 根 bar 的区间收益
    (qfq 收盘;停牌缺行使窗口自然后移到最近可得 bar,「N 根 bar」
    而非「N 个日历日」);历史不足 N 根 → 缺测(None)。
    """

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, Any] = {}
        for symbol, series in closes.items():
            base = ts_delay(series.values, window)
            per_symbol[symbol] = ts_delta(series.values, window) / base
        return inp.sample(closes, per_symbol)

    return compute


def _definition(
    name: str,
    *,
    title: str,
    family: str,
    window: int,
    direction: FactorPreference = FactorPreference.HIGHER,
    cross_section: bool = False,
    signal_eligible: bool = True,
    data_dependencies: tuple[str, ...] = ("bars.close",),
    implementation_version: str = "1",
) -> PredefinedFactorDefinition:
    """``return_Nd`` 族的条目工厂(唯一公式实现,窗口变体展开注册)。"""
    return PredefinedFactorDefinition(
        name=name,
        title=title,
        family=family,
        direction=direction,
        signal_eligible=signal_eligible,
        data_dependencies=data_dependencies,
        window=window,
        implementation_version=implementation_version,
        compute=_momentum_return(window),
        cross_section=cross_section,
    )


# --------------------------------------------------------------------- #
# Alpha101 批次(issue #400)——共享口径辅助
# --------------------------------------------------------------------- #


def _values(
    series_by_symbol: Mapping[str, SymbolSeries],
) -> dict[str, np.ndarray]:
    """{symbol: SymbolSeries} → {symbol: 1-D float64 值数组}(逐行对齐)。"""
    return {symbol: series.values for symbol, series in series_by_symbol.items()}


def _per_symbol(
    values: Mapping[str, np.ndarray],
    fn: Callable[[np.ndarray], np.ndarray],
) -> dict[str, np.ndarray]:
    """逐标的套用一元时序/逐点算子(输入与输出逐行对齐)。"""
    return {symbol: fn(item) for symbol, item in values.items()}


def _per_symbol_pair(
    left: Mapping[str, np.ndarray],
    right: Mapping[str, np.ndarray],
    fn: Callable[[np.ndarray, np.ndarray], np.ndarray],
) -> dict[str, np.ndarray]:
    """逐标的套用二元算子(两字段序列同一挂载表逐行对齐)。"""
    return {symbol: fn(left[symbol], right[symbol]) for symbol in left}


def _returns_values(closes: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    """``Returns = close / delay(close, 1) - 1``(qfq 收盘;首行缺测)。"""
    return _per_symbol(
        closes, lambda c: ew_div(ts_delta(c, 1), ts_delay(c, 1))
    )


def _adv20_values(volumes: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    """``ADV20`` = 20 日成交量均值(主流复现口径,量纲自洽)。"""
    return _per_symbol(volumes, lambda v: ts_mean(v, 20))


def _vwap_values(inp: PredefinedFactorInput) -> dict[str, np.ndarray]:
    """``VWAP ≈ amount / volume``(issue #400 验收口径;volume<=0 → NaN)。"""
    amounts = _values(inp.bars("amount"))
    volumes = _values(inp.bars("volume"))
    return _per_symbol_pair(amounts, volumes, lambda a, v: ew_div(a, v))


def _cs_transform(
    axis: Mapping[str, SymbolSeries],
    per_symbol: Mapping[str, np.ndarray],
    transform: Callable[[CrossSection], Mapping[str, float | None]],
    *,
    universe: Collection[str],
) -> dict[str, np.ndarray]:
    """逐自然日截面变换回填(Alpha101 的 ``Rank(x)`` 输入装配)。

    对每个业务日,取 ``universe`` 内标的当日有限值组截面喂给 ``transform``
    (如 :func:`cs_rank`),结果回填到各标的行序;非有限/缺测 → NaN。
    只用同日截面值 → 因果(截断前缀不变);``universe`` 外的标的
    (benchmark-only)全行 NaN,不进截面分母(#380)。
    """
    members = set(universe) & set(axis)
    by_date: dict[Any, list[tuple[str, int, float]]] = {}
    for symbol in sorted(members):
        series = axis[symbol]
        values = per_symbol[symbol]
        for position, day in enumerate(series.dates):
            value = float(values[position])
            if math.isfinite(value):
                by_date.setdefault(day, []).append((symbol, position, value))
    out = {s: np.full(v.size, np.nan) for s, v in per_symbol.items()}
    for items in by_date.values():
        cross = {symbol: value for symbol, _position, value in items}
        transformed = transform(cross)
        for symbol, position, _value in items:
            mapped = transformed.get(symbol)
            if mapped is not None and math.isfinite(mapped):
                out[symbol][position] = mapped
    return out


def _cs_rank_series(
    axis: Mapping[str, SymbolSeries],
    per_symbol: Mapping[str, np.ndarray],
    inp: PredefinedFactorInput,
) -> dict[str, np.ndarray]:
    """Alpha101 的 ``Rank(x)``:逐日截面百分位(分母 = 可交易域,#380)。"""
    return _cs_transform(axis, per_symbol, cs_rank, universe=inp.tradable_symbols)


def _signed_momentum_rule(delta: np.ndarray, window: int) -> np.ndarray:
    """Alpha101#9/#10 的条件符号规则:
    ``(0 < Ts_Min(d, w)) ? d : ((Ts_Max(d, w) < 0) ? d : -d)``。"""
    cond_rising = ew_gt(ts_min(delta, window), 0.0)
    cond_falling = ew_lt(ts_max(delta, window), 0.0)
    return ew_where(cond_rising, delta, ew_where(cond_falling, delta, -delta))


def _compute_alpha101_1(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank(Ts_ArgMax(SignedPower(IF(Returns<0, StdDev(Returns,20), Close), 2), 5)) - 0.5``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    returns = _returns_values(closes)
    inner = {
        symbol: ew_where(
            ew_lt(item, 0.0), closes[symbol], ts_std(item, 20)
        )
        for symbol, item in returns.items()
    }
    positioned = _per_symbol(
        _per_symbol(inner, lambda x: ew_signed_power(x, 2.0)),
        lambda x: ts_argmax(x, 5),
    )
    ranked = _cs_rank_series(axis, positioned, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: x - 0.5))


def _compute_alpha101_2(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Corr(Rank(Delta(Log(Volume), 2)), Rank((Close-Open)/Open), 6)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    volumes = _values(inp.bars("volume"))
    left = _cs_rank_series(
        axis, _per_symbol(volumes, lambda v: ts_delta(ew_log(v), 2)), inp
    )
    right = _cs_rank_series(
        axis,
        _per_symbol_pair(closes, opens, lambda c, o: ew_div(c - o, o)),
        inp,
    )
    return inp.sample(
        axis, _per_symbol_pair(left, right, lambda a, b: -ts_corr(a, b, 6))
    )


def _compute_alpha101_3(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Corr(Rank(Open), Rank(Volume), 10)``。"""
    axis = inp.bars("open")
    ranked_open = _cs_rank_series(axis, _values(axis), inp)
    ranked_volume = _cs_rank_series(
        inp.bars("close"), _values(inp.bars("volume")), inp
    )
    return inp.sample(
        axis,
        _per_symbol_pair(ranked_open, ranked_volume, lambda a, b: -ts_corr(a, b, 10)),
    )


def _compute_alpha101_4(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Ts_Rank(Rank(Low), 9)``。"""
    axis = inp.bars("low")
    ranked_low = _cs_rank_series(axis, _values(axis), inp)
    return inp.sample(axis, _per_symbol(ranked_low, lambda x: -ts_rank(x, 9)))


def _compute_alpha101_5(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank(Open - Sum(VWAP,10)/10) * (-1 * Abs(Rank(Close - VWAP)))``。

    VWAP = amount/volume 近似(模块 docstring 量纲注记)。
    """
    axis = inp.bars("open")
    opens = _values(axis)
    closes = _values(inp.bars("close"))
    vwap = _vwap_values(inp)
    left = _cs_rank_series(
        axis,
        _per_symbol_pair(opens, vwap, lambda o, v: o - ts_sum(v, 10) / 10.0),
        inp,
    )
    right = _cs_rank_series(
        axis,
        _per_symbol_pair(closes, vwap, lambda c, v: c - v),
        inp,
    )
    return inp.sample(
        axis,
        _per_symbol_pair(left, right, lambda a, b: a * -np.abs(b)),
    )


def _compute_alpha101_6(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Corr(Open, Volume, 10)``。"""
    axis = inp.bars("open")
    volumes = _values(inp.bars("volume"))
    return inp.sample(
        axis,
        _per_symbol_pair(_values(axis), volumes, lambda o, v: -ts_corr(o, v, 10)),
    )


def _compute_alpha101_7(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``IF(ADV20 < Volume, -Ts_Rank(Abs(Delta(Close,7)), 60) * Sign(Delta(Close,7)), -1)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    volumes = _values(inp.bars("volume"))
    adv20 = _adv20_values(volumes)
    delta7 = _per_symbol(closes, lambda c: ts_delta(c, 7))
    body = _per_symbol(
        delta7, lambda d: -ts_rank(np.abs(d), 60) * ew_sign(d)
    )
    condition = _per_symbol_pair(volumes, adv20, lambda v, a: ew_gt(v, a))
    fallback = {symbol: np.full(v.size, -1.0) for symbol, v in condition.items()}
    return inp.sample(
        axis,
        {
            symbol: ew_where(condition[symbol], body[symbol], fallback[symbol])
            for symbol in condition
        },
    )


def _compute_alpha101_8(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Rank(Sum(Open,5) * Sum(Returns,5) - Delay(Sum(Open,5) * Sum(Returns,5), 10))``。"""
    axis = inp.bars("close")
    opens = _values(inp.bars("open"))
    returns = _returns_values(_values(axis))
    combined = _per_symbol_pair(
        opens, returns, lambda o, r: ts_sum(o, 5) * ts_sum(r, 5)
    )
    diff = _per_symbol(combined, lambda x: x - ts_delay(x, 10))
    ranked = _cs_rank_series(axis, diff, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: -x))


def _compute_alpha101_9(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``IF(0 < Ts_Min(Delta(Close,1),5), Delta(Close,1), IF(Ts_Max(Delta(Close,1),5) < 0, Delta(Close,1), -Delta(Close,1)))``。"""
    axis = inp.bars("close")
    delta1 = _per_symbol(_values(axis), lambda c: ts_delta(c, 1))
    return inp.sample(
        axis, _per_symbol(delta1, lambda d: _signed_momentum_rule(d, 5))
    )


def _compute_alpha101_10(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank(IF(0 < Ts_Min(Delta(Close,1),4), Delta(Close,1), IF(Ts_Max(Delta(Close,1),4) < 0, Delta(Close,1), -Delta(Close,1))))``。"""
    axis = inp.bars("close")
    delta1 = _per_symbol(_values(axis), lambda c: ts_delta(c, 1))
    ruled = _per_symbol(delta1, lambda d: _signed_momentum_rule(d, 4))
    return inp.sample(axis, _cs_rank_series(axis, ruled, inp))


def _compute_alpha101_11(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``(Rank(Ts_Max(VWAP-Close,3)) + Rank(Ts_Min(VWAP-Close,3))) * Rank(Delta(Volume,3))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    volumes = _values(inp.bars("volume"))
    vwap = _vwap_values(inp)
    deviation = _per_symbol_pair(vwap, closes, lambda v, c: v - c)
    upper = _cs_rank_series(axis, _per_symbol(deviation, lambda x: ts_max(x, 3)), inp)
    lower = _cs_rank_series(axis, _per_symbol(deviation, lambda x: ts_min(x, 3)), inp)
    volume_rank = _cs_rank_series(
        axis, _per_symbol(volumes, lambda v: ts_delta(v, 3)), inp
    )
    return inp.sample(
        axis,
        {
            symbol: (upper[symbol] + lower[symbol]) * volume_rank[symbol]
            for symbol in upper
        },
    )


def _compute_alpha101_12(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Sign(Delta(Volume,1)) * (-1 * Delta(Close,1))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    volumes = _values(inp.bars("volume"))
    return inp.sample(
        axis,
        {
            symbol: ew_sign(ts_delta(volumes[symbol], 1))
            * -ts_delta(closes[symbol], 1)
            for symbol in closes
        },
    )


def _compute_alpha101_13(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Rank(Covariance(Rank(Close), Rank(Volume), 5))``。"""
    axis = inp.bars("close")
    ranked_close = _cs_rank_series(axis, _values(axis), inp)
    ranked_volume = _cs_rank_series(
        inp.bars("volume"), _values(inp.bars("volume")), inp
    )
    covariance = _per_symbol_pair(
        ranked_close, ranked_volume, lambda a, b: ts_cov(a, b, 5)
    )
    ranked = _cs_rank_series(axis, covariance, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: -x))


def _compute_alpha101_14(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Rank(Delta(Returns,3)) * Corr(Open, Volume, 10)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    volumes = _values(inp.bars("volume"))
    ranked_delta = _cs_rank_series(
        axis,
        _per_symbol(_returns_values(closes), lambda r: ts_delta(r, 3)),
        inp,
    )
    correlation = _per_symbol_pair(opens, volumes, lambda o, v: ts_corr(o, v, 10))
    return inp.sample(
        axis,
        _per_symbol_pair(ranked_delta, correlation, lambda a, b: -a * b),
    )


def _compute_alpha101_15(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Sum(Rank(Corr(Rank(High), Rank(Volume), 3)), 3)``。"""
    axis = inp.bars("high")
    ranked_high = _cs_rank_series(axis, _values(axis), inp)
    ranked_volume = _cs_rank_series(
        inp.bars("volume"), _values(inp.bars("volume")), inp
    )
    correlation = _per_symbol_pair(
        ranked_high, ranked_volume, lambda a, b: ts_corr(a, b, 3)
    )
    ranked = _cs_rank_series(axis, correlation, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: -ts_sum(x, 3)))


def _compute_alpha101_16(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Rank(Covariance(Rank(High), Rank(Volume), 5))``。"""
    axis = inp.bars("high")
    ranked_high = _cs_rank_series(axis, _values(axis), inp)
    ranked_volume = _cs_rank_series(
        inp.bars("volume"), _values(inp.bars("volume")), inp
    )
    covariance = _per_symbol_pair(
        ranked_high, ranked_volume, lambda a, b: ts_cov(a, b, 5)
    )
    ranked = _cs_rank_series(axis, covariance, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: -x))


def _compute_alpha101_17(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-Rank(Ts_Rank(Close,10)) * Rank(Delta(Delta(Close,1),1)) * Rank(Ts_Rank(Volume/ADV20,5))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    volumes = _values(inp.bars("volume"))
    adv20 = _adv20_values(volumes)
    rank_price = _cs_rank_series(
        axis, _per_symbol(closes, lambda c: ts_rank(c, 10)), inp
    )
    rank_accel = _cs_rank_series(
        axis,
        _per_symbol(closes, lambda c: ts_delta(ts_delta(c, 1), 1)),
        inp,
    )
    relative_volume = _per_symbol_pair(volumes, adv20, lambda v, a: ew_div(v, a))
    rank_volume = _cs_rank_series(
        axis, _per_symbol(relative_volume, lambda r: ts_rank(r, 5)), inp
    )
    return inp.sample(
        axis,
        {
            symbol: -rank_price[symbol] * rank_accel[symbol] * rank_volume[symbol]
            for symbol in rank_price
        },
    )


def _compute_alpha101_18(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-1 * Rank(StdDev(Abs(Close-Open),5) + (Close-Open) + Corr(Close,Open,10))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    body = _per_symbol_pair(
        closes,
        opens,
        lambda c, o: ts_std(np.abs(c - o), 5) + (c - o) + ts_corr(c, o, 10),
    )
    ranked = _cs_rank_series(axis, body, inp)
    return inp.sample(axis, _per_symbol(ranked, lambda x: -x))


def _compute_alpha101_19(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-Sign((Close-Delay(Close,7))+Delta(Close,7)) * (1+Rank(1+Sum(Returns,250))) - Rank(X)^2``;

    其中 ``X = Corr(Rank(VWAP-Close), Rank(Volume), 12) * Corr(Rank(Close),
    Rank(ADV20), 12)``(tushare 口径把论文的 scale/indneutralize 项写为
    ``-Rank(X) * Rank(X)``,逐字直译)。
    """
    axis = inp.bars("close")
    closes = _values(axis)
    volumes = _values(inp.bars("volume"))
    returns = _returns_values(closes)
    vwap = _vwap_values(inp)
    adv20 = _adv20_values(volumes)
    momentum_sign = _per_symbol(
        closes, lambda c: ew_sign((c - ts_delay(c, 7)) + ts_delta(c, 7))
    )
    long_return_rank = _cs_rank_series(
        axis,
        _per_symbol(returns, lambda r: 1.0 + ts_sum(r, 250)),
        inp,
    )
    term1 = _per_symbol_pair(
        momentum_sign, long_return_rank, lambda s, r: -s * (1.0 + r)
    )
    left_corr = _per_symbol_pair(
        _cs_rank_series(
            axis,
            _per_symbol_pair(vwap, closes, lambda v, c: v - c),
            inp,
        ),
        _cs_rank_series(inp.bars("volume"), volumes, inp),
        lambda a, b: ts_corr(a, b, 12),
    )
    right_corr = _per_symbol_pair(
        _cs_rank_series(axis, closes, inp),
        _cs_rank_series(axis, adv20, inp),
        lambda a, b: ts_corr(a, b, 12),
    )
    product = _per_symbol_pair(left_corr, right_corr, lambda a, b: a * b)
    term2_rank = _cs_rank_series(axis, product, inp)
    return inp.sample(
        axis,
        _per_symbol_pair(
            term1, term2_rank, lambda a, r: a + (-r * r)
        ),
    )


def _compute_alpha101_20(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-Rank(Open-Delay(High,1)) * Rank(Open-Delay(Close,1)) * Rank(Open-Delay(Low,1))``。"""
    axis = inp.bars("open")
    opens = _values(axis)
    highs = _values(inp.bars("high"))
    closes = _values(inp.bars("close"))
    lows = _values(inp.bars("low"))
    gap_high = _cs_rank_series(
        axis,
        _per_symbol_pair(opens, highs, lambda o, h: o - ts_delay(h, 1)),
        inp,
    )
    gap_close = _cs_rank_series(
        axis,
        _per_symbol_pair(opens, closes, lambda o, c: o - ts_delay(c, 1)),
        inp,
    )
    gap_low = _cs_rank_series(
        axis,
        _per_symbol_pair(opens, lows, lambda o, low: o - ts_delay(low, 1)),
        inp,
    )
    return inp.sample(
        axis,
        {
            symbol: -gap_high[symbol] * gap_close[symbol] * gap_low[symbol]
            for symbol in gap_high
        },
    )


def _compute_alpha101_22(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-Delta(Corr(High, Volume, 5), 5) * Rank(StdDev(Close, 20))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    highs = _values(inp.bars("high"))
    volumes = _values(inp.bars("volume"))
    correlation_delta = _per_symbol_pair(
        highs, volumes, lambda h, v: ts_delta(ts_corr(h, v, 5), 5)
    )
    volatility_rank = _cs_rank_series(
        axis, _per_symbol(closes, lambda c: ts_std(c, 20)), inp
    )
    return inp.sample(
        axis,
        _per_symbol_pair(
            correlation_delta, volatility_rank, lambda a, b: -(a * b)
        ),
    )


def _compute_alpha101_23(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``IF(Sum(High,20)/20 < High, -Delta(High,2), 0)``。"""
    axis = inp.bars("high")
    highs = _values(axis)
    return inp.sample(
        axis,
        {
            symbol: ew_where(
                ew_lt(ts_sum(highs[symbol], 20) / 20.0, highs[symbol]),
                -ts_delta(highs[symbol], 2),
                np.zeros(highs[symbol].size),
            )
            for symbol in highs
        },
    )


def _compute_alpha101_25(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank(-1 * Returns * ADV20 * VWAP * (High - Close))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    highs = _values(inp.bars("high"))
    volumes = _values(inp.bars("volume"))
    returns = _returns_values(closes)
    adv20 = _adv20_values(volumes)
    vwap = _vwap_values(inp)
    body = {
        symbol: -returns[symbol]
        * adv20[symbol]
        * vwap[symbol]
        * (highs[symbol] - closes[symbol])
        for symbol in closes
    }
    return inp.sample(axis, _cs_rank_series(axis, body, inp))


def _compute_alpha101_33(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank(-1 * (1 - Open/Close))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    body = _per_symbol_pair(opens, closes, lambda o, c: -(1.0 - ew_div(o, c)))
    return inp.sample(axis, _cs_rank_series(axis, body, inp))


def _compute_alpha101_34(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``Rank((1 - Rank(StdDev(Returns,2)/StdDev(Returns,5))) + (1 - Rank(Delta(Close,1))))``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    returns = _returns_values(closes)
    volatility_ratio = _per_symbol(
        returns, lambda r: ew_div(ts_std(r, 2), ts_std(r, 5))
    )
    rank_ratio = _cs_rank_series(axis, volatility_ratio, inp)
    rank_delta = _cs_rank_series(
        axis, _per_symbol(closes, lambda c: ts_delta(c, 1)), inp
    )
    combined = {
        symbol: (1.0 - rank_ratio[symbol]) + (1.0 - rank_delta[symbol])
        for symbol in rank_ratio
    }
    return inp.sample(axis, _cs_rank_series(axis, combined, inp))


def _compute_alpha101_41(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``(High*Low)^0.5 - VWAP``(VWAP = amount/volume 近似,量纲注记见模块 docstring)。"""
    axis = inp.bars("close")
    highs = _values(inp.bars("high"))
    lows = _values(inp.bars("low"))
    vwap = _vwap_values(inp)
    with np.errstate(invalid="ignore"):
        body = {
            symbol: np.sqrt(highs[symbol] * lows[symbol]) - vwap[symbol]
            for symbol in highs
        }
    return inp.sample(axis, body)


def _compute_alpha101_52(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``(-Ts_Min(Low,5) + Delay(Ts_Min(Low,5),5)) * Rank((Sum(Returns,240)-Sum(Returns,20))/220) * Ts_Rank(Volume,5)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    lows = _values(inp.bars("low"))
    volumes = _values(inp.bars("volume"))
    returns = _returns_values(closes)
    low_trough = _per_symbol(lows, lambda low: ts_min(low, 5))
    head = _per_symbol(low_trough, lambda m: -m + ts_delay(m, 5))
    momentum_spread = _per_symbol(
        returns, lambda r: (ts_sum(r, 240) - ts_sum(r, 20)) / 220.0
    )
    ranked_spread = _cs_rank_series(axis, momentum_spread, inp)
    volume_rank = _per_symbol(volumes, lambda v: ts_rank(v, 5))
    return inp.sample(
        axis,
        {
            symbol: (head[symbol] * ranked_spread[symbol]) * volume_rank[symbol]
            for symbol in head
        },
    )


def _compute_alpha101_53(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-Delta(((Close-Low)-(High-Close))/(Close-Low), 9)``(分母 0 → NaN,#400 除法纪律)。"""
    axis = inp.bars("close")
    closes = _values(axis)
    highs = _values(inp.bars("high"))
    lows = _values(inp.bars("low"))
    body = {
        symbol: ew_div(
            (closes[symbol] - lows[symbol]) - (highs[symbol] - closes[symbol]),
            closes[symbol] - lows[symbol],
        )
        for symbol in closes
    }
    return inp.sample(
        axis, _per_symbol(body, lambda x: -ts_delta(x, 9))
    )


def _compute_alpha101_54(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``(-1 * (Low-Close) * Open^5) / ((Low-High) * Close^5)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    highs = _values(inp.bars("high"))
    lows = _values(inp.bars("low"))
    body = {
        symbol: ew_div(
            -1.0 * (lows[symbol] - closes[symbol]) * opens[symbol] ** 5,
            (lows[symbol] - highs[symbol]) * closes[symbol] ** 5,
        )
        for symbol in closes
    }
    return inp.sample(axis, body)


def _compute_alpha101_57(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``-(Close - VWAP) / Decay_Linear(Rank(Ts_ArgMax(Close,30)), 2)``。

    VWAP = amount/volume 近似(量纲注记见模块 docstring)。
    """
    axis = inp.bars("close")
    closes = _values(axis)
    vwap = _vwap_values(inp)
    positioned = _per_symbol(closes, lambda c: ts_argmax(c, 30))
    ranked = _cs_rank_series(axis, positioned, inp)
    denominator = _per_symbol(ranked, lambda r: ts_decay(r, 2))
    body = {
        symbol: ew_div(vwap[symbol] - closes[symbol], denominator[symbol])
        for symbol in closes
    }
    return inp.sample(axis, body)


def _compute_alpha101_101(inp: PredefinedFactorInput) -> FactorSeriesFrame:
    """``(Close - Open) / ((High - Low) + 0.001)``。"""
    axis = inp.bars("close")
    closes = _values(axis)
    opens = _values(inp.bars("open"))
    highs = _values(inp.bars("high"))
    lows = _values(inp.bars("low"))
    body = {
        symbol: ew_div(
            closes[symbol] - opens[symbol],
            (highs[symbol] - lows[symbol]) + 0.001,
        )
        for symbol in closes
    }
    return inp.sample(axis, body)


def industry_neutralize(
    values: CrossSection,
    groups: Mapping[str, str | None],
) -> tuple[dict[str, float | None], int]:
    """行业中性化(issue #400 口径):组内去均值,**缺组标签 → 缺测 + 计数**。

    与 :func:`operators.cs_neutralize`(缺组标的归入合成组一起去均值)
    的差异是本 issue 的验收口径:缺行业数据的标的宁可缺测(可见、
    可计数、由质量门把关)也不静默归组 —— 用法 = 因子 compute 经
    ``inp.industry_groups()`` 取 ``{symbol: 行业}`` 后传入本助手。
    组标签 = 冻结发布 ``instruments.industry``(#185,stock_basic 行业
    字符串近似 research_industry_memberships 一级分组;非 WQ
    ``indneutralize`` 的 rank 中性语义——组内原始值去均值)。
    返回 ``(中性化截面, 缺组标的数)``。
    """
    grouped = {
        symbol: (values[symbol] if (groups.get(symbol) or "") else None)
        for symbol in values
    }
    missing = sum(1 for symbol in values if not (groups.get(symbol) or ""))
    return cs_neutralize(grouped, groups), missing


def _alpha_definition(
    number: int,
    *,
    title: str,
    window: int | None,
    compute: PredefinedFactorCompute,
    cross_section: bool,
    data_dependencies: tuple[str, ...],
) -> PredefinedFactorDefinition:
    """Alpha101 批次条目工厂(issue #400)。

    方向 = 文献构建约定:WQ 论文按「因子值越高 → 预期收益越高」构造
    (负号已在公式内),故统一 ``HIGHER``;个别量纲敏感因子(41/54/57)
    的方向稳健性由 IC sanity 复核(PR 清单表标注)。
    """
    return PredefinedFactorDefinition(
        name=f"alpha101_{number}",
        title=title,
        family="alpha101",
        direction=FactorPreference.HIGHER,
        signal_eligible=True,
        data_dependencies=data_dependencies,
        window=window,
        implementation_version="1",
        compute=compute,
        cross_section=cross_section,
    )


#: Alpha101 批次(tushare ``factor_list`` 圈定的 31 个;编号即 WQ 论文
#: 原始编号,缺号为论文中依赖行业/市值数据或非截面语义的未收录因子)。
#: ``cross_section=True`` = 公式含 ``Rank`` 类截面算子(构建期采样面
#: 收窄到可交易域,#380);纯时序条件公式 False。
_ALPHA101_FACTORS: tuple[PredefinedFactorDefinition, ...] = (
    _alpha_definition(
        1,
        title=(
            "Alpha101#1: Rank(Ts_ArgMax(SignedPower(IF(Returns<0, "
            "StdDev(Returns,20), Close), 2), 5)) - 0.5"
            "(负收益时段波动放大的极值位置排名)"
        ),
        window=20,
        compute=_compute_alpha101_1,
        cross_section=True,
        data_dependencies=("bars.close",),
    ),
    _alpha_definition(
        2,
        title=(
            "Alpha101#2: -1 * Corr(Rank(Delta(Log(Volume), 2)), "
            "Rank((Close-Open)/Open), 6)(量变排名与日内动量排名的负相关)"
        ),
        window=6,
        compute=_compute_alpha101_2,
        cross_section=True,
        data_dependencies=("bars.close", "bars.open", "bars.volume"),
    ),
    _alpha_definition(
        3,
        title=(
            "Alpha101#3: -1 * Corr(Rank(Open), Rank(Volume), 10)"
            "(开盘价排名与成交量排名的负相关)"
        ),
        window=10,
        compute=_compute_alpha101_3,
        cross_section=True,
        data_dependencies=("bars.open", "bars.volume"),
    ),
    _alpha_definition(
        4,
        title="Alpha101#4: -1 * Ts_Rank(Rank(Low), 9)(低价排名时序百分位的负值)",
        window=9,
        compute=_compute_alpha101_4,
        cross_section=True,
        data_dependencies=("bars.low",),
    ),
    _alpha_definition(
        5,
        title=(
            "Alpha101#5: Rank(Open - Sum(VWAP,10)/10) * "
            "(-1 * Abs(Rank(Close - VWAP)))(开盘对 VWAP 均值偏离 x 收盘对 VWAP 偏离)"
        ),
        window=10,
        compute=_compute_alpha101_5,
        cross_section=True,
        data_dependencies=(
            "bars.close",
            "bars.open",
            "bars.volume",
            "bars.amount",
        ),
    ),
    _alpha_definition(
        6,
        title=(
            "Alpha101#6: -1 * Corr(Open, Volume, 10)"
            "(开盘价与成交量的负相关;纯时序无截面算子)"
        ),
        window=10,
        compute=_compute_alpha101_6,
        cross_section=False,
        data_dependencies=("bars.open", "bars.volume"),
    ),
    _alpha_definition(
        7,
        title=(
            "Alpha101#7: IF(ADV20 < Volume, -Ts_Rank(Abs(Delta(Close,7)), 60) "
            "* Sign(Delta(Close,7)), -1)(放量日的 7 日动量符号加权)"
        ),
        window=60,
        compute=_compute_alpha101_7,
        cross_section=False,
        data_dependencies=("bars.close", "bars.volume"),
    ),
    _alpha_definition(
        8,
        title=(
            "Alpha101#8: -1 * Rank(Sum(Open,5) * Sum(Returns,5) - "
            "Delay(Sum(Open,5) * Sum(Returns,5), 10))(开盘-收益联合动量反转)"
        ),
        window=10,
        compute=_compute_alpha101_8,
        cross_section=True,
        data_dependencies=("bars.close", "bars.open"),
    ),
    _alpha_definition(
        9,
        title=(
            "Alpha101#9: IF(0 < Ts_Min(Delta(Close,1),5), Delta(Close,1), "
            "IF(Ts_Max(Delta(Close,1),5) < 0, Delta(Close,1), -Delta(Close,1)))"
            "(短期动量方向确认)"
        ),
        window=5,
        compute=_compute_alpha101_9,
        cross_section=False,
        data_dependencies=("bars.close",),
    ),
    _alpha_definition(
        10,
        title=(
            "Alpha101#10: Rank(IF(0 < Ts_Min(Delta(Close,1),4), Delta(Close,1), "
            "IF(Ts_Max(Delta(Close,1),4) < 0, Delta(Close,1), -Delta(Close,1))))"
            "(#9 的截面排名版,4 日窗)"
        ),
        window=4,
        compute=_compute_alpha101_10,
        cross_section=True,
        data_dependencies=("bars.close",),
    ),
    _alpha_definition(
        11,
        title=(
            "Alpha101#11: (Rank(Ts_Max(VWAP-Close,3)) + Rank(Ts_Min(VWAP-Close,3))) "
            "* Rank(Delta(Volume,3))(VWAP 偏离极值 x 量变排名)"
        ),
        window=3,
        compute=_compute_alpha101_11,
        cross_section=True,
        data_dependencies=("bars.close", "bars.volume", "bars.amount"),
    ),
    _alpha_definition(
        12,
        title=(
            "Alpha101#12: Sign(Delta(Volume,1)) * (-1 * Delta(Close,1))"
            "(量变方向加权的价格反转)"
        ),
        window=1,
        compute=_compute_alpha101_12,
        cross_section=False,
        data_dependencies=("bars.close", "bars.volume"),
    ),
    _alpha_definition(
        13,
        title=(
            "Alpha101#13: -1 * Rank(Covariance(Rank(Close), Rank(Volume), 5))"
            "(量价排名协方差的负值)"
        ),
        window=5,
        compute=_compute_alpha101_13,
        cross_section=True,
        data_dependencies=("bars.close", "bars.volume"),
    ),
    _alpha_definition(
        14,
        title=(
            "Alpha101#14: -1 * Rank(Delta(Returns,3)) * Corr(Open, Volume, 10)"
            "(收益加速反转 x 量价相关)"
        ),
        window=10,
        compute=_compute_alpha101_14,
        cross_section=True,
        data_dependencies=("bars.close", "bars.open", "bars.volume"),
    ),
    _alpha_definition(
        15,
        title=(
            "Alpha101#15: -1 * Sum(Rank(Corr(Rank(High), Rank(Volume), 3)), 3)"
            "(高价-量相关排名的 3 日和)"
        ),
        window=3,
        compute=_compute_alpha101_15,
        cross_section=True,
        data_dependencies=("bars.high", "bars.volume"),
    ),
    _alpha_definition(
        16,
        title=(
            "Alpha101#16: -1 * Rank(Covariance(Rank(High), Rank(Volume), 5))"
            "(高价-量排名协方差的负值)"
        ),
        window=5,
        compute=_compute_alpha101_16,
        cross_section=True,
        data_dependencies=("bars.high", "bars.volume"),
    ),
    _alpha_definition(
        17,
        title=(
            "Alpha101#17: -Rank(Ts_Rank(Close,10)) * Rank(Delta(Delta(Close,1),1)) "
            "* Rank(Ts_Rank(Volume/ADV20,5))(价格时序位置 x 二阶动量 x 相对量)"
        ),
        window=10,
        compute=_compute_alpha101_17,
        cross_section=True,
        data_dependencies=("bars.close", "bars.volume"),
    ),
    _alpha_definition(
        18,
        title=(
            "Alpha101#18: -1 * Rank(StdDev(Abs(Close-Open),5) + (Close-Open) "
            "+ Corr(Close,Open,10))(日内波动与动量组合的负值)"
        ),
        window=10,
        compute=_compute_alpha101_18,
        cross_section=True,
        data_dependencies=("bars.close", "bars.open"),
    ),
    _alpha_definition(
        19,
        title=(
            "Alpha101#19: -Sign((Close-Delay(Close,7))+Delta(Close,7)) * "
            "(1+Rank(1+Sum(Returns,250))) - Rank(X)^2, X = Corr(Rank(VWAP-Close), "
            "Rank(Volume), 12) * Corr(Rank(Close), Rank(ADV20), 12)"
            "(长周期动量符号 x VWAP 偏离-量相关联合排名)"
        ),
        window=250,
        compute=_compute_alpha101_19,
        cross_section=True,
        data_dependencies=("bars.close", "bars.volume", "bars.amount"),
    ),
    _alpha_definition(
        20,
        title=(
            "Alpha101#20: -Rank(Open-Delay(High,1)) * Rank(Open-Delay(Close,1)) "
            "* Rank(Open-Delay(Low,1))(开盘跳空三重排名反转)"
        ),
        window=1,
        compute=_compute_alpha101_20,
        cross_section=True,
        data_dependencies=(
            "bars.close",
            "bars.open",
            "bars.high",
            "bars.low",
        ),
    ),
    _alpha_definition(
        22,
        title=(
            "Alpha101#22: -Delta(Corr(High, Volume, 5), 5) * Rank(StdDev(Close, 20))"
            "(量价相关变化 x 波动排名)"
        ),
        window=20,
        compute=_compute_alpha101_22,
        cross_section=True,
        data_dependencies=("bars.close", "bars.high", "bars.volume"),
    ),
    _alpha_definition(
        23,
        title=(
            "Alpha101#23: IF(Sum(High,20)/20 < High, -Delta(High,2), 0)"
            "(突破 20 日高价均线后的短期回落)"
        ),
        window=20,
        compute=_compute_alpha101_23,
        cross_section=False,
        data_dependencies=("bars.high",),
    ),
    _alpha_definition(
        25,
        title=(
            "Alpha101#25: Rank(-1 * Returns * ADV20 * VWAP * (High - Close))"
            "(放量长上影反转)"
        ),
        window=20,
        compute=_compute_alpha101_25,
        cross_section=True,
        data_dependencies=("bars.close", "bars.high", "bars.volume", "bars.amount"),
    ),
    _alpha_definition(
        33,
        title=(
            "Alpha101#33: Rank(-1 * (1 - Open/Close))(日内开盘-收盘反向排名)"
        ),
        window=None,
        compute=_compute_alpha101_33,
        cross_section=True,
        data_dependencies=("bars.close", "bars.open"),
    ),
    _alpha_definition(
        34,
        title=(
            "Alpha101#34: Rank((1 - Rank(StdDev(Returns,2)/StdDev(Returns,5))) "
            "+ (1 - Rank(Delta(Close,1))))(短波动比与价格动量的组合排名)"
        ),
        window=5,
        compute=_compute_alpha101_34,
        cross_section=True,
        data_dependencies=("bars.close",),
    ),
    _alpha_definition(
        41,
        title=(
            "Alpha101#41: (High*Low)^0.5 - VWAP(几何均价对 VWAP 的偏离;"
            "量纲敏感因子,方向待 IC 确认)"
        ),
        window=None,
        compute=_compute_alpha101_41,
        cross_section=False,
        data_dependencies=("bars.high", "bars.low", "bars.volume", "bars.amount"),
    ),
    _alpha_definition(
        52,
        title=(
            "Alpha101#52: (-Ts_Min(Low,5) + Delay(Ts_Min(Low,5),5)) * "
            "Rank((Sum(Returns,240)-Sum(Returns,20))/220) * Ts_Rank(Volume,5)"
            "(低价改善 x 长短期动量差 x 量时序位置)"
        ),
        window=240,
        compute=_compute_alpha101_52,
        cross_section=True,
        data_dependencies=("bars.close", "bars.low", "bars.volume"),
    ),
    _alpha_definition(
        53,
        title=(
            "Alpha101#53: -Delta(((Close-Low)-(High-Close))/(Close-Low), 9)"
            "(日内位置指标的 9 日反转)"
        ),
        window=9,
        compute=_compute_alpha101_53,
        cross_section=False,
        data_dependencies=("bars.close", "bars.high", "bars.low"),
    ),
    _alpha_definition(
        54,
        title=(
            "Alpha101#54: (-1 * (Low-Close) * Open^5) / ((Low-High) * Close^5)"
            "(日内位置与开收盘比的极值组合;量纲敏感,方向待 IC 确认)"
        ),
        window=None,
        compute=_compute_alpha101_54,
        cross_section=False,
        data_dependencies=("bars.close", "bars.open", "bars.high", "bars.low"),
    ),
    _alpha_definition(
        57,
        title=(
            "Alpha101#57: -(Close - VWAP) / Decay_Linear(Rank(Ts_ArgMax(Close,30)), 2)"
            "(收盘-VWAP 偏离除以极值位置线性衰减;量纲敏感,方向待 IC 确认)"
        ),
        window=30,
        compute=_compute_alpha101_57,
        cross_section=True,
        data_dependencies=("bars.close", "bars.volume", "bars.amount"),
    ),
    _alpha_definition(
        101,
        title=(
            "Alpha101#101: (Close - Open) / ((High - Low) + 0.001)(日内动量归一)"
        ),
        window=None,
        compute=_compute_alpha101_101,
        cross_section=False,
        data_dependencies=("bars.close", "bars.open", "bars.high", "bars.low"),
    ),
)


#: 目录(裸名 → 条目)。批次 0 样板:return_Nd 动量族(4 窗口变体);
#: 批次 2(#400):Alpha101 量价 31 因子;后续批次按同一模式追加注册。
PREDEFINED_FACTORS: dict[str, PredefinedFactorDefinition] = {
    item.name: item
    for item in (
        _definition(
            "return_21d",
            title="return_21d = close / close[-21] - 1(21 根 bar 区间收益,动量)",
            family="momentum",
            window=21,
        ),
        _definition(
            "return_63d",
            title="return_63d = close / close[-63] - 1(季度动量)",
            family="momentum",
            window=63,
        ),
        _definition(
            "return_126d",
            title="return_126d = close / close[-126] - 1(半年动量)",
            family="momentum",
            window=126,
        ),
        _definition(
            "return_252d",
            title="return_252d = close / close[-252] - 1(年度动量)",
            family="momentum",
            window=252,
        ),
        *_ALPHA101_FACTORS,
    )
}


def get_predefined_factor(name: str) -> PredefinedFactorDefinition:
    """按裸名取目录条目;未知名 KeyError 附可用清单(入队/编译期秒拒用)。"""
    try:
        return PREDEFINED_FACTORS[name]
    except KeyError:
        raise KeyError(
            f"未注册的平台预置因子: {name!r};可用: {sorted(PREDEFINED_FACTORS)}"
        ) from None


def is_registered_predefined_factor(name: str) -> bool:
    """裸名(或 p_ 引用名)是否已注册。"""
    bare = name.removeprefix("p_")
    return bare in PREDEFINED_FACTORS


def predefined_factor_names() -> tuple[str, ...]:
    """全部已注册裸名(稳定排序)。"""
    return tuple(sorted(PREDEFINED_FACTORS))


def predefined_factor_commit(name: str) -> str:
    """内容寻址的 commit 锚:``predefined-<sha256[:12]>``。

    锚原料 = schema 版本 + 目录条目的**语义字段**(name / 公式依赖 /
    窗口 / 截面标记 / 实现版本);title 等文档性字段不参与(改文案不使
    序列失效)。任何语义变化(实现版本递增 / 依赖调整)→ 新锚 → 新
    series_key → 托管重建;不变 → 同参数重建命中缓存。
    """
    item = get_predefined_factor(name)
    payload = {
        "schema_version": PREDEFINED_FACTORS_SCHEMA_VERSION,
        "name": item.name,
        "data_dependencies": sorted(item.data_dependencies),
        "window": item.window,
        "cross_section": item.cross_section,
        "implementation_version": item.implementation_version,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"predefined-{digest[:12]}"


def _validate_catalog() -> None:
    """导入期目录防漂移(与 BENCHMARK_INDEX_REGISTRY 同风格)。"""
    for name, item in PREDEFINED_FACTORS.items():
        if item.name != name:
            raise ValueError(f"目录键与条目名不一致: {name} vs {item.name}")
        expected = predefined_factor_commit(name)
        if not expected.startswith("predefined-"):
            raise ValueError(f"commit 锚形态非法: {name}")


_validate_catalog()

__all__ = [
    "PREDEFINED_FACTORS",
    "PREDEFINED_FACTORS_SCHEMA_VERSION",
    "PredefinedFactorCompute",
    "PredefinedFactorDefinition",
    "get_predefined_factor",
    "industry_neutralize",
    "is_registered_predefined_factor",
    "predefined_factor_commit",
    "predefined_factor_names",
]
