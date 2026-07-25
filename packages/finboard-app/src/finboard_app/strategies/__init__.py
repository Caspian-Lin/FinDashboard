"""策略注册表 —— kind → 策略类。

通过 :func:`create_strategy` 按 ``kind`` 实例化策略,
配合 YAML 配置实现声明式策略加载。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from finboard_app.strategies.etf_dca import EtfDcaStrategy
from finboard_app.strategies.ma_cross import MaCrossStrategy
from finboard_app.strategies.periodic_query import PeriodicQueryStrategy
from finboard_core.strategy import Strategy

_REGISTRY: dict[str, Callable[..., Strategy]] = {
    "periodic_query": PeriodicQueryStrategy,
    "etf_dca": EtfDcaStrategy,
    "ma_cross": MaCrossStrategy,
}


def create_strategy(kind: str, strategy_id: str, **params: Any) -> Strategy:
    """根据 ``kind`` 创建策略实例。"""
    factory = _REGISTRY.get(kind)
    if factory is None:
        raise ValueError(f"未知策略类型: {kind}")
    return factory(strategy_id=strategy_id, **params)
