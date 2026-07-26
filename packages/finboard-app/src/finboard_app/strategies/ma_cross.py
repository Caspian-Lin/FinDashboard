"""均线交叉策略(MA Cross)—— 多标的信号驱动策略。

经典双均线交叉,支持同时跟踪任意数量的标的:

* **金叉**:短期均线上穿长期均线 → 买入该标的(等权分配)
* **死叉**:短期均线下穿长期均线 → 卖出该标的全部持仓

资金分配:每个标的目标仓位 = ``总权益 * max_position_pct / N``(N = 已发现的标的数)。
当目标股数不足 1 手(100 股)时自动跳过。

策略通过 ``on_market_data`` 消费 Bar,使用 ``ctx.submit_order`` 下单。
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

import structlog

from finboard_broker.market_base import MarketDataEvent, MarketDataEventType
from finboard_core.strategy import Strategy, StrategyContext
from finboard_shared.identifiers import StrategyId
from finboard_shared.models import Symbol
from finboard_shared.types import OrderType, Side

logger = structlog.get_logger(__name__)


class MaCrossStrategy(Strategy):
    """双均线交叉策略(多标的)。

    :param short_window: 短期均线周期(如 5)
    :param long_window:  长期均线周期(如 20),必须 > short_window
    :param max_position_pct: 最大总仓位比例(如 0.95 = 留 5% 现金)
    """

    def __init__(
        self,
        *,
        strategy_id: str,
        short_window: int = 5,
        long_window: int = 20,
        max_position_pct: float = 0.95,
        symbol_code: str | None = None,  # 向后兼容,忽略
    ) -> None:
        self._id = StrategyId(strategy_id)
        self._short_window = short_window
        self._long_window = long_window
        self._max_position_pct = Decimal(str(max_position_pct))

        if short_window >= long_window:
            raise ValueError("short_window 必须小于 long_window")

        # 每个标的独立状态
        self._prices: dict[str, deque[Decimal]] = {}
        self._prev_mas: dict[str, tuple[Decimal, Decimal]] = {}
        self._positions: dict[str, Decimal] = {}
        self._symbol_objs: dict[str, Symbol] = {}
        self._symbols_seen: set[str] = set()

    @property
    def strategy_id(self) -> StrategyId:
        return self._id

    async def on_start(self, ctx: StrategyContext) -> None:
        logger.info(
            "ma_cross.started",
            strategy_id=str(self._id),
            short=self._short_window,
            long=self._long_window,
        )

    async def on_market_data(
        self, event: MarketDataEvent, ctx: StrategyContext
    ) -> None:
        if event.type is not MarketDataEventType.BAR or event.bar is None:
            return

        bar = event.bar
        code = bar.symbol.code

        if code not in self._prices:
            self._prices[code] = deque(maxlen=self._long_window)
            self._symbol_objs[code] = bar.symbol
        self._symbols_seen.add(code)
        self._prices[code].append(bar.close)

        prices = self._prices[code]
        if len(prices) < self._long_window:
            return

        short_ma = self._sma(prices, self._short_window)
        long_ma = self._sma(prices, self._long_window)

        prev = self._prev_mas.get(code)
        if prev is not None:
            prev_short, prev_long = prev
            crossed_up = prev_short <= prev_long and short_ma > long_ma
            crossed_down = prev_short >= prev_long and short_ma < long_ma

            if crossed_up:
                await self._go_long(ctx, code, bar.close)
            elif crossed_down:
                await self._go_short(ctx, code, bar.close)

        self._prev_mas[code] = (short_ma, long_ma)

    @staticmethod
    def _sma(prices: deque[Decimal], period: int) -> Decimal:
        """计算简单移动平均(取 deque 最后 period 个值)。"""
        values = list(prices)[-period:]
        return sum(values) / Decimal(len(values))

    async def _go_long(self, ctx: StrategyContext, code: str, price: Decimal) -> None:
        """金叉 → 等权买入。"""
        account = await ctx.get_account()
        if account is None:
            return

        n = max(len(self._symbols_seen), 1)
        target_value = account.total_asset * self._max_position_pct / Decimal(n)
        target_qty = self._calc_quantity(target_value, price)

        current = self._positions.get(code, Decimal("0"))
        if target_qty <= current:
            return

        buy_qty = self._round_lot(target_qty - current)
        if buy_qty <= 0:
            return

        logger.info(
            "ma_cross.gold_cross",
            strategy_id=str(self._id),
            symbol=code,
            price=str(price),
            buy_qty=str(buy_qty),
        )
        try:
            await ctx.submit_order(
                self._symbol_objs[code],
                Side.BUY,
                buy_qty,
                order_type=OrderType.MARKET,
            )
            self._positions[code] = current + buy_qty
        except Exception:
            logger.exception("ma_cross.buy_failed", symbol=code)

    async def _go_short(self, ctx: StrategyContext, code: str, price: Decimal) -> None:
        """死叉 → 全部卖出。"""
        current = self._positions.get(code, Decimal("0"))
        if current <= 0:
            return

        sell_qty = self._round_lot(current)
        if sell_qty <= 0:
            return

        logger.info(
            "ma_cross.death_cross",
            strategy_id=str(self._id),
            symbol=code,
            price=str(price),
            sell_qty=str(sell_qty),
        )
        try:
            await ctx.submit_order(
                self._symbol_objs[code],
                Side.SELL,
                sell_qty,
                order_type=OrderType.MARKET,
            )
            self._positions[code] = current - sell_qty
        except Exception:
            logger.exception("ma_cross.sell_failed", symbol=code)

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
