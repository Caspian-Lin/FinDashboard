"""CTP (期货) BrokerAdapter 占位实现。详见包 README。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

from finboard_broker.base import BrokerAdapter, SubmissionResult
from finboard_broker.events import BrokerEvent
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Account, Order, Position
from finboard_shared.types import BrokerKind

CTP_NOT_IMPLEMENTED = (
    "CTP 适配器尚未实现(P0 阶段占位)。"
    " P1 实盘接入 CTP/上期 API 时在本模块填充;当前请使用 mock broker。"
)


class CtpBroker(BrokerAdapter):
    @property
    def kind(self) -> BrokerKind:
        return BrokerKind.CTP

    async def connect(self, account_id: AccountId, credentials: Mapping[str, str]) -> None:
        raise NotImplementedError(CTP_NOT_IMPLEMENTED)

    async def disconnect(self) -> None:
        raise NotImplementedError(CTP_NOT_IMPLEMENTED)

    async def is_connected(self) -> bool:
        return False

    async def query_account(self) -> Account:
        raise NotImplementedError(CTP_NOT_IMPLEMENTED)

    async def query_positions(self) -> list[Position]:
        raise NotImplementedError(CTP_NOT_IMPLEMENTED)

    async def query_order(self, client_order_id: str) -> Order | None:
        raise NotImplementedError(CTP_NOT_IMPLEMENTED)

    async def query_active_orders(self) -> list[Order]:
        raise NotImplementedError(CTP_NOT_IMPLEMENTED)

    async def place_order(self, order: Order) -> SubmissionResult:
        raise NotImplementedError(CTP_NOT_IMPLEMENTED)

    async def cancel_order(self, client_order_id: str) -> None:
        raise NotImplementedError(CTP_NOT_IMPLEMENTED)

    def events(self) -> AsyncIterator[BrokerEvent]:
        raise NotImplementedError(CTP_NOT_IMPLEMENTED)


def create_broker() -> BrokerAdapter:
    return CtpBroker()
