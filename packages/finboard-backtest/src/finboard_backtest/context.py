"""``BacktestContext`` —— 回测中策略可用的安全 API。

与 :class:`finboard_core.strategy.StrategyContext` 同构(相同公开方法),
但**不**经过 OrderManager / DB —— 直接路由到 ``BacktestBroker``。

策略代码在回测和实盘中完全一致,无需感知运行环境。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from finboard_backtest.broker import BacktestBroker
from finboard_backtest.clock import SimulatedClock
from finboard_shared.identifiers import (
    AccountId,
    StrategyId,
    generate_client_order_id,
)
from finboard_shared.models import Account, Order, Position, Symbol
from finboard_shared.types import BrokerKind, OrderType, Side


class BacktestContext:
    """回测策略上下文 —— 与 ``StrategyContext`` 接口一致。

    策略通过此对象下单 / 查持仓 / 查账户,所有操作直达 ``BacktestBroker``,
    无 DB 持久化、无风控拦截(回测不模拟实盘风控)。
    """

    def __init__(
        self,
        *,
        strategy_id: StrategyId,
        broker: BacktestBroker,
        clock: SimulatedClock,
        account_id: AccountId,
    ) -> None:
        self._strategy_id = strategy_id
        self._broker = broker
        self._clock = clock
        self._account_id = account_id

    @property
    def strategy_id(self) -> StrategyId:
        return self._strategy_id

    @property
    def now(self) -> datetime:
        """当前回测时间(由 SimulatedClock 驱动)。"""
        return self._clock.now()

    async def submit_order(
        self,
        symbol: Symbol,
        side: Side,
        quantity: Decimal,
        *,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
    ) -> Order:
        """提交订单 → BacktestBroker 撮合。"""
        cid = generate_client_order_id()
        order = Order(
            client_order_id=cid,
            account_id=self._account_id,
            broker_kind=BrokerKind.BACKTEST,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            strategy_id=self._strategy_id,
            price=price,
        )
        await self._broker.place_order(order)
        return replace(order)

    async def cancel_order(self, client_order_id: str) -> None:
        await self._broker.cancel_order(client_order_id)

    async def list_positions(self) -> list[Position]:
        return await self._broker.query_positions()

    async def get_account(self) -> Account | None:
        return await self._broker.query_account()
