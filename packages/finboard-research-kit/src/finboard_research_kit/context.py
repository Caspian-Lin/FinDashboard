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

不在白名单的 import(subprocess/socket/os...)在静态校验(#215)与本镜像
运行环境双重拒绝。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from types import MappingProxyType
from typing import Any

import pandas as pd

#: 窗口挂载(v3)在 bars / 研究数据集长表中携带的逐行可见时点列名
AVAILABLE_AT_COL = "available_at"


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["symbol", "date", "open", "high", "low", "close",
                 "volume", "amount"]
    )


@dataclass(frozen=True, slots=True)
class FactorContext:
    """一次因子计算的只读输入(挂载内容 = decision_at 之前的世界)。"""

    #: 决策时点(UTC,带时区);挂载数据的 available_at 均不晚于它
    decision_at: datetime
    #: 候选池标的代码(如 "600000.SH")
    symbols: tuple[str, ...]
    #: 全部标的的日线长表(见模块 docstring 列约定)
    bars: pd.DataFrame = field(default_factory=_empty_bars)
    #: daily_metrics 研究发布视图;无对应发布时为 None
    daily_metrics: pd.DataFrame | None = None
    #: financial_indicators 研究发布视图;无对应发布时为 None
    financial_indicators: pd.DataFrame | None = None
    #: 运行参数(入队 payload.params 覆盖 manifest.params 的合并结果,只读)
    params: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

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
        if isinstance(col.dtype, pd.DatetimeTZDtype) or str(col.dtype).startswith(
            "datetime64"
        ):
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


@dataclass(frozen=True, slots=True)
class FactorSeriesContext:
    """区间因子计算的只读输入(协议 v2,issue #359)。

    与 :class:`FactorContext` 的差异:数据面覆盖**整个窗口**
    (``available_at <= window_end`` 日终,由服务端窗口挂载物化),
    逐日 PIT 由访问器承担 —— ``bars_view(as_of)`` / ``dataset_view`` 只
    返回 ``available_at <= as_of`` 日终的行。``dates`` 为平台推导后传入
    的窗口内决策日(升序),容器不自行推导。

    契约:``compute_series`` 产出的 ``value[t]`` 只许依赖
    ``available_at <= t`` 的数据;违反由前缀不变性审计检出。
    """

    #: 窗口内决策日(升序;平台推导后传入)
    dates: tuple[date, ...]
    #: 候选池标的代码
    symbols: tuple[str, ...]
    #: 窗口全量行情长表(含 window_end 之前的数据;列约定同 FactorContext,
    #: 窗口挂载 v3 另携带逐行 available_at)
    bars: pd.DataFrame = field(default_factory=_empty_bars)
    #: daily_metrics 研究发布窗口视图;无对应发布时为 None
    daily_metrics: pd.DataFrame | None = None
    #: financial_indicators 研究发布窗口视图;无对应发布时为 None
    financial_indicators: pd.DataFrame | None = None
    #: 运行参数(入队 payload.params 覆盖 manifest.params 的合并结果,只读)
    params: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def bars_view(self, as_of: date) -> BarsView:
        """``as_of`` 日终时点可见的行情视图(PIT 契约入口)。"""
        visible = _pit_frame(self.bars, as_of=as_of, fallback_date_col="date")
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
            frame = _pit_frame(
                self.daily_metrics, as_of=as_of, fallback_date_col="trade_date"
            )
        elif kind == "financial_indicators":
            frame = _pit_frame(
                self.financial_indicators,
                as_of=as_of,
                fallback_date_col="announcement_date",
            )
        else:
            raise ValueError(
                f"未知数据集 kind {kind!r}"
                "(允许: daily_metrics / financial_indicators)"
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
