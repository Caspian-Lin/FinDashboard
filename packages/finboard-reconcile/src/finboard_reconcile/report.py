"""核对报告数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class OrderMismatch:
    client_order_id: str
    local_status: str
    broker_status: str
    local_filled: Decimal
    broker_filled: Decimal
    notes: str = ""


@dataclass(frozen=True, slots=True)
class PositionMismatch:
    symbol: str
    local_total: Decimal
    broker_total: Decimal
    local_avg_price: Decimal
    broker_avg_price: Decimal
    diff: Decimal
    notes: str = ""


@dataclass(frozen=True, slots=True)
class AccountMismatch:
    field_name: str
    local_value: Decimal
    broker_value: Decimal
    diff: Decimal


def _empty_orders() -> list[OrderMismatch]:
    return []


def _empty_positions() -> list[PositionMismatch]:
    return []


def _empty_accounts() -> list[AccountMismatch]:
    return []


def _empty_strs() -> list[str]:
    return []


@dataclass
class ReconciliationReport:
    """一次核对的汇总结果。"""

    snapshot_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    order_mismatches: list[OrderMismatch] = field(default_factory=_empty_orders)
    position_mismatches: list[PositionMismatch] = field(
        default_factory=_empty_positions
    )
    account_mismatches: list[AccountMismatch] = field(
        default_factory=_empty_accounts
    )
    local_only_orders: list[str] = field(default_factory=_empty_strs)
    broker_only_orders: list[str] = field(default_factory=_empty_strs)

    @property
    def ok(self) -> bool:
        return not (
            self.order_mismatches
            or self.position_mismatches
            or self.account_mismatches
            or self.local_only_orders
            or self.broker_only_orders
        )

    def summary(self) -> str:
        return (
            f"reconciliation ok={self.ok} "
            f"orders_diff={len(self.order_mismatches)} "
            f"local_only={len(self.local_only_orders)} "
            f"broker_only={len(self.broker_only_orders)} "
            f"positions_diff={len(self.position_mismatches)} "
            f"account_diff={len(self.account_mismatches)}"
        )
