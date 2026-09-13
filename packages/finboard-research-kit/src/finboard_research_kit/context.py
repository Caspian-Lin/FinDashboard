"""因子执行协议 —— 沙箱内数据上下文(issue #216;#359 增区间协议 v2)。

``FactorContext`` 是 agent 因子代码在沙箱容器内看到的**全部**数据面:
决策时点 ``decision_at`` 之前的冻结发布行情/研究指标(PIT 由挂载内容物理
保证 —— 容器内根本不存在未来数据文件,见服务端 data_mount),叠加运行
参数。纯截面函数式:无状态、无副作用、无网络。

约定:

* ``bars``:列 ``symbol, date, open, high, low, close, volume, amount``,
  全部标的合并的长表,只含 ``date <= decision_at`` 的行;
* ``daily_metrics`` / ``financial_indicators``:冻结研究发布的 PIT 视图
  (``available_at <= decision_at``),列为发布白名单字段原样(float 化),
  对应发布缺失时为 ``None``;
* DataFrame 内容应视为只读:容器根文件系统与挂载均为只读,harness 也只
  信任自己从 parquet 重新读出的数据,因子内篡改 ctx 只会污染自身计算。

**协议 v2(issue #359,区间执行)**:``FactorSeriesContext`` 覆盖整个
决策窗口 —— 挂载(服务端 data_mount v3)物化 ``available_at <=
window_end`` 日终的全量数据(否则算不了后段日期),容器内逐日 PIT 由
``bars_view(as_of)`` / ``dataset_view(kind, as_of)`` 访问器契约承担:
``as_of`` 日终(``available_at <= as_of 当日 23:59:59.999999 UTC``)之前
的数据才可见。**契约:value[t] 只许依赖 available_at <= t 的数据**;
违反由服务端前缀不变性审计(truncation / perturbation)检出 —— 逐日
物理隔离不再是窗口模式的防线,窗口上界(window_end)仍是物理硬边界。

**数据面底座(issue #374)**:harness 以 **Arrow 常驻 + 按需截面** 装配
序列上下文 —— parquet 读为 Arrow 表(raw 内存,无 Python 对象税),
``as_of`` 过滤在 Arrow compute 内完成,仅当次访问的可见行物化为
pandas;容器内存不再随「整表进 pandas」线性放大(705 万行 daily_metrics
曾以 ~2GB 撞穿容器限额)。直接以 pandas 构造(测试 / 老代码)的行为
与 0.3.0 完全一致,两种来源构造时二选一。

**v1 回退路径惰性化(issue #378)**:#374 只覆盖了 v2 访问器路径,v1
因子在序列模式下的逐日回退仍每日**急切物化全部三个数据集**的当日
视图 —— 因子不碰的数据集也逐日付出「整段前缀进 pandas」的成本,真实
挂载(705 万行 daily_metrics)据此再次撞穿容器限额。0.3.2 起
:class:`FactorContext` 接受 ``*_factory`` 构造参(帧二选一,属性首次
访问才物化并缓存),:class:`FactorSeriesContext` 接受 ``*_loader``
(首次访问才读 parquet)—— 不触碰的数据集连文件都不读。

不在白名单的 import(subprocess/socket/os...)在静态校验(#215)与本镜像
运行环境双重拒绝。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from types import MappingProxyType
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

#: 窗口挂载(v3)在 bars / 研究数据集长表中携带的逐行可见时点列名
AVAILABLE_AT_COL = "available_at"


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    )


class FactorContext:
    """一次因子计算的只读输入(挂载内容 = decision_at 之前的世界)。

    数据字段支持两种构造来源(issue #378):直接传 pandas 帧(测试 /
    0.3.x 兼容行为),或传 ``*_factory``(harness 装配)—— factory 在
    属性**首次访问**时调用一次,结果物化并缓存。v1 因子在序列回退路径
    下由 harness 传 factory:因子不触碰的数据集完全不付物化成本(此前
    逐日急切物化曾让不碰 daily_metrics 的因子也被其 705 万行前缀帧
    撞穿容器限额)。帧与 factory 二选一,不可混装。
    """

    __slots__ = (
        "_bars_factory",
        "_bars_frame",
        "_daily_factory",
        "_daily_frame",
        "_fin_factory",
        "_fin_frame",
        "decision_at",
        "params",
        "symbols",
    )

    #: 决策时点(UTC,带时区);挂载数据的 available_at 均不晚于它
    decision_at: datetime
    #: 候选池标的代码(如 "600000.SH")
    symbols: tuple[str, ...]
    #: 运行参数(入队 payload.params 覆盖 manifest.params 的合并结果,只读)
    params: Mapping[str, Any]
    _bars_factory: Callable[[], pd.DataFrame | None] | None
    _bars_frame: pd.DataFrame | None
    _daily_factory: Callable[[], pd.DataFrame | None] | None
    _daily_frame: pd.DataFrame | None
    _fin_factory: Callable[[], pd.DataFrame | None] | None
    _fin_frame: pd.DataFrame | None

    def __init__(
        self,
        *,
        decision_at: datetime,
        symbols: tuple[str, ...],
        bars: pd.DataFrame | None = None,
        daily_metrics: pd.DataFrame | None = None,
        financial_indicators: pd.DataFrame | None = None,
        params: Mapping[str, Any] = MappingProxyType({}),
        bars_factory: Callable[[], pd.DataFrame | None] | None = None,
        daily_metrics_factory: Callable[[], pd.DataFrame | None] | None = None,
        financial_indicators_factory: (Callable[[], pd.DataFrame | None] | None) = None,
    ) -> None:
        if bars is not None and bars_factory is not None:
            raise ValueError("bars 与 bars_factory 二选一(帧与惰性来源不可混装)")
        if daily_metrics is not None and daily_metrics_factory is not None:
            raise ValueError("daily_metrics 与 daily_metrics_factory 二选一(帧与惰性来源不可混装)")
        if financial_indicators is not None and financial_indicators_factory is not None:
            raise ValueError(
                "financial_indicators 与 financial_indicators_factory 二选一(帧与惰性来源不可混装)"
            )
        set_ = object.__setattr__
        set_(self, "decision_at", decision_at)
        set_(self, "symbols", tuple(symbols))
        set_(self, "params", params)
        set_(self, "_bars_factory", bars_factory)
        set_(self, "_bars_frame", bars)
        set_(self, "_daily_factory", daily_metrics_factory)
        set_(self, "_daily_frame", daily_metrics)
        set_(self, "_fin_factory", financial_indicators_factory)
        set_(self, "_fin_frame", financial_indicators)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} 是只读上下文(不可赋值: {name})")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} 是只读上下文(不可删除: {name})")

    def _load(self, factory_slot: str, frame_slot: str) -> pd.DataFrame | None:
        """首次访问调用 factory 物化并缓存;此后直接返回缓存帧。"""
        factory: Callable[[], pd.DataFrame | None] | None = getattr(self, factory_slot)
        if factory is not None:
            object.__setattr__(self, frame_slot, factory())
            object.__setattr__(self, factory_slot, None)
        frame: pd.DataFrame | None = getattr(self, frame_slot)
        return frame

    @property
    def bars(self) -> pd.DataFrame:
        """全部标的的日线长表(见模块 docstring 列约定;惰性物化)。"""
        frame = self._load("_bars_factory", "_bars_frame")
        if frame is None:
            frame = _empty_bars()
            object.__setattr__(self, "_bars_frame", frame)
        return frame

    @property
    def daily_metrics(self) -> pd.DataFrame | None:
        """daily_metrics 研究发布视图;无对应发布时为 None(惰性物化)。"""
        return self._load("_daily_factory", "_daily_frame")

    @property
    def financial_indicators(self) -> pd.DataFrame | None:
        """financial_indicators 研究发布视图;无对应发布时 None。"""
        return self._load("_fin_factory", "_fin_frame")

    def bars_for(self, symbol: str) -> pd.DataFrame:
        """单个标的的行情子集(按 date 升序)。"""
        return self.bars[self.bars["symbol"] == symbol].sort_values("date")


@dataclass(frozen=True, slots=True)
class StrategyConstraints:
    """策略可声明的组合约束只读视图(issue #218)。

    值来自 strategy_spec.portfolio_policy(服务端在挂载清单里物化);
    策略代码只读 —— 输出仍会经服务端 #91 组合管线的硬约束截断,
    这里只是让 decide 能「看见」自己将受什么约束。
    """

    #: 单标的权重上限(策略输出超出会被管线截断,记审计)
    max_weight_per_asset: float
    #: 是否 long-only(True 时负权重会被管线截断为 0)
    long_only: bool
    #: 目标总敞口上限(gross exposure 上限)
    max_gross_exposure: float
    #: 最低现金缓冲
    min_cash_buffer: float


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """一次策略决策的只读输入(issue #218,逐日决策函数协议 v1)。

    与 :class:`FactorContext` 的差异:引擎回显**当前组合权重**
    (上一决策成交后的实际持仓市值占比,首轮为空 = 空仓)与约束视图;
    跨日路径依赖(动量持续、仓位爬坡、冷却期)由此覆盖,decide 仍是
    纯截面函数 —— 状态由引擎持有,策略只读。
    """

    #: 决策时点(UTC,带时区);挂载数据的 available_at 均不晚于它
    decision_at: datetime
    #: 候选池标的代码(本次可下目标权重的全部标的)
    symbols: tuple[str, ...]
    #: 全部标的的日线长表(列约定同 FactorContext.bars)
    bars: pd.DataFrame = field(default_factory=_empty_bars)
    #: daily_metrics 研究发布视图;无对应发布时为 None
    daily_metrics: pd.DataFrame | None = None
    #: financial_indicators 研究发布视图;无对应发布时为 None
    financial_indicators: pd.DataFrame | None = None
    #: 当前组合权重(引擎回显;index=symbol,value=市值占比,未持有/0 权重
    #: 的标的不在索引中;可能含已跌出候选池的持仓标的 —— 对其输出目标
    #: 权重会被拒,正确做法是任其归零由管线清仓)
    current_weights: pd.Series = field(default_factory=pd.Series)
    #: 组合约束只读视图(来自 strategy_spec.portfolio_policy)
    constraints: StrategyConstraints = field(
        default_factory=lambda: StrategyConstraints(
            max_weight_per_asset=1.0,
            long_only=True,
            max_gross_exposure=1.0,
            min_cash_buffer=0.0,
        )
    )
    #: 运行参数(入队 parameters 覆盖 manifest.params 的合并结果,只读)
    params: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def bars_for(self, symbol: str) -> pd.DataFrame:
        """单个标的的行情子集(按 date 升序)。"""
        return self.bars[self.bars["symbol"] == symbol].sort_values("date")


def end_of_day(day: date) -> datetime:
    """``day`` 的日终(UTC 23:59:59.999999)—— 访问器 PIT 上界。

    D1 bar 的 ``available_at`` 恒为业务日之内(如 A 股 T 日 15:30 沪时),
    因此「``available_at <= as_of 日终``」与「``as_of`` 当日数据可见」
    逐值等值,同时天然容纳 available_at 落在业务日晚间的研究指标发布。
    """
    return datetime.combine(day, time(23, 59, 59, 999999), tzinfo=UTC)


def _pit_frame(
    frame: pd.DataFrame | None,
    *,
    as_of: date,
    fallback_date_col: str,
) -> pd.DataFrame | None:
    """按 ``available_at <= as_of 日终`` 过滤;无 available_at 列时按
    业务日期列回退(D1 available_at 是日期的确定性函数,两者逐值等值)。"""
    if frame is None or frame.empty:
        return frame
    ceiling = end_of_day(as_of)
    visible: pd.DataFrame
    if AVAILABLE_AT_COL in frame.columns:
        stamps = pd.to_datetime(frame[AVAILABLE_AT_COL], utc=True)
        visible = frame.loc[stamps <= ceiling]
    else:
        col = frame[fallback_date_col]
        if isinstance(col.dtype, pd.DatetimeTZDtype) or str(col.dtype).startswith("datetime64"):
            mask = col.dt.date <= as_of
        else:
            mask = col.map(lambda d: d is not None and d <= as_of)
        visible = frame.loc[mask]
    return visible


@dataclass(frozen=True, slots=True)
class BarsView:
    """``as_of`` 时点可见的行情 PIT 视图(协议 v2)。

    列约定与 :class:`FactorContext`.`bars` 一致(symbol/date/open/high/
    low/close/volume/amount,窗口挂载 v3 另携带 available_at);只含
    ``available_at <= as_of`` 日终的行 —— 通过它看到 as_of 之后的数据
    即协议违规,由服务端前缀不变性审计检出。
    """

    as_of: date
    frame: pd.DataFrame

    def for_symbol(self, symbol: str) -> pd.DataFrame:
        """单个标的的可见行情子集(按 date 升序)。"""
        return self.frame[self.frame["symbol"] == symbol].sort_values("date")


@dataclass(frozen=True, slots=True)
class DatasetView:
    """``as_of`` 时点可见的研究数据集 PIT 视图(协议 v2)。

    ``kind``:``daily_metrics`` / ``financial_indicators``;对应发布未
    挂载时 ``frame`` 为 ``None``。列约定与 v1 的同名上下文字段一致。
    """

    kind: str
    as_of: date
    frame: pd.DataFrame | None


def _table_to_frame(table: pa.Table) -> pd.DataFrame:
    """Arrow 表物化为 pandas(镜像 harness ``_read_frame`` 的读出语义)。

    ``pd.read_parquet`` 即 pyarrow 读表 + ``to_pandas``;date32 → object
    dtype 的 ``datetime.date`` 逐值一致,bars 的 ``date`` 列随后
    ``pd.to_datetime``(与既有 harness 行为一致),其余列不动。
    """
    frame: pd.DataFrame = table.to_pandas()
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"])
    return frame


def _visible_table_to_frame(
    table: pa.Table,
    *,
    as_of: date,
    fallback_date_col: str,
) -> pd.DataFrame:
    """Arrow 底座的可见行物化(与 pandas ``frame.loc[mask]`` 语义逐值等值)。

    行内容经 :func:`_visible_from_table` 同一过滤;行索引镜像 pandas
    ``loc`` 的标签语义 —— 保留命中行在原表中的位置(过滤空洞保留),
    与 0.3.0 pandas 过滤路径的帧完全一致。
    """
    if table.num_rows == 0:
        return _table_to_frame(table)
    mask = _visibility_mask(table, as_of=as_of, fallback_date_col=fallback_date_col)
    frame = _table_to_frame(table.filter(mask))
    positions = pa.array(range(table.num_rows), type=pa.int64())
    frame.index = pd.Index(pc.filter(positions, mask).to_pylist())
    return frame


def _visibility_mask(
    table: pa.Table,
    *,
    as_of: date,
    fallback_date_col: str,
) -> pa.BooleanArray:
    """``available_at <= as_of 日终`` 的布尔掩码(#374)。

    与 :func:`_pit_frame` 逐值等值:available_at 为 null 的行不可见
    (掩码 null 行在 filter 中被丢弃,与 pandas NaT 比较为 False 一致)。
    无 available_at 列时按业务日期列回退 —— v3 挂载写出恒为 date32;
    其余类型 fail-closed(不猜测时区语义)。
    """
    if AVAILABLE_AT_COL in table.column_names:
        stamps = table.column(AVAILABLE_AT_COL)
        ceiling = pa.scalar(end_of_day(as_of), type=stamps.type)
        return pc.less_equal(stamps, ceiling)
    col = table.column(fallback_date_col)
    if not pa.types.is_date(col.type):
        raise ValueError(
            f"无 {AVAILABLE_AT_COL} 列的 Arrow 数据面仅支持 "
            f"{fallback_date_col} 为 date 列,收到 {col.type}"
            "(窗口挂载 v3 各数据集恒携带 available_at)"
        )
    return pc.less_equal(col, pa.scalar(as_of, type=col.type))


class FactorSeriesContext:
    """区间因子计算的只读输入(协议 v2,issue #359)。

    与 :class:`FactorContext` 的差异:数据面覆盖**整个窗口**
    (``available_at <= window_end`` 日终,由服务端窗口挂载物化),
    逐日 PIT 由访问器承担 —— ``bars_view`` / ``dataset_view`` 只
    返回 ``available_at <= as_of`` 日终的行。``dates`` 为平台推导后传入
    的窗口内决策日(升序),容器不自行推导。

    契约:``compute_series`` 产出的 ``value[t]`` 只许依赖
    ``available_at <= t`` 的数据;违反由前缀不变性审计检出。

    数据面来源(harness 装配,#374):**Arrow 底座** —— ``*_table`` 传入
    pyarrow 表,常驻内存为 raw Arrow(无对象税),访问器在 Arrow compute
    内做 ``as_of`` 过滤后仅把可见行物化为 pandas;pandas 字段(``bars`` /
    ``daily_metrics`` / ``financial_indicators``)成为惰性属性,首次访问
    才整表物化(0.3.0 行为兼容)。两种来源构造时**二选一**;直接以 pandas
    构造(测试 / 老代码)的访问器行为与 0.3.0 完全一致。

    ``*_loader``(issue #378):harness 以 loader 构造时,parquet 的读取
    本身也推迟到该数据集**首次被访问** —— 只用 bars 的因子不为 705 万行
    daily_metrics 付任何常驻成本。loader 与帧 / 表互斥,调用一次后缓存
    (清空 loader 槽位,不重复读盘)。
    """

    __slots__ = (
        "_bars_frame",
        "_bars_loader",
        "_bars_source",
        "_daily_frame",
        "_daily_loader",
        "_daily_source",
        "_fin_frame",
        "_fin_loader",
        "_fin_source",
        "dates",
        "params",
        "symbols",
    )

    dates: tuple[date, ...]
    symbols: tuple[str, ...]
    params: Mapping[str, Any]
    _bars_source: pa.Table | pd.DataFrame | None
    _daily_source: pa.Table | pd.DataFrame | None
    _fin_source: pa.Table | pd.DataFrame | None
    _bars_loader: Callable[[], pa.Table | pd.DataFrame | None] | None
    _daily_loader: Callable[[], pa.Table | pd.DataFrame | None] | None
    _fin_loader: Callable[[], pa.Table | pd.DataFrame | None] | None
    _bars_frame: pd.DataFrame | None
    _daily_frame: pd.DataFrame | None
    _fin_frame: pd.DataFrame | None

    def __init__(
        self,
        *,
        dates: tuple[date, ...],
        symbols: tuple[str, ...],
        bars: pd.DataFrame | None = None,
        daily_metrics: pd.DataFrame | None = None,
        financial_indicators: pd.DataFrame | None = None,
        params: Mapping[str, Any] = MappingProxyType({}),
        bars_table: pa.Table | None = None,
        daily_metrics_table: pa.Table | None = None,
        financial_indicators_table: pa.Table | None = None,
        bars_loader: (Callable[[], pa.Table | pd.DataFrame | None] | None) = None,
        daily_metrics_loader: (Callable[[], pa.Table | pd.DataFrame | None] | None) = None,
        financial_indicators_loader: (Callable[[], pa.Table | pd.DataFrame | None] | None) = None,
    ) -> None:
        if bars_table is not None and bars is not None:
            raise ValueError("bars 与 bars_table 二选一(Arrow 底座与 pandas 兼容来源不可混装)")
        if daily_metrics_table is not None and daily_metrics is not None:
            raise ValueError(
                "daily_metrics 与 daily_metrics_table 二选一(Arrow 底座与 pandas 兼容来源不可混装)"
            )
        if financial_indicators_table is not None and financial_indicators is not None:
            raise ValueError(
                "financial_indicators 与 financial_indicators_table 二选一"
                "(Arrow 底座与 pandas 兼容来源不可混装)"
            )
        if bars_loader is not None and (bars is not None or bars_table is not None):
            raise ValueError("bars_loader 与 bars/bars_table 二选一(惰性读取不可与既有来源混装)")
        if daily_metrics_loader is not None and (
            daily_metrics is not None or daily_metrics_table is not None
        ):
            raise ValueError(
                "daily_metrics_loader 与 daily_metrics(_table) 二选一(惰性读取不可与既有来源混装)"
            )
        if financial_indicators_loader is not None and (
            financial_indicators is not None or financial_indicators_table is not None
        ):
            raise ValueError(
                "financial_indicators_loader 与 financial_indicators(_table)"
                " 二选一(惰性读取不可与既有来源混装)"
            )
        set_ = object.__setattr__
        set_(self, "dates", tuple(dates))
        set_(self, "symbols", tuple(symbols))
        set_(self, "params", params)
        set_(
            self,
            "_bars_source",
            bars_table if bars_table is not None else (bars if bars is not None else _empty_bars()),
        )
        set_(
            self,
            "_daily_source",
            daily_metrics_table if daily_metrics_table is not None else daily_metrics,
        )
        set_(
            self,
            "_fin_source",
            financial_indicators_table
            if financial_indicators_table is not None
            else financial_indicators,
        )
        set_(self, "_bars_loader", bars_loader)
        set_(self, "_daily_loader", daily_metrics_loader)
        set_(self, "_fin_loader", financial_indicators_loader)
        set_(self, "_bars_frame", None)
        set_(self, "_daily_frame", None)
        set_(self, "_fin_frame", None)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} 是只读上下文(不可赋值: {name})")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} 是只读上下文(不可删除: {name})")

    def __repr__(self) -> str:
        if self._bars_loader is not None:
            base = "lazy"
        elif isinstance(self._bars_source, pa.Table):
            base = "arrow"
        else:
            base = "pandas"
        return (
            f"{type(self).__name__}(dates={len(self.dates)}, "
            f"symbols={len(self.symbols)}, bars_source={base})"
        )

    # ---- 惰性 pandas 兼容字段(#374:Arrow 底座下首次访问才整表物化) ----

    def _ensure(self, loader_slot: str, source_slot: str) -> Any:
        """loader 存在则首次访问读表并缓存(清空 loader,不重复读盘)。"""
        loader: Callable[[], pa.Table | pd.DataFrame | None] | None = getattr(self, loader_slot)
        if loader is not None:
            object.__setattr__(self, source_slot, loader())
            object.__setattr__(self, loader_slot, None)
        return getattr(self, source_slot)

    def _materialize(
        self, source: pa.Table | pd.DataFrame | None, cache_slot: str
    ) -> pd.DataFrame | None:
        """按来源物化 pandas 并缓存;pandas 来源原样返回(无需缓存)。"""
        if isinstance(source, pd.DataFrame) or source is None:
            return source
        frame = _table_to_frame(source)
        object.__setattr__(self, cache_slot, frame)
        return frame

    @property
    def bars(self) -> pd.DataFrame:
        """窗口全量行情长表(惰性;Arrow 底座下首次访问才物化)。"""
        if self._bars_frame is not None:
            return self._bars_frame
        source = self._ensure("_bars_loader", "_bars_source")
        frame = self._materialize(source, "_bars_frame")
        if frame is None:
            return _empty_bars()
        return frame

    @property
    def daily_metrics(self) -> pd.DataFrame | None:
        """daily_metrics 研究发布窗口视图(惰性;无对应发布时为 None)。"""
        if self._daily_frame is None:
            source = self._ensure("_daily_loader", "_daily_source")
            return self._materialize(source, "_daily_frame")
        return self._daily_frame

    @property
    def financial_indicators(self) -> pd.DataFrame | None:
        """financial_indicators 研究发布窗口视图(惰性;无对应发布时 None)。"""
        if self._fin_frame is None:
            source = self._ensure("_fin_loader", "_fin_source")
            return self._materialize(source, "_fin_frame")
        return self._fin_frame

    # ---- 访问器(逐日 PIT 契约入口) ----

    def _visible(self, source: Any, *, as_of: date, fallback_date_col: str) -> pd.DataFrame | None:
        """按来源分派 PIT 过滤:Arrow 底座走 compute,pandas 走既有实现。"""
        if source is None:
            return None
        if isinstance(source, pd.DataFrame):
            return _pit_frame(source, as_of=as_of, fallback_date_col=fallback_date_col)
        return _visible_table_to_frame(source, as_of=as_of, fallback_date_col=fallback_date_col)

    def bars_view(self, as_of: date) -> BarsView:
        """``as_of`` 日终时点可见的行情视图(PIT 契约入口)。

        Arrow 底座下每次调用都把当次可见行物化为一个新 pandas 帧(不缓存
        —— 按日缓存会让内存随窗口长度二次增长);同一 ``as_of`` 的多次
        调用应在逐日循环外复用视图变量。
        """
        source = self._ensure("_bars_loader", "_bars_source")
        visible = self._visible(source, as_of=as_of, fallback_date_col="date")
        return BarsView(
            as_of=as_of,
            frame=visible if visible is not None else pd.DataFrame(),
        )

    def dataset_view(self, kind: str, as_of: date) -> DatasetView:
        """``as_of`` 日终时点可见的研究数据集视图。

        ``kind``:``daily_metrics`` / ``financial_indicators``;未挂载的
        kind 返回 ``frame=None`` 的视图(因子代码应按缺数据降级)。
        """
        if kind == "daily_metrics":
            source = self._ensure("_daily_loader", "_daily_source")
            frame = self._visible(source, as_of=as_of, fallback_date_col="trade_date")
        elif kind == "financial_indicators":
            source = self._ensure("_fin_loader", "_fin_source")
            frame = self._visible(
                source,
                as_of=as_of,
                fallback_date_col="announcement_date",
            )
        else:
            raise ValueError(
                f"未知数据集 kind {kind!r}(允许: daily_metrics / financial_indicators)"
            )
        return DatasetView(kind=kind, as_of=as_of, frame=frame)


__all__ = [
    "AVAILABLE_AT_COL",
    "BarsView",
    "DatasetView",
    "FactorContext",
    "FactorSeriesContext",
    "StrategyConstraints",
    "StrategyContext",
    "end_of_day",
]
