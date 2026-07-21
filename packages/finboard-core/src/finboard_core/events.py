"""内核内部领域事件。

* 与 :mod:`finboard_broker.events` 区分:broker 事件描述"券商侧发生了什么",
  本模块事件描述"内核完成了什么处理"。例如 broker 推送 ``ORDER_FILLED``,
  内核处理完后会发布 :class:`OrderFilled` 给策略 / 持仓 manager 订阅。
* 全部 frozen dataclass,事件不可变,便于审计重放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from finboard_shared.identifiers import AccountId, StrategyId
from finboard_shared.models import Account, Fill, Order, Symbol
from finboard_shared.types import KillSwitchLevel, RejectReason


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class OrderCreated:
    order: Order
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class OrderSubmitted:
    order: Order
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class OrderFilled:
    order: Order
    fill: Fill
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class OrderCancelled:
    order: Order
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class OrderRejected:
    order: Order
    reason: RejectReason
    message: str = ""
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class PositionUpdated:
    account_id: AccountId
    symbol: Symbol
    total_quantity: Decimal
    available_quantity: Decimal
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class AccountSnapshotUpdated:
    account: Account
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class KillSwitchActivated:
    level: KillSwitchLevel
    strategy_id: StrategyId | None = None
    reason: str = ""
    timestamp: datetime = field(default_factory=_utcnow)
