"""预置因子的输入契约与采样(issue #398,批次 0)。

因子作者只消费本模块的三个名字:

* :class:`SymbolSeries` —— 单标的按 ``available_at`` 升序的列式历史
  (日期轴 + float64 值 + 逐行 available_at);
* :class:`PredefinedFactorInput` —— 引擎装配的输入面(bars / daily_metrics
  列式取数、行业分组、决策日采样);
* :func:`sample_series_frame` —— 标准采样(把逐标的 1-D 因子序列按决策日
  对齐成 ``{date: {symbol: float | None}}`` 截面帧)。

**因子实现契约**(前缀不变性审计兜底,见 ``research_sandbox.audit``):

* 输入序列按 ``available_at`` 升序;因子必须**因果**——位置 ``i`` 的输出
  只依赖 ``<= i`` 的行(全部 ``ts_*`` 算子满足;自写变换同样受约束);
* 采样规则 = 每个决策日取该标的「``available_at <= 决策日日终`` 的最后
  一行」的因子值,停牌日自然延续最近可得值;
* ``sample`` 的输出 universe 由引擎按目录条目决定:``cross_section=True``
  的因子采样面自动收窄到可交易域(#380:截面算子的分母不得混入
  benchmark-only 标的);时序因子保留全挂载标的(消费端统一剔除基准,
  ``explicit_symbols`` 豁免照常生效)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time

import numpy as np

#: 因子序列帧:{决策日 → {symbol → 因子值或 None(缺测)}}
FactorSeriesFrame = dict[date, dict[str, float | None]]

__all__ = [
    "FactorSeriesFrame",
    "PredefinedFactorInput",
    "SymbolSeries",
    "end_of_day",
    "sample_series_frame",
]


def end_of_day(day: date) -> datetime:
    """决策日日终(UTC)——采样与审计共用的 PIT 上界(与挂载 v3 同口径)。"""
    return datetime.combine(day, time(23, 59, 59, 999999), tzinfo=UTC)


@dataclass(frozen=True)
class SymbolSeries:
    """单标的按 ``available_at`` 升序的列式历史。

    ``values`` 与 ``dates`` / ``available_at`` 逐行平行;``values`` 中
    缺测一律为 NaN(挂载读取时已归一)。``dates`` 升序且与
    ``available_at`` 同序(引擎装配时稳定排序保证)。
    """

    dates: tuple[date, ...]
    values: np.ndarray
    available_at: tuple[datetime, ...]
    _bounds: list[float] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not (len(self.dates) == len(self.available_at) == self.values.size):
            raise ValueError(
                "SymbolSeries dates/available_at/values 长度不一致: "
                f"{len(self.dates)}/{len(self.available_at)}/{self.values.size}"
            )
        object.__setattr__(
            self,
            "_bounds",
            [item.timestamp() for item in self.available_at],
        )

    def __len__(self) -> int:
        return len(self.dates)

    def asof(self, day: date) -> float:
        """``date <= day`` 的最后一行因子值;无可见行或值缺测 → NaN。"""
        position = self.position_asof(end_of_day(day))
        if position < 0:
            return math.nan
        return float(self.values[position])

    def position_asof(self, visible_until: datetime) -> int:
        """``available_at <= visible_until`` 的最后一行下标;-1 = 无可见行。"""
        import bisect

        return bisect.bisect_right(self._bounds, visible_until.timestamp()) - 1


class PredefinedFactorInput(ABC):
    """因子 ``compute`` 的输入面(引擎装配;因子作者只读)。

    * ``bars(field)`` —— 窗口挂载 bars 列式取数(字段:``close`` /
      ``open`` / ``high`` / ``low`` / ``volume`` / ``amount``);缺字段的
      标的整条序列缺测(NaN);
    * ``daily_metrics(field)`` —— daily_metrics 发布列式取数(惰性:
      因子不触碰的研究数据集零读取,#378 同精神);未挂载 → 空映射;
    * ``industry_groups()`` —— 行业分组装配(symbol → 一级行业代码,
      缺失 → ``None``,供 ``cs_neutralize``;v1 自研究发布观测装配,
      因子层允许显式分组语义见算子 docstring);
    * ``sample(per_symbol_values)`` —— 逐标的 1-D 因子序列(与该标的
      ``SymbolSeries`` 逐行对齐)→ 决策日截面帧;输出 universe 由引擎
      按目录条目决定(见模块 docstring)。
    """

    def __init__(
        self,
        *,
        factor_name: str,
        decision_dates: tuple[date, ...],
        tradable_symbols: tuple[str, ...],
        benchmark_only_symbols: frozenset[str],
    ) -> None:
        self.factor_name = factor_name
        self.decision_dates = tuple(decision_dates)
        self.tradable_symbols = tuple(tradable_symbols)
        self.benchmark_only_symbols = frozenset(benchmark_only_symbols)

    @abstractmethod
    def bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        """窗口挂载 bars 列式取数(全挂载标的,含 benchmark-only)。"""

    @abstractmethod
    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        """daily_metrics 发布列式取数(未挂载 → 空映射)。"""

    @abstractmethod
    def industry_groups(self) -> dict[str, str | None]:
        """行业分组装配(symbol → 一级行业代码;缺失 → None)。"""

    @abstractmethod
    def sample(
        self, per_symbol_values: Mapping[str, np.ndarray]
    ) -> FactorSeriesFrame:
        """逐标的 1-D 因子序列 → 决策日截面帧(契约见模块 docstring)。"""


def sample_series_frame(
    series_by_symbol: Mapping[str, SymbolSeries],
    per_symbol_values: Mapping[str, np.ndarray],
    *,
    decision_dates: tuple[date, ...],
    value_universe: tuple[str, ...] | None = None,
) -> FactorSeriesFrame:
    """标准采样(引擎与测试共用的纯函数)。

    每个决策日对 ``value_universe``(None = ``per_symbol_values`` 键序)
    逐标的取「``available_at <= 决策日日终`` 的最后一行」因子值;
    非有限值(NaN/inf)统一输出 ``None``。未知标的 / 长度不一致 fail-fast。
    """
    universe = (
        tuple(per_symbol_values)
        if value_universe is None
        else tuple(value_universe)
    )
    for symbol in universe:
        if symbol not in per_symbol_values:
            raise ValueError(f"sample 缺少标的的因子序列: {symbol}")
        series = series_by_symbol.get(symbol)
        if series is None:
            raise ValueError(f"sample 缺少标的的行情历史: {symbol}")
        if per_symbol_values[symbol].size != series.values.size:
            raise ValueError(
                f"sample 序列长度与行情历史不一致: {symbol} "
                f"({per_symbol_values[symbol].size} vs {series.values.size})"
            )
    positions: dict[tuple[str, date], int] = {}
    for symbol in universe:
        series = series_by_symbol[symbol]
        for day in decision_dates:
            positions[(symbol, day)] = series.position_asof(end_of_day(day))
    frame: FactorSeriesFrame = {}
    for day in decision_dates:
        cross: dict[str, float | None] = {}
        for symbol in universe:
            position = positions[(symbol, day)]
            if position < 0:
                cross[symbol] = None
                continue
            value = float(per_symbol_values[symbol][position])
            cross[symbol] = value if math.isfinite(value) else None
        frame[day] = cross
    return frame
