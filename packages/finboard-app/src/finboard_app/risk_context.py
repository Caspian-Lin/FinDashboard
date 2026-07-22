"""风控上下文的具体实现 —— 基于 persistence 仓储。

``RiskContext`` Protocol 定义在 :mod:`finboard_risk.context`;本模块提供一个
绑定 ``AsyncSession`` 的实现,由 bootstrap 在 kernel 创建时注入。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from finboard_persistence.repo import (
    AccountRepository,
    FillRepository,
    OrderRepository,
    PositionRepository,
)
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Account, Position


class SessionRiskContext:
    """基于 DB 仓储的风控上下文。

    所有方法共享同一个 ``AsyncSession``,与 ``TradingKernel`` 的生命周期一致。
    """

    def __init__(
        self,
        *,
        position_repo: PositionRepository,
        account_repo: AccountRepository,
        order_repo: OrderRepository,
        fill_repo: FillRepository,
        account_id: AccountId,
    ) -> None:
        self._position_repo = position_repo
        self._account_repo = account_repo
        self._order_repo = order_repo
        self._fill_repo = fill_repo
        self._account_id = account_id

    async def list_positions(self) -> list[Position]:
        return await self._position_repo.list_local(str(self._account_id))

    async def get_account(self) -> Account | None:
        return await self._account_repo.get(str(self._account_id))

    async def count_active_orders(self) -> int:
        return len(await self._order_repo.list_active(str(self._account_id)))

    async def daily_buy_value(self) -> Decimal:
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._fill_repo.sum_buy_value_since(
            str(self._account_id), today
        )
