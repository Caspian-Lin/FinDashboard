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


class _FakeTrade:
    """模拟 xtquant XtTrade(单笔成交回报)。"""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _FakeOrderError:
    """模拟 xtquant XtOrderError。"""

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
        # 交易方法可配置返回值 + 调用记录
        self.order_stock_result: int = 1
        self.cancel_order_stock_result: int = 0
        self.order_stock_calls: list[tuple[Any, ...]] = []
        self.cancel_order_stock_calls: list[Any] = []
        self.order_stock_delay: float = 0.0
        self.cancel_order_stock_delay: float = 0.0

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

    def order_stock(
        self,
        account: Any,
        stock_code: str,
        order_type: int,
        order_volume: float,
        price_type: int,
        price: float = 0,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> int:
        import time

        self.order_stock_calls.append(
            (stock_code, order_type, order_volume, price_type, price, strategy_name, order_remark)
        )
        if self.order_stock_delay > 0:
            time.sleep(self.order_stock_delay)
        return self.order_stock_result

    def cancel_order_stock(self, account: Any, order_id: int) -> int:
        import time

        self.cancel_order_stock_calls.append(order_id)
        if self.cancel_order_stock_delay > 0:
            time.sleep(self.cancel_order_stock_delay)
        return self.cancel_order_stock_result


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
from finboard_broker.events import BrokerEventType  # noqa: E402
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

    @pytest.mark.unit
    def test_order_remark_encode(self) -> None:
        """F-YYYYMMDD-<16hex> → YYYYMMDD<16hex> (24 字符)。"""
        cid = "F-20260722-aabbccdd11223344"
        remark = _mapping.encode_order_remark(cid)
        assert len(remark) == 24
        assert remark == "20260722aabbccdd11223344"

    @pytest.mark.unit
    def test_order_remark_decode(self) -> None:
        """YYYYMMDD<16hex> → F-YYYYMMDD-<16hex>。"""
        remark = "20260722aabbccdd11223344"
        cid = _mapping.decode_order_remark(remark)
        assert cid == "F-20260722-aabbccdd11223344"

    @pytest.mark.unit
    def test_order_remark_roundtrip(self) -> None:
        """编解码往返不丢失信息。"""
        cid = "F-20260722-deadbeefcafebabe"
        assert _mapping.decode_order_remark(_mapping.encode_order_remark(cid)) == cid

    @pytest.mark.unit
    def test_order_remark_encode_nonstandard_truncates(self) -> None:
        """非标准格式截断到 24 字符。"""
        long_str = "X" * 30
        assert _mapping.encode_order_remark(long_str) == "X" * 24

    @pytest.mark.unit
    def test_order_remark_decode_nonstandard_passthrough(self) -> None:
        """非标准格式原样返回。"""
        assert _mapping.decode_order_remark("short") == "short"

    @pytest.mark.unit
    def test_side_to_xt_order_type(self) -> None:
        assert _mapping.side_to_xt_order_type(Side.BUY) == 23
        assert _mapping.side_to_xt_order_type(Side.SELL) == 24

    @pytest.mark.unit
    def test_order_type_to_xt_price_type(self) -> None:
        assert _mapping.order_type_to_xt_price_type(OrderType.LIMIT) == 11
        assert _mapping.order_type_to_xt_price_type(OrderType.MARKET) == 5

    @pytest.mark.unit
    def test_xt_trade_to_fill(self) -> None:
        trade = _FakeTrade(
            stock_code="510300.SH",
            filled_id="T123456",
            order_id=99,
            traded_volume=200,
            traded_price=3.92,
            order_remark="20260722aabbccdd11223344",
            order_type=23,
        )
        fill = _mapping.xt_trade_to_fill(trade, broker_kind=BrokerKind.QMT)
        assert fill.fill_id == "T123456"
        assert fill.quantity == Decimal("200")
        assert fill.price == Decimal("3.92")
        assert fill.symbol.code == "510300.SH"
        assert fill.side is Side.BUY
        assert str(fill.client_order_id) == "F-20260722-aabbccdd11223344"
        assert fill.broker_order_id == "99"


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

    # ----------------------------------------------------------- 交易(issue #4)
    def _make_order(
        self,
        cid: str = "F-20260722-aabbccdd11223344",
        side: Side = Side.BUY,
        order_type: OrderType = OrderType.LIMIT,
        price: Decimal | None = Decimal("3.85"),
    ) -> Any:
        from finboard_shared.identifiers import ClientOrderId
        from finboard_shared.models import Order, Symbol
        from finboard_shared.types import Market

        return Order(
            client_order_id=ClientOrderId(cid),
            account_id=AccountId("A123"),
            broker_kind=BrokerKind.QMT,
            symbol=Symbol(code="510300.SH", market=Market.A_SHARE),
            side=side,
            order_type=order_type,
            quantity=Decimal("100"),
            price=price,
        )

    @pytest.mark.unit
    async def test_place_order_success(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        broker._trader.order_stock_result = 42

        order = self._make_order()
        result = await broker.place_order(order)

        assert result.accepted
        assert result.broker_order_id == "42"
        # 验证 order_stock 参数转换
        call = broker._trader.order_stock_calls[-1]
        assert call[0] == "510300.SH"       # stock_code
        assert call[1] == 23                 # STOCK_BUY
        assert call[2] == 100.0              # volume
        assert call[3] == 11                 # FIX_PRICE
        assert call[4] == 3.85               # price
        assert call[6] == "20260722aabbccdd11223344"  # remark(编码后)
        # 契约:status / filled_quantity 未被修改
        assert order.status is OrderStatus.CREATED
        assert order.filled_quantity == Decimal("0")
        # broker_order_id 已写入
        assert order.broker_order_id == "42"
        # 映射已记录
        assert broker._cid_to_broker_id["F-20260722-aabbccdd11223344"] == "42"
        await broker.disconnect()

    @pytest.mark.unit
    async def test_place_order_sell_market(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        broker._trader.order_stock_result = 7

        order = self._make_order(side=Side.SELL, order_type=OrderType.MARKET, price=None)
        await broker.place_order(order)

        call = broker._trader.order_stock_calls[-1]
        assert call[1] == 24   # STOCK_SELL
        assert call[3] == 5    # LATEST_PRICE
        assert call[4] == 0.0  # 市价单 price=0
        await broker.disconnect()

    @pytest.mark.unit
    async def test_place_order_rejected(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        broker._trader.order_stock_result = -1

        order = self._make_order()
        result = await broker.place_order(order)
        assert not result.accepted
        await broker.disconnect()

    @pytest.mark.unit
    async def test_place_order_timeout(self) -> None:
        import finboard_broker_qmt.adapter as adapter_mod
        from finboard_shared.exceptions import BrokerTimeoutError

        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        broker._trader.order_stock_delay = 2.0

        original = adapter_mod.PLACE_ORDER_TIMEOUT
        adapter_mod.PLACE_ORDER_TIMEOUT = 0.05
        try:
            with pytest.raises(BrokerTimeoutError, match="order_stock 超时"):
                await broker.place_order(self._make_order())
        finally:
            adapter_mod.PLACE_ORDER_TIMEOUT = original
        await broker.disconnect()

    @pytest.mark.unit
    async def test_cancel_order_success(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        broker._trader.order_stock_result = 42

        order = self._make_order()
        await broker.place_order(order)
        await broker.cancel_order(str(order.client_order_id))

        assert broker._trader.cancel_order_stock_calls[-1] == 42
        await broker.disconnect()

    @pytest.mark.unit
    async def test_cancel_order_not_found(self) -> None:
        from finboard_shared.exceptions import OrderNotFoundError

        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        with pytest.raises(OrderNotFoundError):
            await broker.cancel_order("unknown-cid")
        await broker.disconnect()

    @pytest.mark.unit
    async def test_cancel_order_timeout(self) -> None:
        import finboard_broker_qmt.adapter as adapter_mod
        from finboard_shared.exceptions import BrokerTimeoutError

        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        broker._trader.order_stock_result = 42
        order = self._make_order()
        await broker.place_order(order)

        broker._trader.cancel_order_stock_delay = 2.0
        original = adapter_mod.CANCEL_ORDER_TIMEOUT
        adapter_mod.CANCEL_ORDER_TIMEOUT = 0.05
        try:
            with pytest.raises(BrokerTimeoutError, match="cancel_order_stock 超时"):
                await broker.cancel_order(str(order.client_order_id))
        finally:
            adapter_mod.CANCEL_ORDER_TIMEOUT = original
        await broker.disconnect()

    # ----------------------------------------------------------- 回报转换(issue #4)
    @staticmethod
    def _drain_queue(broker: QmtBroker) -> None:
        """排空 connect 产生的 CONNECTED 事件。"""
        while not broker._event_queue.empty():
            broker._event_queue.get_nowait()

    @pytest.mark.unit
    async def test_on_stock_order_accepted(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        self._drain_queue(broker)

        data = _FakeOrder(
            order_id=42, order_status=50,
            order_remark="20260722aabbccdd11223344",
        )
        await broker._on_xt_order(data)
        event = broker._event_queue.get_nowait()
        assert event.type is BrokerEventType.ORDER_ACCEPTED
        assert event.broker_order_id == "42"
        assert str(event.client_order_id) == "F-20260722-aabbccdd11223344"
        await broker.disconnect()

    @pytest.mark.unit
    async def test_on_stock_order_cancelled(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        self._drain_queue(broker)

        data = _FakeOrder(
            order_id=42, order_status=54,
            order_remark="20260722aabbccdd11223344",
        )
        await broker._on_xt_order(data)
        event = broker._event_queue.get_nowait()
        assert event.type is BrokerEventType.ORDER_CANCELLED
        await broker.disconnect()

    @pytest.mark.unit
    async def test_on_stock_order_rejected(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        self._drain_queue(broker)

        data = _FakeOrder(
            order_id=42, order_status=57,
            order_remark="20260722aabbccdd11223344",
        )
        await broker._on_xt_order(data)
        event = broker._event_queue.get_nowait()
        assert event.type is BrokerEventType.ORDER_REJECTED
        assert event.reject_reason is not None
        await broker.disconnect()

    @pytest.mark.unit
    async def test_on_stock_trade(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        self._drain_queue(broker)

        data = _FakeTrade(
            stock_code="510300.SH", filled_id="T001",
            order_id=42, traded_volume=100, traded_price=3.85,
            order_remark="20260722aabbccdd11223344", order_type=23,
        )
        await broker._on_xt_trade(data)
        event = broker._event_queue.get_nowait()
        assert event.type is BrokerEventType.ORDER_FILLED
        assert event.fill is not None
        assert event.fill.quantity == Decimal("100")
        assert event.fill.price == Decimal("3.85")
        assert str(event.client_order_id) == "F-20260722-aabbccdd11223344"
        await broker.disconnect()

    @pytest.mark.unit
    async def test_on_order_error(self) -> None:
        broker = QmtBroker(path="/fake", session_id=1)
        await broker.connect(AccountId("A123"), {})
        self._drain_queue(broker)
        # 先建立映射
        broker._record_oid_mapping("F-20260722-aabbccdd11223344", "42")

        data = _FakeOrderError(order_id=42, error_id=1, error_msg="资金不足")
        await broker._on_xt_order_error(data)
        event = broker._event_queue.get_nowait()
        assert event.type is BrokerEventType.ORDER_REJECTED
        assert "资金不足" in event.message
        assert str(event.client_order_id) == "F-20260722-aabbccdd11223344"
        await broker.disconnect()
