"""因子执行协议 v1 —— 沙箱内数据上下文(issue #216)。

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

不在白名单的 import(subprocess/socket/os...)在静态校验(#215)与本镜像
运行环境双重拒绝。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

import pandas as pd


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


__all__ = ["FactorContext", "StrategyConstraints", "StrategyContext"]
