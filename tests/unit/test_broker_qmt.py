"""QMT 适配器单元测试。

由于 xtquant 仅 Windows + miniQMT 可用,本测试通过注入 mock xtquant 模块
到 ``sys.modules`` 来模拟 SDK 行为。mock 在模块顶层注入,确保 adapter
import 时 ``XTQUANT_AVAILABLE = True``。
"""

from __future__ import annotations

import sys
import types
from decimal import Decimal
from typing import Any

import pytest


# --------------------------------------------------------------------------- mock xtquant 注入
class _FakeAsset:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _FakePosition:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _FakeOrder:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _FakeStockAccount:
    def __init__(self, account_id: str, account_type: str) -> None:
        self.account_id = account_id
        self.account_type = account_type


class _FakeCallback:
    """ XtQuantTraderCallback 占位基类。"""
    pass


class _FakeXtQuantTrader:
    def __init__(self, path: str, session_id: int) -> None:
        self.path = path
        self.session_id = session_id
        self._callback: Any = None
        self._started = False
        self.connect_rc = 0
        self.subscribe_rc = 0
        self.stopped = False
        # 可配置的查询返回值
        self.asset_result: Any = None
        self.positions_result: Any = None
        self.orders_result: Any = None

    def register_callback(self, cb: Any) -> None:
        self._callback = cb

    def start(self) -> None:
        self._started = True

    def connect(self) -> int:
        return self.connect_rc

    def subscribe(self, account: Any) -> int:
        return self.subscribe_rc

    def stop(self) -> None:
        self.stopped = True

    def query_stock_asset(self, account: Any) -> Any:
        return self.asset_result

    def query_stock_positions(self, account: Any) -> Any:
        return self.positions_result

    def query_stock_orders(self, account: Any, cancelable_only: bool = False) -> Any:
        return self.orders_result


def _inject_mock_xtquant() -> None:
    """注入 fake xtquant 包到 sys.modules(若已注入则跳过)。"""
    if "xtquant" in sys.modules:
        return
    pkg = types.ModuleType("xtquant")
    xttrader = types.ModuleType("xtquant.xttrader")
    xttype = types.ModuleType("xtquant.xttype")
    xtconstant = types.ModuleType("xtquant.xtconstant")

    xttrader.XtQuantTrader = _FakeXtQuantTrader  # type: ignore[attr-defined]
    xttrader.XtQuantTraderCallback = _FakeCallback  # type: ignore[attr-defined]
    xttype.StockAccount = _FakeStockAccount  # type: ignore[attr-defined]

    # xtconstant 枚举值
    xtconstant.STOCK_BUY = 23  # type: ignore[attr-defined]
    xtconstant.STOCK_SELL = 24  # type: ignore[attr-defined]
    xtconstant.FIX_PRICE = 11  # type: ignore[attr-defined]
    xtconstant.LATEST_PRICE = 5  # type: ignore[attr-defined]

    pkg.xttrader = xttrader  # type: ignore[attr-defined]
    pkg.xttype = xttype  # type: ignore[attr-defined]
    pkg.xtconstant = xtconstant  # type: ignore[attr-defined]

    sys.modules["xtquant"] = pkg
    sys.modules["xtquant.xttrader"] = xttrader
    sys.modules["xtquant.xttype"] = xttype
    sys.modules["xtquant.xtconstant"] = xtconstant


_inject_mock_xtquant()

# 注入后 import 被测模块
from finboard_broker_qmt import _mapping  # noqa: E402
from finboard_broker_qmt.adapter import (  # noqa: E402
    XTQUANT_AVAILABLE,
    QmtBroker,
    create_broker,
)
from finboard_shared.identifiers import AccountId  # noqa: E402
from finboard_shared.types import (  # noqa: E402
    BrokerKind,
    OrderStatus,
    OrderType,
    Side,
)


# --------------------------------------------------------------------------- _mapping 测试
class TestMapping:
    @pytest.mark.unit
    def test_order_status_map(self) -> None:
        assert _mapping.xt_order_status_to_finboard(50) is OrderStatus.ACKNOWLEDGED
        assert _mapping.xt_order_status_to_finboard(54) is OrderStatus.CANCELLED
        assert _mapping.xt_order_status_to_finboard(56) is OrderStatus.FILLED
        assert _mapping.xt_order_status_to_finboard(57) is OrderStatus.REJECTED
        assert _mapping.xt_order_status_to_finboard(55) is OrderStatus.PARTIALLY_FILLED
        assert _mapping.xt_order_status_to_finboard(255) is OrderStatus.UNKNOWN
        # 未知码 → UNKNOWN
        assert _mapping.xt_order_status_to_finboard(999) is OrderStatus.UNKNOWN

    @pytest.mark.unit
    def test_field_compatibility_m_prefix(self) -> None:
        """旧 m_ 前缀字段应被兼容读取。"""
        obj = types.SimpleNamespace(m_dCash=999.0)
        assert _mapping._field(obj, "cash") == 999.0

    @pytest.mark.unit
    def test_xt_asset_to_account(self) -> None:
        asset = _FakeAsset(
            cash=500000.0, frozen_cash=1000.0, market_value=200000.0,
            total_asset=701000.0,
        )
        acct = _mapping.xt_asset_to_account(
            asset, AccountId("test"), BrokerKind.QMT
        )
        assert acct.cash == Decimal("500000.0")
        assert acct.frozen_cash == Decimal("1000.0")
        assert acct.total_asset == Decimal("701000.0")
        assert acct.broker_kind is BrokerKind.QMT

    @pytest.mark.unit
    def test_xt_position_to_position(self) -> None:
        pos = _FakePosition(
            stock_code="510300.SH", volume=1000, can_use_volume=800,
            frozen_volume=200, avg_price=3.85, market_value=3850.0,
        )
        result = _mapping.xt_position_to_position(pos, AccountId("test"))
        assert result.symbol.code == "510300.SH"
        assert result.total_quantity == Decimal("1000")
        assert result.available_quantity == Decimal("800")
        assert result.frozen_quantity == Decimal("200")
        assert result.average_price == Decimal("3.85")

    @pytest.mark.unit
    def test_xt_order_to_order(self) -> None:
        order = _FakeOrder(
            stock_code="600000.SH", order_id=12345, order_status=56,
            order_type=23, price_type=11, order_volume=100, price=10.5,
            traded_volume=100, traded_price=10.5, order_remark="F-test123",
        )
        result = _mapping.xt_order_to_order(
            order, account_id=AccountId("test"), broker_kind=BrokerKind.QMT
        )
        assert result.symbol.code == "600000.SH"
        assert result.status is OrderStatus.FILLED
        assert result.side is Side.BUY
        assert result.order_type is OrderType.LIMIT
        assert result.filled_quantity == Decimal("100")
        assert str(result.client_order_id) == "F-test123"


