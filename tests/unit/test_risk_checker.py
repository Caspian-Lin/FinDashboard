"""``PreTradeChecker`` 与 Kill Switch 单测。"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest

from finboard_risk import KillSwitch, PreTradeChecker, RiskConfig
from finboard_risk.context import RiskContext
from finboard_shared.exceptions import KillSwitchActiveError, RiskCheckError
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Account, OrderRequest, Position, Symbol
from finboard_shared.types import BrokerKind, Market, OrderType, RejectReason, Side


@pytest.fixture
def config() -> RiskConfig:
    return RiskConfig(
        max_order_value=Decimal("10000"),
        max_symbol_position_value=Decimal("30000"),
        max_daily_buy_value=Decimal("50000"),
        max_active_orders=10,
        max_orders_per_minute=5,
        allow_short=False,
        allow_market_order=False,
    )


@pytest.fixture
def checker(config: RiskConfig) -> PreTradeChecker:
    return PreTradeChecker(config=config, kill_switch=KillSwitch())


@pytest.fixture
def symbol() -> Symbol:
    return Symbol(code="510300.SH", market=Market.A_SHARE)


def _make_request(
    symbol: Symbol,
    *,
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.LIMIT,
    quantity: Decimal = Decimal("100"),
    price: Decimal | None = Decimal("3.85"),
) -> OrderRequest:
    return OrderRequest(
        account_id=AccountId("test-account"),
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
    )


@pytest.mark.unit
async def test_passes_basic_limit_order(checker: PreTradeChecker, symbol: Symbol) -> None:
    await checker.check(_make_request(symbol))


@pytest.mark.unit
async def test_rejects_zero_quantity(
    checker: PreTradeChecker, symbol: Symbol
) -> None:
    with pytest.raises(RiskCheckError) as exc:
        await checker.check(_make_request(symbol, quantity=Decimal("0")))
    assert exc.value.reason is RejectReason.INVALID_QUANTITY


@pytest.mark.unit
async def test_rejects_market_when_disabled(
    checker: PreTradeChecker, symbol: Symbol
) -> None:
    with pytest.raises(RiskCheckError):
        await checker.check(
            _make_request(symbol, order_type=OrderType.MARKET, price=None)
        )


@pytest.mark.unit
async def test_rejects_too_large_order(
    checker: PreTradeChecker, symbol: Symbol
) -> None:
    # 10000 / 3.85 ≈ 2597;下 3000 股会超 10000 上限
    with pytest.raises(RiskCheckError) as exc:
        await checker.check(
            _make_request(symbol, quantity=Decimal("3000"), price=Decimal("3.85"))
        )
    assert exc.value.reason is RejectReason.RISK_CHECK_FAILED


@pytest.mark.unit
async def test_rate_limit_kicks_in(
    checker: PreTradeChecker, symbol: Symbol
) -> None:
    for _ in range(5):
        await checker.check(_make_request(symbol))
    with pytest.raises(RiskCheckError) as exc:
        await checker.check(_make_request(symbol))
    assert exc.value.reason is RejectReason.RISK_CHECK_FAILED


@pytest.mark.unit
async def test_kill_switch_blocks_new_orders(
    config: RiskConfig, symbol: Symbol
) -> None:
    from finboard_shared.types import KillSwitchLevel

    ks = KillSwitch()
    ks.set(KillSwitchLevel.NO_NEW_ORDERS)
    checker = PreTradeChecker(config=config, kill_switch=ks)
    with pytest.raises(KillSwitchActiveError):
        await checker.check(_make_request(symbol))


@pytest.mark.unit
async def test_kill_switch_allows_reduce(
    config: RiskConfig, symbol: Symbol
) -> None:
    """NO_NEW_ORDERS 时仍允许 SELL(平仓)。"""
    from finboard_shared.types import KillSwitchLevel

    ks = KillSwitch()
    ks.set(KillSwitchLevel.NO_NEW_ORDERS)
    checker = PreTradeChecker(config=config, kill_switch=ks)
    # SELL 在 reduce_only 允许下应当通过(基础合法性都满足)
    await checker.check(_make_request(symbol, side=Side.SELL))


@pytest.mark.unit
async def test_context_active_orders_limit(
    config: RiskConfig, symbol: Symbol
) -> None:
    """注入 RiskContext 提供活动订单数;超过上限则拒。"""

    class StubContext:
        async def list_positions(self) -> list[Position]:
            return []

        async def get_account(self) -> Account | None:
            return None

        async def count_active_orders(self) -> int:
            return 10  # 等于上限 → 下一个就该拒

        async def daily_buy_value(self) -> Decimal:
            return Decimal("0")

    checker = PreTradeChecker(
        config=config,
        context=cast(RiskContext, StubContext()),
    )
    with pytest.raises(RiskCheckError) as exc:
        await checker.check(_make_request(symbol))
    assert exc.value.reason is RejectReason.RISK_CHECK_FAILED
    _ = BrokerKind  # 占位


def _stub_context(
    *,
    positions: list[Position] | None = None,
    active_orders: int = 0,
    daily_buy: Decimal = Decimal("0"),
) -> RiskContext:
    class _Ctx:
        async def list_positions(self) -> list[Position]:
            return positions or []

        async def get_account(self) -> Account | None:
            return None

        async def count_active_orders(self) -> int:
            return active_orders

        async def daily_buy_value(self) -> Decimal:
            return daily_buy

    return cast(RiskContext, _Ctx())


@pytest.mark.unit
async def test_sell_blocked_without_position(
    config: RiskConfig, symbol: Symbol
) -> None:
    """无持仓时不允许卖出。"""
    checker = PreTradeChecker(
        config=config,
        context=_stub_context(),
    )
    with pytest.raises(RiskCheckError) as exc:
        await checker.check(_make_request(symbol, side=Side.SELL))
    assert exc.value.reason is RejectReason.INSUFFICIENT_POSITION


@pytest.mark.unit
async def test_sell_exceeds_available(
    config: RiskConfig, symbol: Symbol
) -> None:
    """卖出数量超过可用时被拒。"""
    pos = Position(
        account_id=AccountId("test-account"),
        symbol=symbol,
        available_quantity=Decimal("100"),
        total_quantity=Decimal("100"),
    )
    checker = PreTradeChecker(
        config=config,
        context=_stub_context(positions=[pos]),
    )
    with pytest.raises(RiskCheckError) as exc:
        await checker.check(
            _make_request(symbol, side=Side.SELL, quantity=Decimal("200"))
        )
    assert exc.value.reason is RejectReason.INSUFFICIENT_POSITION


@pytest.mark.unit
async def test_sell_within_available_passes(
    config: RiskConfig, symbol: Symbol
) -> None:
    """卖出数量 <= 可用时通过。"""
    pos = Position(
        account_id=AccountId("test-account"),
        symbol=symbol,
        available_quantity=Decimal("200"),
        total_quantity=Decimal("200"),
    )
    checker = PreTradeChecker(
        config=config,
        context=_stub_context(positions=[pos]),
    )
    await checker.check(
        _make_request(symbol, side=Side.SELL, quantity=Decimal("100"))
    )


@pytest.mark.unit
async def test_sell_no_context_skips_position_check(
    config: RiskConfig, symbol: Symbol
) -> None:
    """无 context 时卖出检查跳过(向后兼容 / 单测场景)。"""
    checker = PreTradeChecker(config=config)
    await checker.check(_make_request(symbol, side=Side.SELL))
