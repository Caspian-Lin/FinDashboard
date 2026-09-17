"""预置因子的输入契约与采样(issue #398,批次 0)。

因子作者只消费本模块的三个名字:

* :class:`SymbolSeries` —— 单标的按 ``available_at`` 升序的列式历史
  (日期轴 + float64 值 + 逐行 available_at);
* :class:`PredefinedFactorInput` —— 引擎装配的输入面(bars / daily_metrics
  列式取数、公告频率研究数据集取数(#401/#402)、分红事件史取数(#402)、
  行业分组、决策日采样);
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
    "DividendEventHistory",
    "FactorSeriesFrame",
    "PredefinedFactorInput",
    "SymbolSeries",
    "end_of_day",
    "sample_series_frame",
]


def end_of_day(day: date) -> datetime:
    """决策日日终(UTC)——采样与审计共用的 PIT 上界(与挂载 v3 同口径)。"""
    return datetime.combine(day, time(23, 59, 59, 999999), tzinfo=UTC)


def _bounds_from(available_at: tuple[datetime, ...]) -> np.ndarray:
    """``available_at`` → float64 秒界数组(#462)。

    ``position_asof`` / ``visible_rows`` 的 searchsorted(side="right") 与
    bisect_right 同语义;数组替代逐行 float 对象列表,全历史窗口(1500 万
    行)省 ~480MB 对象内存。
    """
    return np.fromiter(
        (item.timestamp() for item in available_at),
        dtype=np.float64,
        count=len(available_at),
    )


@dataclass(frozen=True)
class DividendEventHistory:
    """单标的的分红进展事件史(issue #402,``dividend_events`` 取数口)。

    分红是**事件型明细**而非财报序列:同一 ``report_period``(分红年度)
    有预案 / 股东大会通过 / 实施多条进展行,``available_at`` 升序排列;
    精确股息率因子据此做「除权除息日对齐」的滚动 12 月聚合(见
    ``registry._dps_ttm``)。行序与 :class:`SymbolSeries` 同契约:按
    ``available_at`` 升序(引擎装配时稳定排序保证)。

    * ``cash_div`` —— 每股现金股利(税前,元/股),缺测 NaN;
    * ``ex_date`` —— 除权除息日(未到实施阶段为 None,不可按除息归属);
    * ``report_periods`` —— 分红年度(去重聚合键:同年度多进展行取
      决策日可见的最新一行)。
    """

    announcement_dates: tuple[date, ...]
    available_at: tuple[datetime, ...]
    report_periods: tuple[date | None, ...]
    ex_dates: tuple[date | None, ...]
    cash_div: np.ndarray
    _bounds: np.ndarray = field(default_factory=lambda: np.empty(0), repr=False, compare=False)

    def __post_init__(self) -> None:
        n = len(self.announcement_dates)
        if not (n == len(self.available_at) == len(self.report_periods) == len(self.ex_dates)):
            raise ValueError(
                "DividendEventHistory 行数不一致: "
                f"{n}/{len(self.available_at)}/{len(self.report_periods)}/"
                f"{len(self.ex_dates)}"
            )
        if self.cash_div.size != n:
            raise ValueError(
                "DividendEventHistory cash_div 长度不一致: "
                f"{self.cash_div.size} vs {n}"
            )
        object.__setattr__(self, "_bounds", _bounds_from(self.available_at))

    def __len__(self) -> int:
        return len(self.announcement_dates)

    def visible_rows(self, day: date) -> range:
        """决策日可见行下标(``available_at <= 决策日日终``,前缀)。"""
        return range(
            int(np.searchsorted(self._bounds, end_of_day(day).timestamp(), side="right"))
        )


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
    _bounds: np.ndarray = field(default_factory=lambda: np.empty(0), repr=False, compare=False)

    def __post_init__(self) -> None:
        if not (len(self.dates) == len(self.available_at) == self.values.size):
            raise ValueError(
                "SymbolSeries dates/available_at/values 长度不一致: "
                f"{len(self.dates)}/{len(self.available_at)}/{self.values.size}"
            )
        object.__setattr__(self, "_bounds", _bounds_from(self.available_at))

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
        return (
            int(np.searchsorted(self._bounds, visible_until.timestamp(), side="right"))
            - 1
        )


class PredefinedFactorInput(ABC):
    """因子 ``compute`` 的输入面(引擎装配;因子作者只读)。

    * ``bars(field)`` —— 窗口挂载 bars 列式取数(字段:``close`` /
      ``open`` / ``high`` / ``low`` / ``volume`` / ``amount``);缺字段的
      标的整条序列缺测(NaN);
    * ``index_bars(field)`` —— 基准(不可撮合)标的行情(v1 派生自
      ``bars`` 按 ``benchmark_only_symbols`` 过滤,Risk 族市场收益用,
      issue #399);
    * ``daily_metrics(field)`` —— daily_metrics 发布列式取数(惰性:
      因子不触碰的研究数据集零读取,#378 同精神);未挂载 → 空映射;
    * ``financial_indicators(field)`` —— financial_indicators 发布的
      **公告序列**取数(#401):每行 = 一次公告修订,``dates`` 为公告日、
      ``available_at`` 为 PIT 可见时刻(公告日次日零点,上海时区),
      序列按 available_at 升序;``sample`` 按「决策日可见的最近一次
      公告」取值 —— 财务因子天然是公告频率的**步进函数**(公告间持仓
      上一期值,停牌日自然延续);同一报告期的修订(多次公告)各自成
      行,采样取修订后最新值;未挂载 → 空映射;
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

    def index_bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        """基准(不可撮合)标的的行情列式取数(issue #399,Risk 族市场收益)。

        v1 派生实现:#256/#341 链路下指数行情与候选池**同处一份 mixed
        bars 主发布**,挂载没有独立 index 数据集——本方法按
        ``benchmark_only_symbols`` 过滤 ``bars(field)``(指数 / 期货主连;
        消费具体指数的因子按代码自取,如 ``000300.SH``)。发布不含指数
        → 空映射 → 因子缺测(None,fail-visible;配合 registry 的
        ``min_history_bars`` 覆盖起点声明在入队期具名拒绝)。

        数据依赖声明形态 ``"index_bars.close"``(纯声明:引擎不加载
        独立数据集,commit 锚随依赖变化)。带默认实现保持向批次 0
        子类兼容;需要独立加载路径时子类覆写。
        """
        benchmark = self.benchmark_only_symbols
        return {
            symbol: series
            for symbol, series in self.bars(field).items()
            if symbol in benchmark
        }

    @abstractmethod
    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        """daily_metrics 发布列式取数(未挂载 → 空映射)。"""

    @abstractmethod
    def financial_indicators(self, field: str) -> dict[str, SymbolSeries]:
        """financial_indicators 发布公告序列取数(#401;未挂载 → 空映射)。

        序列 = 该标的的公告修订史(每行一次公告,按 available_at 升序);
        ``sample`` 经 ``position_asof`` 天然实现「决策日可见的最近一次
        公告」的步进取值。
        """

    @abstractmethod
    def research_dataset(self, kind: str, field: str) -> dict[str, SymbolSeries]:
        """公告频率研究数据集的通用列式取数(issue #402;未挂载 → 空映射)。

        ``kind`` 为发布数据集 kind(``financial_indicators`` /
        ``income_statements`` / ``balance_sheets`` / ``cashflow_statements``
        / ``dividends``),与目录条目 ``data_dependencies`` 的
        ``"<kind>.<field>"`` 形态同构(#401 的 ``financial_indicators(field)``
        即 ``research_dataset("financial_indicators", field)``)。序列契约与
        :meth:`financial_indicators` 一致:行日期轴 = ``announcement_date``,
        PIT 走逐行 ``available_at``,采样 = 「决策日可见的最近一次公告」
        步进函数;同一报告期的修订(多次公告)各自成行。bars /
        daily_metrics 有专属取数口,传入即具名拒绝。
        """

    @abstractmethod
    def dividend_events(self) -> dict[str, DividendEventHistory]:
        """dividends 发布的分红事件史取数(issue #402;未挂载 → 空映射)。

        分红是事件型明细(进展行而非单值序列),与 :meth:`research_dataset`
        的单字段序列不同:精确股息率需要逐行 ``ex_date``(除权除息日)与
        ``cash_div``(每股现金股利)成对聚合,故给专用取数口。行序按
        ``available_at`` 升序,PIT 过滤经 :meth:`DividendEventHistory.visible_rows`。
        """

    @abstractmethod
    def industry_groups(self) -> dict[str, str | None]:
        """行业分组装配(symbol → 一级行业代码;缺失 → None)。"""

    @abstractmethod
    def sample(
        self,
        series_by_symbol: Mapping[str, SymbolSeries],
        per_symbol_values: Mapping[str, np.ndarray],
    ) -> FactorSeriesFrame:
        """逐标的 1-D 因子序列 → 决策日截面帧(契约见模块 docstring)。

        ``series_by_symbol`` 是值对齐的序列轴(时序因子传 ``bars(field)``、
        daily 因子传 ``daily_metrics(field)``);输出 universe 由引擎按
        目录条目决定(cross_section → 可交易域)。
        """


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
