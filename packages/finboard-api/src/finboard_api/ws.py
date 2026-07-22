"""WebSocket 连接管理 + EventBus 事件广播。

EventBus 是进程内 pub-sub,不跨进程。WebSocket 必须与 TradingKernel 同进程。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import WebSocket

from finboard_core.bus import EventBus
from finboard_core.events import (
    AccountSnapshotUpdated,
    KillSwitchActivated,
    OrderCancelled,
    OrderCreated,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
    PositionUpdated,
)
from finboard_shared.models import Account, Fill, Order, Symbol

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

_EVENT_TYPES = [
    OrderCreated,
    OrderSubmitted,
    OrderFilled,
    OrderCancelled,
    OrderRejected,
    PositionUpdated,
    AccountSnapshotUpdated,
    KillSwitchActivated,
]


def _decimal(v: Decimal | None) -> str | None:
    if v is None:
        return None
    return str(v)


def _dt(v: datetime) -> str:
    return v.isoformat()


def _symbol(s: Symbol) -> dict[str, str]:
    return {"code": s.code, "market": s.market.value}


def _order_to_dict(order: Order) -> dict[str, object]:
    return {
        "client_order_id": str(order.client_order_id),
        "account_id": str(order.account_id),
        "symbol": order.symbol.code,
        "market": order.symbol.market.value,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "quantity": _decimal(order.quantity),
        "price": _decimal(order.price),
        "status": order.status.value,
        "filled_quantity": _decimal(order.filled_quantity),
        "average_fill_price": _decimal(order.average_fill_price),
        "strategy_id": str(order.strategy_id) if order.strategy_id else None,
        "reject_reason": order.reject_reason.value if order.reject_reason else None,
    }


def _fill_to_dict(fill: Fill) -> dict[str, object]:
    return {
        "fill_id": fill.fill_id,
        "client_order_id": str(fill.client_order_id),
        "symbol": fill.symbol.code,
        "side": fill.side.value,
        "quantity": _decimal(fill.quantity),
        "price": _decimal(fill.price),
        "commission": _decimal(fill.commission),
        "tax": _decimal(fill.tax),
        "filled_at": _dt(fill.filled_at),
    }


def _account_to_dict(account: Account) -> dict[str, object]:
    return {
        "account_id": str(account.account_id),
        "total_asset": _decimal(account.total_asset),
        "cash": _decimal(account.cash),
        "frozen_cash": _decimal(account.frozen_cash),
        "margin_used": _decimal(account.margin_used),
        "updated_at": _dt(account.updated_at),
    }


def event_to_dict(event: object) -> dict[str, Any]:
    """把内核领域事件序列化为 JSON 可发的 dict。"""
    result: dict[str, object] = {"timestamp": _dt(datetime.now(UTC))}

    if isinstance(event, OrderCreated):
        result["type"] = "order_created"
        result["order"] = _order_to_dict(event.order)
        result["timestamp"] = _dt(event.timestamp)
    elif isinstance(event, OrderSubmitted):
        result["type"] = "order_submitted"
        result["order"] = _order_to_dict(event.order)
        result["timestamp"] = _dt(event.timestamp)
    elif isinstance(event, OrderFilled):
        result["type"] = "order_filled"
        result["order"] = _order_to_dict(event.order)
        result["fill"] = _fill_to_dict(event.fill)
        result["timestamp"] = _dt(event.timestamp)
    elif isinstance(event, OrderCancelled):
        result["type"] = "order_cancelled"
        result["order"] = _order_to_dict(event.order)
        result["timestamp"] = _dt(event.timestamp)
    elif isinstance(event, OrderRejected):
        result["type"] = "order_rejected"
        result["order"] = _order_to_dict(event.order)
        result["reason"] = event.reason.value
        result["message"] = event.message
        result["timestamp"] = _dt(event.timestamp)
    elif isinstance(event, PositionUpdated):
        result["type"] = "position_updated"
        result["account_id"] = str(event.account_id)
        result["symbol"] = event.symbol.code
        result["total_quantity"] = _decimal(event.total_quantity)
        result["available_quantity"] = _decimal(event.available_quantity)
        result["timestamp"] = _dt(event.timestamp)
    elif isinstance(event, AccountSnapshotUpdated):
        result["type"] = "account_updated"
        result["account"] = _account_to_dict(event.account)
        result["timestamp"] = _dt(event.timestamp)
    elif isinstance(event, KillSwitchActivated):
        result["type"] = "kill_switch_activated"
        result["level"] = event.level.value
        result["reason"] = event.reason
        result["timestamp"] = _dt(event.timestamp)
    else:
        result["type"] = "unknown"
        result["data"] = str(event)

    return result


class ConnectionManager:
    """管理 WebSocket 连接池,广播事件。"""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        logger.info("ws.connected", total=len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        logger.info("ws.disconnected", total=len(self._connections))

    async def broadcast(self, message: str) -> None:
        """向所有连接广播文本消息。"""
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    async def broadcast_event(self, event: object) -> None:
        """序列化事件并广播 JSON。"""
        data = event_to_dict(event)
        await self.broadcast(json.dumps(data, ensure_ascii=False, default=str))


def setup_event_bridge(
    event_bus: EventBus, manager: ConnectionManager
) -> list[tuple[type, Callable[..., Awaitable[None]]]]:
    """为每种领域事件注册广播 handler,返回 (event_type, handler) 对用于卸载。"""
    handlers: list[tuple[type, Callable[..., Awaitable[None]]]] = []

    async def _handler(event: object) -> None:
        await manager.broadcast_event(event)

    for evt_type in _EVENT_TYPES:
        event_bus.subscribe(evt_type, _handler)
        handlers.append((evt_type, _handler))

    return handlers


def teardown_event_bridge(
    event_bus: EventBus, handlers: list[tuple[type, Callable[..., Awaitable[None]]]]
) -> None:
    for evt_type, handler in handlers:
        event_bus.unsubscribe(evt_type, handler)


# 占位避免 asyncio 未使用告警
_ = asyncio
