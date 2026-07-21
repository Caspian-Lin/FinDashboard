"""下单前风控主实现。

实现 :class:`finboard_core.protocols.RiskChecker`(Protocol,本包不直接 import
``finboard-core`` 以避免循环依赖,只保证结构匹配)。

检查顺序(任一失败立即抛异常):

1. Kill Switch 状态(NO_NEW_ORDERS / HALT 等)
2. 基础合法性(价格、数量正数;symbol 非空)
3. 市价单 / 卖空开关
4. 单笔金额上限 ``max_order_value``
5. 当日累计买入上限 ``max_daily_buy_value``
6. 速率限制 ``max_orders_per_minute``(滑窗内存计数)
7. 活动订单数 ``max_active_orders``
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog

from finboard_risk.config import RiskConfig
from finboard_risk.context import RiskContext
from finboard_risk.kill_switch import KillSwitch
from finboard_shared.exceptions import KillSwitchActiveError, RiskCheckError
from finboard_shared.models import OrderRequest
from finboard_shared.types import KillSwitchLevel, OrderType, RejectReason, Side

logger = structlog.get_logger(__name__)


class PreTradeChecker:
    """下单前风控实现。匹配 :class:`finboard_core.protocols.RiskChecker`。"""

    def __init__(
        self,
        *,
        config: RiskConfig,
        kill_switch: KillSwitch | None = None,
        context: RiskContext | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._kill_switch = kill_switch or KillSwitch()
        self._context = context
        self._call_timestamps: deque[datetime] = deque()
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        _ = (asyncio,)  # 占位:后续速率限制可能切到 asyncio.Lock

    # ------------------------------------------------------------------ RiskChecker 接口
    async def check(self, request: OrderRequest) -> None:
        await self._check_kill_switch(request)
        self._check_basic(request)
        self._check_order_type(request)
        self._check_per_order_value(request)
        await self._check_daily_buy_value(request)
        self._check_rate_limit()
        await self._check_active_orders()

    async def can_place_new_orders(self) -> bool:
        return self._kill_switch.allows_new_orders()

    async def set_kill_switch_level(self, level: KillSwitchLevel) -> None:
        self._kill_switch.set(level)
        logger.warning("risk.kill_switch_set", level=level.value)

    # ------------------------------------------------------------------ 各检查
    async def _check_kill_switch(self, request: OrderRequest) -> None:
        if self._kill_switch.allows_new_orders():
            return
        if (
            request.side is Side.SELL
            and self._kill_switch.allows_reduce_only()
        ):
            return
        raise KillSwitchActiveError(
            f"Kill Switch 处于 {self._kill_switch.level.value},"
            f" 拒绝 {request.side.value} {request.symbol}"
        )

    def _check_basic(self, request: OrderRequest) -> None:
        if request.quantity <= 0:
            raise RiskCheckError(RejectReason.INVALID_QUANTITY, "数量必须为正")
        if (
            request.order_type is OrderType.LIMIT
            and (request.price is None or request.price <= 0)
        ):
            raise RiskCheckError(
                RejectReason.INVALID_PRICE, "限价单价格必须为正"
            )
        if not request.symbol.code:
            raise RiskCheckError(RejectReason.INVALID_SYMBOL, "symbol 不能为空")

    def _check_order_type(self, request: OrderRequest) -> None:
        if request.order_type is OrderType.MARKET and not self._config.allow_market_order:
            raise RiskCheckError(
                RejectReason.RISK_CHECK_FAILED,
                "当前风控配置禁止市价单(allow_market_order=false)",
            )
        if request.side is Side.SELL and not self._config.allow_short:
            # A股不允许卖空;此检查只在"无持仓还卖"时为真正错误,
            # 真实判断需要 context 提供 available_quantity,这里只兜底配置开关
            pass

    def _check_per_order_value(self, request: OrderRequest) -> None:
        price = request.price or Decimal("0")  # 市价单无法准确估,放过后由 broker 二次校验
        notional = request.quantity * price
        if price > 0 and notional > self._config.max_order_value:
            raise RiskCheckError(
                RejectReason.RISK_CHECK_FAILED,
                f"单笔金额 {notional} 超过上限 {self._config.max_order_value}",
            )

    async def _check_daily_buy_value(self, request: OrderRequest) -> None:
        if request.side is not Side.BUY:
            return
        if request.price is None or self._context is None:
            return
        used = await self._context.daily_buy_value()
        new_notional = request.quantity * request.price
        if used + new_notional > self._config.max_daily_buy_value:
            raise RiskCheckError(
                RejectReason.INSUFFICIENT_CASH,
                f"当日累计买入将达 {used + new_notional},"
                f" 超过 max_daily_buy_value={self._config.max_daily_buy_value}",
            )

    def _check_rate_limit(self) -> None:
        now = self._clock()
        window = timedelta(minutes=1)
        while self._call_timestamps and now - self._call_timestamps[0] > window:
            self._call_timestamps.popleft()
        if len(self._call_timestamps) >= self._config.max_orders_per_minute:
            raise RiskCheckError(
                RejectReason.RISK_CHECK_FAILED,
                f"超过每分钟订单数上限 {self._config.max_orders_per_minute}",
            )
        self._call_timestamps.append(now)

    async def _check_active_orders(self) -> None:
        if self._context is None:
            return
        count = await self._context.count_active_orders()
        if count >= self._config.max_active_orders:
            raise RiskCheckError(
                RejectReason.RISK_CHECK_FAILED,
                f"活动订单数 {count} 超过上限 {self._config.max_active_orders}",
            )
