"""ETF 定额定投策略 —— 定时买入固定数量,验证完整下单链路。

链路: ``on_timer`` → ``ctx.submit_order`` → 风控 → OrderManager → Broker → 成交 → 持仓更新。

为防止重复下单,每次 ``start()`` 后仅下单一次(``_ordered`` flag)。
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from finboard_core.strategy import Strategy, StrategyContext, TimerEvent
from finboard_shared.identifiers import StrategyId
from finboard_shared.models import Symbol
from finboard_shared.types import Market, OrderType, Side

logger = structlog.get_logger(__name__)


class EtfDcaStrategy(Strategy):
    """ETF 定额定投 —— 首次 ``on_timer`` 时买入指定数量,之后不再重复。"""

    def __init__(
        self,
        *,
        strategy_id: str,
        symbol_code: str,
        quantity: Decimal,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
    ) -> None:
        self._id = StrategyId(strategy_id)
        self._symbol = Symbol(code=symbol_code, market=Market.A_SHARE)
        self._quantity = quantity
        self._order_type = order_type
        self._price = price
        self._ordered = False

    @property
    def strategy_id(self) -> StrategyId:
        return self._id

    async def on_start(self, ctx: StrategyContext) -> None:
        logger.info(
            "etf_dca.started",
            strategy_id=str(self._id),
            symbol=str(self._symbol),
            quantity=str(self._quantity),
            order_type=self._order_type.value,
        )

    async def on_timer(self, event: TimerEvent, ctx: StrategyContext) -> None:
        if self._ordered:
            return
        self._ordered = True
        try:
            order = await ctx.submit_order(
                self._symbol,
                Side.BUY,
                self._quantity,
                order_type=self._order_type,
                price=self._price,
            )
            logger.info(
                "etf_dca.order_placed",
                strategy_id=str(self._id),
                client_order_id=str(order.client_order_id),
                symbol=str(self._symbol),
                quantity=str(self._quantity),
            )
        except Exception:
            logger.exception(
                "etf_dca.order_failed", strategy_id=str(self._id)
            )
