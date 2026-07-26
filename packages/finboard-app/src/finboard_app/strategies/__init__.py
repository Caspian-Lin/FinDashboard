"""经过测试的内置策略注册表。

注册表同时声明策略类、参数 schema 与运行能力。它不支持动态导入或执行用户代码。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from finboard_app.strategies.etf_dca import EtfDcaStrategy
from finboard_app.strategies.ma_cross import MaCrossStrategy
from finboard_app.strategies.periodic_query import PeriodicQueryStrategy
from finboard_app.strategies.schema import (
    EtfDcaParams,
    MaCrossParams,
    PeriodicQueryParams,
    StrategyParams,
)
from finboard_core.strategy import Strategy


@dataclass(frozen=True)
class StrategyDefinition:
    """一个内置策略及其可公开的配置契约。"""

    kind: str
    name: str
    description: str
    strategy_class: Callable[..., Strategy]
    params_model: type[StrategyParams]
    supports_backtest: bool


_REGISTRY: dict[str, StrategyDefinition] = {
    "periodic_query": StrategyDefinition(
        kind="periodic_query",
        name="定时账户查询",
        description="定时读取账户和持仓快照,用于验证查询链路与策略生命周期。",
        strategy_class=PeriodicQueryStrategy,
        params_model=PeriodicQueryParams,
        supports_backtest=False,
    ),
    "etf_dca": StrategyDefinition(
        kind="etf_dca",
        name="ETF 定额定投",
        description="首次定时回调时按固定数量买入指定 ETF,用于验证完整交易链路。",
        strategy_class=EtfDcaStrategy,
        params_model=EtfDcaParams,
        supports_backtest=False,
    ),
    "ma_cross": StrategyDefinition(
        kind="ma_cross",
        name="双均线交叉",
        description="短期均线上穿长期均线时买入、下穿时卖出的多标的趋势策略。",
        strategy_class=MaCrossStrategy,
        params_model=MaCrossParams,
        supports_backtest=True,
    ),
}


def list_strategy_definitions() -> tuple[StrategyDefinition, ...]:
    """返回稳定排序的内置策略定义。"""
    return tuple(_REGISTRY.values())


def get_strategy_definition(kind: str) -> StrategyDefinition:
    """按 kind 获取策略定义。"""
    definition = _REGISTRY.get(kind)
    if definition is None:
        raise ValueError(f"未知策略类型: {kind}")
    return definition


def validate_strategy_params(kind: str, params: dict[str, Any]) -> BaseModel:
    """使用策略自己的 schema 校验并规范化参数。"""
    definition = get_strategy_definition(kind)
    return definition.params_model.model_validate(params)


def create_strategy(kind: str, strategy_id: str, **params: Any) -> Strategy:
    """校验参数后实例化经过注册的内置策略。"""
    definition = get_strategy_definition(kind)
    validated = definition.params_model.model_validate(params)
    return definition.strategy_class(
        strategy_id=strategy_id,
        **validated.model_dump(),
    )
