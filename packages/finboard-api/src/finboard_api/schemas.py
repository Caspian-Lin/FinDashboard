"""Pydantic 请求 / 响应 schema。

领域 dataclass(``finboard_shared.models``)保持零依赖,API 边界在此做 pydantic 转换。
``Decimal`` 字段在 JSON 序列化时转为 ``str``,避免浮点精度丢失。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class BaseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- Account
class AccountOut(BaseSchema):
    account_id: str
    broker_kind: str
    total_asset: Decimal
    cash: Decimal
    frozen_cash: Decimal
    margin_used: Decimal
    updated_at: datetime


# --------------------------------------------------------------------------- Position
class PositionOut(BaseSchema):
    symbol: str
    market: str
    position_side: str
    total_quantity: Decimal
    available_quantity: Decimal
    frozen_quantity: Decimal
    average_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    updated_at: datetime


# --------------------------------------------------------------------------- Order
class OrderCreate(BaseSchema):
    """手工下单请求体。

    红线:``client_order_id`` 由 OrderManager 内部生成,不接受外部传入。
    """

    symbol: str
    market: str = "a_share"
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None = None
    time_in_force: str = "GFD"


class OrderOut(BaseSchema):
    client_order_id: str
    account_id: str
    broker_kind: str
    symbol: str
    market: str
    side: str
    order_type: str
    quantity: Decimal
    strategy_id: str | None = None
    price: Decimal | None = None
    time_in_force: str
    position_side: str
    broker_order_id: str | None = None
    filled_quantity: Decimal
    average_fill_price: Decimal | None = None
    status: str
    reject_reason: str | None = None
    reject_message: str | None = None
    created_at: datetime
    risk_checked_at: datetime | None = None
    submitted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    updated_at: datetime
    is_active: bool
    is_terminal: bool
    remaining_quantity: Decimal


# --------------------------------------------------------------------------- Fill
class FillOut(BaseSchema):
    fill_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    tax: Decimal
    broker_order_id: str | None = None
    filled_at: datetime


# --------------------------------------------------------------------------- Kill Switch
class KillSwitchOut(BaseSchema):
    level: str
    allows_new_orders: bool
    allows_reduce_only: bool


class KillSwitchActivate(BaseSchema):
    level: str
    reason: str = ""


# --------------------------------------------------------------------------- Reconcile
class ReconcileResult(BaseSchema):
    ok: bool
    summary: str
    total_checks: int
    mismatches: int


# --------------------------------------------------------------------------- Health
class HealthOut(BaseSchema):
    status: str
    kernel_ready: bool
    kill_switch_level: str
    broker_connected: bool = False
    broker_kind: str = ""
    active_orders: int = 0
    started_at: datetime | None = None


# --------------------------------------------------------------------------- Risk Config
class RiskConfigOut(BaseSchema):
    max_order_value: Decimal
    max_symbol_position_value: Decimal
    max_daily_buy_value: Decimal
    max_active_orders: int
    max_orders_per_minute: int
    allow_short: bool
    allow_market_order: bool


# --------------------------------------------------------------------------- Audit
class AuditLogOut(BaseSchema):
    id: int
    actor: str
    action: str
    target: str | None = None
    payload: str | None = None
    created_at: datetime


# --------------------------------------------------------------------------- Pagination
class PageResponse[T](BaseSchema):
    items: list[T]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- WebSocket
class WsError(BaseSchema):
    detail: str
