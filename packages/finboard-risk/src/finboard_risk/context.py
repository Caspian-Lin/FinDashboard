"""风控上下文依赖(Protocol)。

风控需要查询"当前持仓、当前活动订单数、当日累计买入金额"等信息,
但这些数据真值在 persistence / broker;为了避免 risk 反向依赖这些包,
定义一组 Protocol 由调用方(kernel / app)实现并注入。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from finboard_shared.models import Account, Position


@runtime_checkable
class RiskContext(Protocol):
    """风控依赖的外部只读上下文。"""

    async def list_positions(self) -> list[Position]:
        """当前本地持仓(用于仓位上限校验)。"""
        ...

    async def get_account(self) -> Account | None:
        """当前账户快照(用于资金上限校验)。"""
        ...

    async def count_active_orders(self) -> int:
        """当前活动订单数(用于 max_active_orders 校验)。"""
        ...

    async def daily_buy_value(self) -> Decimal:
        """当日已累计买入金额(用于 max_daily_buy_value 校验)。"""
        ...
