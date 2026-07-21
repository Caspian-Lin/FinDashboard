"""Broker 回报事件。

事件流是 push 模型 —— broker 在收到券商推送 / 内部撮合完成后,
把 ``BrokerEvent`` 写入内部 ``asyncio.Queue``,OrderManager 通过
``adapter.events()`` 异步消费。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from finboard_shared.identifiers import ClientOrderId
from finboard_shared.models import Fill
from finboard_shared.types import RejectReason


class BrokerEventType(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ORDER_ACCEPTED = "order_accepted"
    ORDER_REJECTED = "order_rejected"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_CANCEL_REJECTED = "order_cancel_rejected"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BrokerEvent:
    """broker 推送的原子事件。

    ``client_order_id`` 与 ``broker_order_id`` 不一定同时存在(例如连接事件没有订单上下文)。
    """

    type: BrokerEventType
    client_order_id: ClientOrderId | None = None
    broker_order_id: str | None = None
    fill: Fill | None = None  # 仅 ``ORDER_FILLED`` 携带
    reject_reason: RejectReason | None = None
    message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
