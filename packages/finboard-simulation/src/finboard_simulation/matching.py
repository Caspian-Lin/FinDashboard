"""模拟撮合纯函数。持久化、风控和会计由 ``SimulationService`` 负责。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from finboard_backtest.asset_rules import (
    AssetRule,
    fill_price_for,
    fill_quantity_within_participation,
    price_with_slippage,
)
from finboard_shared.models import Bar
from finboard_shared.types import OrderType, Side, TimeInForce
from finboard_simulation.contracts import SimulationMatchingConfig
from finboard_simulation.rules import SimulationAssetRule


class MatchAction(StrEnum):
    WAIT = "wait"
    FILL = "fill"
    REJECT = "reject"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class PendingOrder:
    order_id: str
    side: Side
    order_type: OrderType
    time_in_force: TimeInForce
    remaining_quantity: Decimal
    limit_price: Decimal | None
    eligible_after: datetime


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    action: MatchAction
    quantity: Decimal = Decimal("0")
    raw_price: Decimal | None = None
    fill_price: Decimal | None = None
    reason: str = ""
    cancel_remainder: bool = False


def match_order(
    *,
    order: PendingOrder,
    bar: Bar,
    previous_close: Decimal | None,
    asset_rule: SimulationAssetRule,
    config: SimulationMatchingConfig,
) -> MatchOutcome:
    rule = asset_rule.rule
    if bar.timestamp <= order.eligible_after:
        return MatchOutcome(MatchAction.WAIT, reason="next_bar_gate")
    if config.enforce_suspension and rule.is_suspended(bar, pre_close=previous_close):
        return MatchOutcome(MatchAction.REJECT, reason="suspended")
    if config.enforce_price_limit and _price_limit_blocked(order.side, bar, rule, previous_close):
        return MatchOutcome(MatchAction.REJECT, reason="price_limit")

    raw_price = _raw_price(order, bar, rule, config)
    if raw_price is None:
        if order.time_in_force in {TimeInForce.IOC, TimeInForce.FOK}:
            return MatchOutcome(MatchAction.CANCEL, reason="limit_not_reached")
        return MatchOutcome(MatchAction.WAIT, reason="limit_not_reached")

    available = fill_quantity_within_participation(
        order.remaining_quantity,
        bar.volume,
        config.max_participation,
    )
    if config.enforce_lot_rounding:
        available = rule.round_to_lot(available)
    if order.time_in_force is TimeInForce.FOK and available < order.remaining_quantity:
        return MatchOutcome(MatchAction.CANCEL, reason="fok_not_fillable")
    if available <= 0:
        if order.time_in_force is TimeInForce.IOC:
            return MatchOutcome(MatchAction.CANCEL, reason="ioc_no_liquidity")
        return MatchOutcome(MatchAction.WAIT, reason="no_liquidity")
    if not config.allow_partial_fill and available < order.remaining_quantity:
        return MatchOutcome(MatchAction.WAIT, reason="partial_fill_disabled")

    fill_quantity = min(available, order.remaining_quantity)
    fill_price = rule.round_to_tick(price_with_slippage(raw_price, order.side, config.slippage_bps))
    if fill_price <= 0:
        return MatchOutcome(MatchAction.REJECT, reason="invalid_fill_price")
    return MatchOutcome(
        MatchAction.FILL,
        quantity=fill_quantity,
        raw_price=raw_price,
        fill_price=fill_price,
        reason="matched",
        cancel_remainder=order.time_in_force is TimeInForce.IOC,
    )


def _raw_price(
    order: PendingOrder,
    bar: Bar,
    rule: AssetRule,
    config: SimulationMatchingConfig,
) -> Decimal | None:
    if order.order_type is OrderType.LIMIT:
        price = order.limit_price
        if price is None:
            return None
        if order.side is Side.BUY and bar.low > price:
            return None
        if order.side is Side.SELL and bar.high < price:
            return None
    else:
        price = None
    timing = {
        "next_bar_open": "open",
        "next_bar_close": "close",
        "next_bar_vwap_proxy": "vwap_proxy",
    }[config.fill_timing.value]
    return fill_price_for(
        rule,
        side=order.side,
        bar=bar,
        fill_timing=timing,
        limit_price=price,
    )


def _price_limit_blocked(
    side: Side,
    bar: Bar,
    rule: AssetRule,
    previous_close: Decimal | None,
) -> bool:
    if side is Side.BUY:
        return rule.is_at_limit_up(bar, pre_close=previous_close)
    return rule.is_at_limit_down(bar, pre_close=previous_close)


__all__ = [
    "MatchAction",
    "MatchOutcome",
    "PendingOrder",
    "match_order",
]
