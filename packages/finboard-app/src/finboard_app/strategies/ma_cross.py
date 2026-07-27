"""均线交叉策略(MA Cross)—— 多标的信号驱动策略。

经典双均线交叉,支持同时跟踪任意数量的标的:

* **金叉**:短期均线上穿长期均线 → 买入该标的(等权分配)
* **死叉**:短期均线下穿长期均线 → 卖出该标的全部持仓

资金分配:每个标的目标仓位 = ``总权益 * max_position_pct / N``(N = 已发现的标的数)。
当目标股数不足 1 手(100 股)时自动跳过。

策略通过 ``on_market_data`` 消费 Bar,使用 ``ctx.submit_order`` 下单。

issue #56:策略内部持仓不再乐观修改;``_positions`` 由 ``on_order_update``
接收的成交回报驱动。拒单 / 部分成交 / 撤单不会让内部状态漂移。

可选的动态选股(``universe_mode``):策略在每根 Bar 上调用
:class:`~finboard_backtest.bar_universe.BarUniverseSelector` 更新入选状态,
**仅对当前入选标的执行金叉买入**。``universe_exit_clear=True`` 时,标的发生
入选 → 退出转换会通过 ``ctx.submit_order`` 提交清仓卖出意图,卖出仍走完整
风控链路。默认 ``universe_mode="all"`` 保持现有策略行为,既有回测语义不变。
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

import structlog

from finboard_backtest.bar_universe import (
    BarUniverseConfig,
    BarUniverseMode,
    BarUniverseSelector,
)
from finboard_broker.market_base import MarketDataEvent, MarketDataEventType
from finboard_core.strategy import OrderEvent, Strategy, StrategyContext
from finboard_shared.identifiers import StrategyId
from finboard_shared.models import Symbol
from finboard_shared.types import OrderType, Side

logger = structlog.get_logger(__name__)


class MaCrossStrategy(Strategy):
    """双均线交叉策略(多标的)。

    :param short_window: 短期均线周期(如 5)
    :param long_window:  长期均线周期(如 20),必须 > short_window
    :param max_position_pct: 最大总仓位比例(如 0.95 = 留 5% 现金)
    :param universe_mode: 动态选股模式,``all``(默认)/``liquidity_momentum``
    :param universe_lookback: 选股回溯窗口(仅 ``liquidity_momentum`` 生效)
    :param universe_min_avg_amount: 选股最低平均成交额;留空不校验
    :param universe_min_momentum: 选股最低区间动量;留空不校验
    :param universe_exit_clear: 退出时是否清仓内部持仓
    """

    def __init__(
        self,
        *,
        strategy_id: str,
        short_window: int = 5,
        long_window: int = 20,
        max_position_pct: float = 0.95,
        symbol_code: str | None = None,  # 向后兼容,忽略
        universe_mode: str = "all",
        universe_lookback: int = 20,
        universe_min_avg_amount: Decimal | None = None,
        universe_min_momentum: Decimal | None = None,
        universe_exit_clear: bool = False,
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

        # 动态选股器(纯函数,无 I/O,实盘启用前需走样本外/影子/小资金验证)
        try:
            mode = BarUniverseMode(universe_mode)
        except ValueError as exc:
            raise ValueError(f"未知 universe_mode: {universe_mode}") from exc
        self._universe = BarUniverseSelector(
            BarUniverseConfig(
                mode=mode,
                lookback=universe_lookback,
                min_avg_amount=universe_min_avg_amount,
                min_momentum=universe_min_momentum,
                exit_clear=universe_exit_clear,
            )
        )
        self._exit_clear = universe_exit_clear

    @property
    def strategy_id(self) -> StrategyId:
        return self._id

    async def on_start(self, ctx: StrategyContext) -> None:
        logger.info(
            "ma_cross.started",
            strategy_id=str(self._id),
            short=self._short_window,
            long=self._long_window,
            universe_mode=self._universe.config.mode.value,
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

        # 先更新选股器,保证它只看到按时间到达的当前/历史 Bar
        selected = self._universe.update(bar)
        transitioned_out = self._universe.transitioned_out(code)

        # 退出清仓:对内部已记录持仓通过 ctx 卖出,但**不**在这里修改 _positions;
        # 实际成交回报在 on_order_update 中驱动 _positions。
        if transitioned_out and self._exit_clear:
            await self._force_exit(ctx, code, bar.close)

        # 未入选标的不参与金叉/死叉信号生成
        if not selected:
            # 仍维护价格窗口,便于入选后立即给出均线状态
            self._prices[code].append(bar.close)
            return

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

    async def on_order_update(self, event: OrderEvent, ctx: StrategyContext) -> None:
        """持仓由成交回报驱动;拒单 / 撤单不会让 _positions 漂移。"""
        order = event.order
        if order.strategy_id != self._id:
            return
        code = order.symbol.code
        if event.kind == "filled" and event.fill is not None:
            fill = event.fill
            current = self._positions.get(code, Decimal("0"))
            if fill.side is Side.BUY:
                self._positions[code] = current + fill.quantity
            else:
                self._positions[code] = max(current - fill.quantity, Decimal("0"))
        elif event.kind == "rejected":
            # 拒单:不修改 _positions,只记录日志
            logger.info(
                "ma_cross.order_rejected",
                strategy_id=str(self._id),
                symbol=code,
                reason=order.reject_reason.value if order.reject_reason else "unknown",
                message=order.reject_message,
            )

    @staticmethod
    def _sma(prices: deque[Decimal], period: int) -> Decimal:
        """计算简单移动平均(取 deque 最后 period 个值)。"""
        values = list(prices)[-period:]
        return sum(values) / Decimal(len(values))

    async def _go_long(self, ctx: StrategyContext, code: str, price: Decimal) -> None:
        """金叉 → 等权买入。持仓由 ``on_order_update`` 驱动,这里只提交订单。"""
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
            # 不再乐观修改 _positions —— 等待 on_order_update 中的 fill
        except Exception:
            logger.exception("ma_cross.buy_failed", symbol=code)

    async def _go_short(self, ctx: StrategyContext, code: str, price: Decimal) -> None:
        """死叉 → 全部卖出。持仓由 ``on_order_update`` 驱动。"""
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
            # 不再乐观修改 _positions —— 等待 fill
        except Exception:
            logger.exception("ma_cross.sell_failed", symbol=code)

    async def _force_exit(
        self, ctx: StrategyContext, code: str, price: Decimal
    ) -> None:
        """标的发生 入选→退出 转换时清仓内部持仓。

        通过 ``ctx.submit_order`` 走完整风控链路。**不再乐观修改 _positions**;
        持仓由 ``on_order_update`` 中的 fill 实际驱动。
        """
        current = self._positions.get(code, Decimal("0"))
        if current <= 0:
            return

        sell_qty = self._round_lot(current)
        if sell_qty <= 0:
            return

        logger.info(
            "ma_cross.universe_exit",
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
        except Exception:
            logger.exception("ma_cross.universe_exit_failed", symbol=code)

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