# --------------------------------------------------------------------------- adapter 测试
class TestQmtBroker:
    @pytest.mark.unit
    def test_xtquant_available(self) -> None:
        """mock 注入后 XTQUANT_AVAILABLE 应为 True。"""
        assert XTQUANT_AVAILABLE is True

    @pytest.mark.unit
    def test_create_broker(self) -> None:
        broker = create_broker(path="/fake/path", session_id=42)
        assert isinstance(broker, QmtBroker)
        assert broker.kind is BrokerKind.QMT

    @pytest.mark.unit
    async def test_connect_success(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        assert await broker.is_connected()
        await broker.disconnect()
        assert not await broker.is_connected()

    @pytest.mark.unit
    async def test_connect_failure_raises(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        # 让 connect() 返回非 0
        original_init = _FakeXtQuantTrader.__init__

        def _fail_init(self: Any, path: str, session_id: int) -> None:
            original_init(self, path, session_id)
            self.connect_rc = -1

        _FakeXtQuantTrader.__init__ = _fail_init  # type: ignore[method-assign]
        try:
            from finboard_shared.exceptions import BrokerError

            with pytest.raises(BrokerError, match="connect"):
                await broker.connect(AccountId("A123"), {})
        finally:
            _FakeXtQuantTrader.__init__ = original_init  # type: ignore[method-assign]

    @pytest.mark.unit
    async def test_query_account(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})

        # 配置 trader 的查询返回值
        broker._trader.asset_result = _FakeAsset(
            cash=800000.0, frozen_cash=0.0, market_value=100000.0,
            total_asset=900000.0,
        )

        acct = await broker.query_account()
        assert acct.cash == Decimal("800000.0")
        assert acct.total_asset == Decimal("900000.0")
        await broker.disconnect()

    @pytest.mark.unit
    async def test_query_positions(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})

        broker._trader.positions_result = [
            _FakePosition(
                stock_code="510300.SH", volume=200, can_use_volume=200,
                frozen_volume=0, avg_price=3.90, market_value=780.0,
            ),
            _FakePosition(
                stock_code="600000.SH", volume=100, can_use_volume=0,
                frozen_volume=100, avg_price=10.5, market_value=1050.0,
            ),
        ]

        positions = await broker.query_positions()
        assert len(positions) == 2
        assert positions[0].symbol.code == "510300.SH"
        assert positions[0].total_quantity == Decimal("200")
        assert positions[1].frozen_quantity == Decimal("100")
        await broker.disconnect()

    @pytest.mark.unit
    async def test_query_positions_none_returns_empty(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        broker._trader.positions_result = None
        assert await broker.query_positions() == []
        await broker.disconnect()

    @pytest.mark.unit
    async def test_query_active_orders_filters_terminal(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})

        broker._trader.orders_result = [
            _FakeOrder(  # 活动(已报)
                stock_code="510300.SH", order_id=1, order_status=50,
                order_type=23, price_type=11, order_volume=100, price=3.8,
                traded_volume=0, traded_price=0, order_remark="F-aaa",
            ),
            _FakeOrder(  # 终态(已成)
                stock_code="600000.SH", order_id=2, order_status=56,
                order_type=24, price_type=11, order_volume=100, price=10.5,
                traded_volume=100, traded_price=10.5, order_remark="F-bbb",
            ),
        ]

        orders = await broker.query_active_orders()
        assert len(orders) == 1
        assert orders[0].symbol.code == "510300.SH"
        assert orders[0].status is OrderStatus.ACKNOWLEDGED
        await broker.disconnect()

    @pytest.mark.unit
    async def test_not_connected_raises(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        from finboard_shared.exceptions import BrokerError

        with pytest.raises(BrokerError, match="未连接"):
            await broker.query_account()

    @pytest.mark.unit
    async def test_disconnected_event_pushed(self) -> None:
        """disconnect 后 events() 应能取到 DISCONNECTED 事件。"""
        from finboard_broker.events import BrokerEventType

        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        await broker.disconnect()

        events = []
        async for event in broker.events():
            events.append(event.type)
        assert BrokerEventType.DISCONNECTED in events
