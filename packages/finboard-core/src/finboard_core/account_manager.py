"""``AccountManager`` —— 账户资金快照。

资金真值来自券商查询;本类仅负责"拉一次查询 → 持久化 → 发布事件"。
本地不下单金额 / 不冻结资金计算 —— 这些由券商侧主导,本地只在需要时刷新。
"""

from __future__ import annotations

import structlog

from finboard_broker.base import BrokerAdapter
from finboard_core.bus import EventBus
from finboard_core.events import AccountSnapshotUpdated
from finboard_persistence.repo import AccountRepository
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Account

logger = structlog.get_logger(__name__)


class AccountManager:
    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        account_repo: AccountRepository,
        account_id: AccountId,
        event_bus: EventBus,
    ) -> None:
        self._broker = broker
        self._repo = account_repo
        self._account_id = account_id
        self._bus = event_bus

    async def refresh(self) -> Account:
        """从 broker 拉取最新账户快照并发布事件。"""
        account = await self._broker.query_account()
        await self._repo.upsert(account)
        await self._bus.publish(AccountSnapshotUpdated(account=account))
        return account

    async def current(self) -> Account | None:
        return await self._repo.get(str(self._account_id))
