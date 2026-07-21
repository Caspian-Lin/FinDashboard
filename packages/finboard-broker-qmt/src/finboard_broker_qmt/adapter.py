"""QMT (xtquant) BrokerAdapter 占位实现。

详见包 README。P1 阶段会用以下方式替换 ``NotImplementedError``:

* 在 ``try: from xtquant import xttrader, xtdata`` 处 ImportError 时
  直接让 :func:`finboard_broker.factory.create_broker` 抛清晰错误;
* 否则初始化 ``XtQuantTrader``,注册回调线程,把回报桥接到 ``asyncio.Queue``。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

from finboard_broker.base import BrokerAdapter, SubmissionResult
from finboard_broker.events import BrokerEvent
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Account, Order, Position
from finboard_shared.types import BrokerKind

QMT_NOT_IMPLEMENTED = (
    "QMT 适配器尚未实现(P0 阶段占位)。"
    " P1 实盘接入 QMT/xtquant 时在本模块填充;当前请使用 mock broker。"
)


class QmtBroker(BrokerAdapter):
    """占位类:接口齐全但所有方法均抛 ``NotImplementedError``。"""

    @property
    def kind(self) -> BrokerKind:
        return BrokerKind.QMT

    async def connect(self, account_id: AccountId, credentials: Mapping[str, str]) -> None:
        raise NotImplementedError(QMT_NOT_IMPLEMENTED)

    async def disconnect(self) -> None:
        raise NotImplementedError(QMT_NOT_IMPLEMENTED)

    async def is_connected(self) -> bool:
        return False

    async def query_account(self) -> Account:
        raise NotImplementedError(QMT_NOT_IMPLEMENTED)

    async def query_positions(self) -> list[Position]:
        raise NotImplementedError(QMT_NOT_IMPLEMENTED)

    async def query_order(self, client_order_id: str) -> Order | None:
        raise NotImplementedError(QMT_NOT_IMPLEMENTED)

    async def query_active_orders(self) -> list[Order]:
        raise NotImplementedError(QMT_NOT_IMPLEMENTED)

    async def place_order(self, order: Order) -> SubmissionResult:
        raise NotImplementedError(QMT_NOT_IMPLEMENTED)

    async def cancel_order(self, client_order_id: str) -> None:
        raise NotImplementedError(QMT_NOT_IMPLEMENTED)

    def events(self) -> AsyncIterator[BrokerEvent]:
        raise NotImplementedError(QMT_NOT_IMPLEMENTED)


def create_broker() -> BrokerAdapter:
    """工厂入口,供 :func:`finboard_broker.factory.create_broker` 动态调用。"""
    return QmtBroker()
