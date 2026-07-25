"""均线交叉策略(MA Cross)—— 第一个信号驱动策略。

经典双均线交叉:

* **金叉**:短期均线上穿长期均线 → 全仓买入
* **死叉**:短期均线下穿长期均线 → 全仓卖出

每次调仓到目标仓位(满仓或空仓),不留部分仓位。
策略通过 ``on_market_data`` 消费 Bar,使用 ``ctx.submit_order`` 下单。

该策略同时用于:

1. 验证回测引擎端到端链路(数据→信号→下单→撮合→绩效);
2. 作为后续复杂策略的基础模板。
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

import structlog

from finboard_broker.market_base import MarketDataEvent, MarketDataEventType
from finboard_core.strategy import Strategy, StrategyContext
from finboard_shared.identifiers import StrategyId
from finboard_shared.models import Symbol
from finboard_shared.types import Market, OrderType, Side

logger = structlog.get_logger(__name__)


class MaCrossStrategy(Strategy):
    """双均线交叉策略。

    :param short_window: 短期均线周期(如 5)
    :param long_window:  长期均线周期(如 20),必须 > short_window
    :param symbol_code:  标的代码(如 ``510300.SH``)
    :param max_position_pct: 最大仓位比例(如 0.95 = 留 5% 现金)
    """

    def __init__(
        self,
        *,
        strategy_id: str,
        symbol_code: str = "510300.SH",
        short_window: int = 5,
        long_window: int = 20,
        max_position_pct: float = 0.95,
    ) -> None:
        self._id = StrategyId(strategy_id)
        self._symbol = Symbol(code=symbol_code, market=Market.A_SHARE)
        self._short_window = short_window
        self._long_window = long_window
        self._max_position_pct = Decimal(str(max_position_pct))

        if short_window >= long_window:
            raise ValueError("short_window 必须小于 long_window")

        self._prices: deque[Decimal] = deque(maxlen=long_window)
        self._prev_short_ma: Decimal | None = None
        self._prev_long_ma: Decimal | None = None
        self._current_position: Decimal = Decimal("0")
        self._target_position: Decimal = Decimal("0")

    @property
    def strategy_id(self) -> StrategyId:
        return self._id

    async def on_start(self, ctx: StrategyContext) -> None:
        logger.info(
            "ma_cross.started",
            strategy_id=str(self._id),
            symbol=str(self._symbol),
            short=self._short_window,
            long=self._long_window,
        )

    async def on_market_data(
        self, event: MarketDataEvent, ctx: StrategyContext
    ) -> None:
        if event.type is not MarketDataEventType.BAR or event.bar is None:
            return
        if event.bar.symbol.code != self._symbol.code:
            return

        close = event.bar.close
        self._prices.append(close)

        if len(self._prices) < self._long_window:
            return

        short_ma = self._sma(self._short_window)
        long_ma = self._sma(self._long_window)

        if self._prev_short_ma is not None and self._prev_long_ma is not None:
            crossed_up = (
                self._prev_short_ma <= self._prev_long_ma and short_ma > long_ma
            )
            crossed_down = (
                self._prev_short_ma >= self._prev_long_ma and short_ma < long_ma
            )

            if crossed_up:
                await self._go_long(ctx, close)
            elif crossed_down:
                await self._go_short(ctx, close)

        self._prev_short_ma = short_ma
        self._prev_long_ma = long_ma

    def _sma(self, period: int) -> Decimal:
        """计算简单移动平均。"""
        values = list(self._prices)[-period:]
        return sum(values) / Decimal(len(values))

    async def _go_long(self, ctx: StrategyContext, price: Decimal) -> None:
        """金叉 → 全仓买入。"""
        account = await ctx.get_account()
        if account is None:
            return

        target_value = account.cash * self._max_position_pct
        target_qty = self._calc_quantity(target_value, price)

        if target_qty <= self._current_position:
            return  # 已在目标仓位

        buy_qty = target_qty - self._current_position
        buy_qty = self._round_lot(buy_qty)
        if buy_qty <= 0:
            return

        logger.info(
            "ma_cross.gold_cross",
            strategy_id=str(self._id),
            price=str(price),
            buy_qty=str(buy_qty),
        )
        try:
            await ctx.submit_order(
                self._symbol,
                Side.BUY,
                buy_qty,
                order_type=OrderType.MARKET,
            )
            self._current_position += buy_qty
        except Exception:
            logger.exception("ma_cross.buy_failed")

    async def _go_short(self, ctx: StrategyContext, price: Decimal) -> None:
        """死叉 → 全仓卖出。"""
        if self._current_position <= 0:
            return

        sell_qty = self._round_lot(self._current_position)
        if sell_qty <= 0:
            return

        logger.info(
            "ma_cross.death_cross",
            strategy_id=str(self._id),
            price=str(price),
            sell_qty=str(sell_qty),
        )
        try:
            await ctx.submit_order(
                self._symbol,
                Side.SELL,
                sell_qty,
                order_type=OrderType.MARKET,
            )
            self._current_position -= sell_qty
        except Exception:
            logger.exception("ma_cross.sell_failed")

    @staticmethod
    def _calc_quantity(value: Decimal, price: Decimal) -> Decimal:
        if price <= 0:
            return Decimal("0")
        return value / price

    @staticmethod
    def _round_lot(qty: Decimal) -> Decimal:
        """向下取整到 100 股。"""
        lots = qty // 100
        return lots * 100
