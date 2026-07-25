"""BacktestBroker 单元测试 —— 撮合逻辑 / 佣金 / 印花税 / 手数取整 / T+1。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from finboard_backtest.broker import BacktestBroker
from finboard_shared.identifiers import (
    AccountId,
    StrategyId,
    generate_client_order_id,
)
from finboard_shared.models import Bar, Order, Symbol
from finboard_shared.types import BrokerKind, Market, OrderStatus, OrderType, Side

SYMBOL = Symbol(code="510300.SH", market=Market.A_SHARE)
ACCOUNT = AccountId("test")


def _make_bar(close: str = "4.00", date_str: str = "2024-01-02") -> Bar:
    return Bar(
        symbol=SYMBOL,
        period=None,  # type: ignore[arg-type]
        timestamp=datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC),
        open=Decimal("3.90"),
        high=Decimal("4.10"),
        low=Decimal("3.80"),
        close=Decimal(close),
        volume=Decimal("1000000"),
    )


def _make_order(
    side: Side = Side.BUY,
    qty: Decimal = Decimal("200"),
    order_type: OrderType = OrderType.MARKET,
    price: Decimal | None = None,
    strategy_id: StrategyId | None = None,
) -> Order:
    return Order(
        client_order_id=generate_client_order_id(),
        account_id=ACCOUNT,
        broker_kind=BrokerKind.BACKTEST,
        symbol=SYMBOL,
        side=side,
        order_type=order_type,
        quantity=qty,
        price=price,
        strategy_id=strategy_id,
    )


class TestBacktestBrokerMarketOrder:
    @pytest.mark.unit
    async def test_market_buy_fills_at_close(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("10000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_make_bar("4.00"))

        order = _make_order(Side.BUY, Decimal("200"))
        result = await broker.place_order(order)

        assert result.accepted
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == Decimal("200")
        assert order.average_fill_price == Decimal("4.00")

    @pytest.mark.unit
    async def test_market_buy_updates_cash(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("10000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_make_bar("4.00"))

        order = _make_order(Side.BUY, Decimal("200"))
        await broker.place_order(order)

        # 200 * 4.00 = 800 turnover
        # commission = max(800 * 0.0003, 5) = max(0.24, 5) = 5
        # cash = 10000 - 800 - 5 = 9195
        assert broker._cash == Decimal("9195")

    @pytest.mark.unit
    async def test_market_sell_with_stamp_tax(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("10000"))
        await broker.connect(ACCOUNT, {})

        # Day 1: buy
        broker.on_new_bar(_make_bar("4.00", "2024-01-02"))
        await broker.place_order(_make_order(Side.BUY, Decimal("200")))

        # Day 2: sell
        broker.on_new_bar(_make_bar("4.00", "2024-01-03"))
        sell = _make_order(Side.SELL, Decimal("200"))
        await broker.place_order(sell)

        assert sell.status == OrderStatus.FILLED
        # turnover = 200 * 4.00 = 800
        # commission = max(800 * 0.0003, 5) = 5
        # stamp_tax = 800 * 0.0005 = 0.40
        # cash after buy = 9195
        # cash after sell = 9195 + 800 - 5 - 0.40 = 9989.60
        assert broker._cash == Decimal("9989.60")


class TestBacktestBrokerCommission:
    @pytest.mark.unit
    async def test_minimum_commission(self) -> None:
        """小额交易佣金不低于 ¥5。"""
        broker = BacktestBroker(
            initial_capital=Decimal("10000"),
            commission_rate=Decimal("0.0003"),
            commission_min=Decimal("5"),
        )
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_make_bar("4.00"))

        # 50 shares * 4.00 = 200 turnover
        # rate comm = 200 * 0.0003 = 0.06 < 5 → commission = 5
        order = _make_order(Side.BUY, Decimal("100"))
        await broker.place_order(order)

        fill = broker.fills[-1]
        assert fill.commission == Decimal("5")

    @pytest.mark.unit
    async def test_large_trade_normal_commission(self) -> None:
        """大额交易按费率计算佣金。"""
        broker = BacktestBroker(
            initial_capital=Decimal("100000"),
        )
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_make_bar("10.00"))

        # 1000 * 10 = 10000 turnover
        # commission = max(10000 * 0.0003, 5) = max(3, 5) = 5
        order = _make_order(Side.BUY, Decimal("1000"))
        await broker.place_order(order)
        fill = broker.fills[-1]
        assert fill.commission == Decimal("5")

        # 5000 * 10 = 50000 turnover
        # commission = max(50000 * 0.0003, 5) = max(15, 5) = 15
        broker.on_new_bar(_make_bar("10.00", "2024-01-03"))
        order2 = _make_order(Side.BUY, Decimal("5000"))
        await broker.place_order(order2)
        fill2 = broker.fills[-1]
        assert fill2.commission == Decimal("15")


class TestBacktestBrokerLotSize:
    @pytest.mark.unit
    async def test_quantity_rounded_down(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_make_bar("4.00"))

        # 350 → rounds to 300
        order = _make_order(Side.BUY, Decimal("350"))
        await broker.place_order(order)

        assert order.filled_quantity == Decimal("300")

    @pytest.mark.unit
    async def test_insufficient_lot_rejected(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_make_bar("4.00"))

        # 50 shares → rounds to 0 → rejected
        order = _make_order(Side.BUY, Decimal("50"))
        result = await broker.place_order(order)

        assert not result.accepted
        assert order.status == OrderStatus.REJECTED


class TestBacktestBrokerLimitOrder:
    @pytest.mark.unit
    async def test_limit_buy_fills_when_price_in_range(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_make_bar("4.00"))

        # bar low=3.80, limit buy at 3.85 → should fill
        order = _make_order(
            Side.BUY, Decimal("200"), OrderType.LIMIT, Decimal("3.85")
        )
        await broker.place_order(order)

        assert order.status == OrderStatus.FILLED
        assert order.average_fill_price == Decimal("3.85")

    @pytest.mark.unit
    async def test_limit_buy_pending_when_price_too_high(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_make_bar("4.00"))

        # bar low=3.80, limit buy at 3.50 → should NOT fill
        order = _make_order(
            Side.BUY, Decimal("200"), OrderType.LIMIT, Decimal("3.50")
        )
        await broker.place_order(order)

        assert order.status == OrderStatus.ACKNOWLEDGED

        # Next bar: low drops to 3.40 → should fill
        broker.on_new_bar(
            Bar(
                symbol=SYMBOL,
                period=None,  # type: ignore[arg-type]
                timestamp=datetime(2024, 1, 3, tzinfo=UTC),
                open=Decimal("3.90"),
                high=Decimal("4.00"),
                low=Decimal("3.40"),
                close=Decimal("3.60"),
            )
        )

        assert order.status == OrderStatus.FILLED  # type: ignore[comparison-overlap]


class TestBacktestBrokerTPlus1:
    @pytest.mark.unit
    async def test_cannot_sell_same_day(self) -> None:
        broker = BacktestBroker(
            initial_capital=Decimal("10000"),
            enforce_t_plus_1=True,
        )
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_make_bar("4.00", "2024-01-02"))

        # Buy
        await broker.place_order(_make_order(Side.BUY, Decimal("200")))

        # Try to sell same day
        sell = _make_order(Side.SELL, Decimal("200"))
        await broker.place_order(sell)

        assert sell.status == OrderStatus.REJECTED

    @pytest.mark.unit
    async def test_can_sell_next_day(self) -> None:
        broker = BacktestBroker(
            initial_capital=Decimal("10000"),
            enforce_t_plus_1=True,
        )
        await broker.connect(ACCOUNT, {})

        # Day 1: buy
        broker.on_new_bar(_make_bar("4.00", "2024-01-02"))
        await broker.place_order(_make_order(Side.BUY, Decimal("200")))

        # Day 2: sell
        broker.on_new_bar(_make_bar("4.00", "2024-01-03"))
        sell = _make_order(Side.SELL, Decimal("200"))
        await broker.place_order(sell)

        assert sell.status == OrderStatus.FILLED

    @pytest.mark.unit
    async def test_t_plus_1_disabled(self) -> None:
        broker = BacktestBroker(
            initial_capital=Decimal("10000"),
            enforce_t_plus_1=False,
        )
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_make_bar("4.00", "2024-01-02"))

        await broker.place_order(_make_order(Side.BUY, Decimal("200")))
        sell = _make_order(Side.SELL, Decimal("200"))
        await broker.place_order(sell)

        assert sell.status == OrderStatus.FILLED


class TestBacktestBrokerEquity:
    @pytest.mark.unit
    async def test_total_equity_mark_to_market(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("10000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_make_bar("4.00", "2024-01-02"))

        # Buy 200 shares @ 4.00 + comm 5
        await broker.place_order(_make_order(Side.BUY, Decimal("200")))

        # cash = 10000 - 800 - 5 = 9195
        # position value = 200 * 4.00 = 800
        # total = 9195 + 800 = 9995 (lost 5 to commission)
        assert broker.total_equity() == Decimal("9995")

        # Price goes up to 5.00
        broker.on_new_bar(_make_bar("5.00", "2024-01-03"))
        # total = 9195 + 200 * 5.00 = 9195 + 1000 = 10195
        assert broker.total_equity() == Decimal("10195")
