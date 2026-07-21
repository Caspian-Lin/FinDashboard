"""``PositionManager`` —— 本地持仓维护。

红线(AGENTS.md §持仓的最终真实来源是券商查询):

* 本地持仓**不**是真值,只用于实时响应 / 风控 / 异常检测;
* **禁止**策略直接调用本类的写方法修改持仓数量 —— 写方法仅供
  ``OrderManager`` 在成交事件中调用;
* broker 查询结果通过 :meth:`overwrite_from_broker` 覆盖 broker 源;
* Reconciliation 在两者不一致时以 broker 源为真值覆盖本地源。
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from finboard_core.bus import EventBus
from finboard_core.events import PositionUpdated
from finboard_persistence.repo import PositionRepository
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Fill, Position, Symbol
from finboard_shared.types import Side

logger = structlog.get_logger(__name__)


class PositionManager:
    def __init__(
        self,
        *,
        position_repo: PositionRepository,
        account_id: AccountId,
        event_bus: EventBus,
    ) -> None:
        self._repo = position_repo
        self._account_id = account_id
        self._bus = event_bus

    async def list_local(self) -> list[Position]:
        return await self._repo.list_local(str(self._account_id))

    async def list_broker(self) -> list[Position]:
        return await self._repo.list_broker(str(self._account_id))

    async def apply_fill(self, fill: Fill) -> Position:
        """根据一笔成交更新本地持仓。仅在 ``OrderManager`` 内部调用。"""
        position = await self._load_or_create(fill.symbol)
        signed_qty = fill.quantity if fill.side is Side.BUY else -fill.quantity

        if signed_qty >= 0:
            new_total = position.total_quantity + signed_qty
            if new_total > 0:
                position.average_price = (
                    position.total_quantity * position.average_price
                    + signed_qty * fill.price
                ) / new_total
            position.total_quantity = new_total
            position.available_quantity = new_total  # 简化:T+1 处理后续完善
        else:
            abs_qty = -signed_qty
            position.total_quantity = max(Decimal("0"), position.total_quantity - abs_qty)
            position.available_quantity = max(
                Decimal("0"), position.available_quantity - abs_qty
            )

        position.market_value = position.total_quantity * position.average_price
        position.touch()

        await self._repo.upsert_local(position)
        await self._bus.publish(
            PositionUpdated(
                account_id=self._account_id,
                symbol=position.symbol,
                total_quantity=position.total_quantity,
                available_quantity=position.available_quantity,
            )
        )
        return position

    async def overwrite_from_broker(self, positions: list[Position]) -> None:
        """把 broker 查询结果整体写入 broker 源。

        这是 Reconciliation 的对齐手段之一;调用方负责语义判定。
        """
        for pos in positions:
            await self._repo.upsert_broker(pos)

    async def _load_or_create(self, symbol: Symbol) -> Position:
        locals_ = await self._repo.list_local(str(self._account_id))
        for pos in locals_:
            if pos.symbol.code == symbol.code:
                return pos
        return Position.empty(self._account_id, symbol)
