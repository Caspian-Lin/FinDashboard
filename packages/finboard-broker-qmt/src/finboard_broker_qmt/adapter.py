"""QMT (xtquant) BrokerAdapter 实现 —— A 股。

架构要点:

1. **xtquant 是同步 SDK**,所有查询调用(``query_stock_asset`` 等)通过
   ``loop.run_in_executor`` 转到线程池,不阻塞 event loop。

2. **SDK 回调在内部线程触发**,不能直接操作 asyncio 对象。
   :class:`_QmtCallback` 在回调线程里用 ``loop.call_soon_threadsafe``
   把 raw 事件推到 ``asyncio.Queue``,``events()`` 在 asyncio 侧消费。

3. **xtquant 仅 Windows + miniQMT 可用**。``try: import xtquant`` 失败时
   :data:`XTQUANT_AVAILABLE` 为 False,工厂函数抛清晰错误。

红线(AGENTS.md):

* ``order_remark``(24 字符)是本地 ``client_order_id`` 关联券商回报的唯一通道;
  编解码见 :mod:`finboard_broker_qmt._mapping` 的 ``encode/decode_order_remark``。
* ``order_status == 255 (UNKNOWN)`` 是超时兜底,不重试。
* ``place_order`` / ``cancel_order`` 超时后抛 ``BrokerTimeoutError``,由
  OrderManager 置 ``UNKNOWN`` —— **禁止无条件重试**。
* ``place_order`` 不修改传入 Order 的 ``status`` / ``filled_quantity``
  (与 MockBroker 契约一致);状态推进由 OrderManager 通过回报事件完成。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

from finboard_broker.base import BrokerAdapter, SubmissionResult
from finboard_broker.events import BrokerEvent, BrokerEventType
from finboard_shared.exceptions import BrokerError, BrokerTimeoutError, OrderNotFoundError
from finboard_shared.identifiers import AccountId
from finboard_shared.models import Fill
from finboard_shared.types import BrokerKind, OrderStatus, RejectReason

if TYPE_CHECKING:
    from finboard_shared.models import Account, Order, Position

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- 超时常量(秒)
#: order_stock 超时 —— 超时后抛 BrokerTimeoutError,订单进 UNKNOWN(红线)
PLACE_ORDER_TIMEOUT: float = 10.0
#: cancel_order_stock 超时
CANCEL_ORDER_TIMEOUT: float = 5.0

# --------------------------------------------------------------------------- xtquant 可选导入
try:
    from xtquant import xtconstant as _xtconstant  # noqa: F401
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount

    XTQUANT_AVAILABLE = True
except ImportError:
    XtQuantTrader = None
    XtQuantTraderCallback = object
    StockAccount = None
    XTQUANT_AVAILABLE = False

XTQUANT_INSTALL_HINT = (
    "xtquant 未安装。xtquant 是迅投 QMT 客户端自带的 Python SDK,"
    " 仅在安装了 miniQMT 的 Windows 环境可用。\n"
    " 请确认:\n"
    "  1. 已安装 QMT 客户端并启动 miniQMT;\n"
    "  2. xtquant 路径(<QMT安装目录>/userdata_mini)已加入 PYTHONPATH;\n"
    "  3. 运行环境为 Windows。\n"
    " 本地开发 / CI 请使用 mock broker:FINBOARD_BROKER=mock"
)


class _QmtCallback(XtQuantTraderCallback):  # type: ignore[misc]
    """SDK 回调 → asyncio.Queue 桥接。

    所有方法在 **xtquant 内部线程** 执行,通过 ``call_soon_threadsafe``
    把 raw 回调对象推到 asyncio 侧。转换在 asyncio 线程完成(见
    :meth:`QmtBroker._consume_raw_callbacks`)。

    线程安全说明:``call_soon_threadsafe`` 是 asyncio 官方推荐的
    跨线程通信方式,内部有锁,可安全调用。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        # raw 回调队列;元素是 (callback_name, data_obj) 元组
        self._raw_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def _push(self, name: str, data: Any = None) -> None:
        self._loop.call_soon_threadsafe(self._raw_queue.put_nowait, (name, data))

    def on_disconnected(self) -> None:
        logger.warning("qmt.disconnected")
        self._push("on_disconnected")

    def on_stock_order(self, data: Any) -> None:
        self._push("on_stock_order", data)

    def on_stock_trade(self, data: Any) -> None:
        self._push("on_stock_trade", data)

    def on_order_error(self, data: Any) -> None:
        self._push("on_order_error", data)

    def on_cancel_error(self, data: Any) -> None:
        self._push("on_cancel_error", data)


class QmtBroker(BrokerAdapter):
    """QMT (xtquant) 券商适配器。

    构造参数:

    * ``path`` —— miniQMT 的 ``userdata_mini`` 完整路径;
    * ``session_id`` —— 会话号(int),同时运行的策略必须互不重复。

    生命周期:``connect`` → ``query_*`` / ``place_order`` → ``disconnect``。
    """

    def __init__(self, *, path: str, session_id: int) -> None:
        if not XTQUANT_AVAILABLE:
            raise ImportError(XTQUANT_INSTALL_HINT)

        self._path = path
        self._session_id = session_id
        self._trader: Any = None  # XtQuantTrader 实例(Any 因类型仅 Windows 存在)
        self._account: Any = None  # StockAccount
        self._account_id: AccountId | None = None
        self._connected: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._callback: _QmtCallback | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        # raw callback → BrokerEvent 转换队列(由 events() 消费)
        self._event_queue: asyncio.Queue[BrokerEvent] = asyncio.Queue()
        # client_order_id ↔ broker_order_id 双向映射(cancel_order / 回报反查用)
        self._cid_to_broker_id: dict[str, str] = {}
        self._broker_id_to_cid: dict[str, str] = {}

    @property
    def kind(self) -> BrokerKind:
        return BrokerKind.QMT

    # ------------------------------------------------------------------ 连接
    async def connect(
        self, account_id: AccountId, credentials: Mapping[str, str]
    ) -> None:
        assert XtQuantTrader is not None  # narrowed by XTQUANT_AVAILABLE
        assert StockAccount is not None
        if self._connected:
            return

        self._account_id = account_id
        self._loop = asyncio.get_running_loop()
        self._callback = _QmtCallback(self._loop)

        # XtQuantTrader 初始化 + start + connect + subscribe 全是同步调用,
        # 放到 executor 里执行(connect 可能阻塞数百 ms)
        await self._loop.run_in_executor(None, self._sync_connect)

        # 启动 raw callback 消费 task(把 _callback._raw_queue 转换到 _event_queue)
        self._consumer_task = asyncio.create_task(
            self._consume_raw_callbacks(), name="qmt-callback-consumer"
        )

        await self._push_event(BrokerEventType.CONNECTED, message=str(account_id))
        logger.info(
            "qmt.connected",
            extra={"account_id": str(account_id), "session_id": self._session_id},
        )

    def _sync_connect(self) -> None:
        """在 executor 线程里执行同步连接序列。"""
        assert self._account_id is not None
        assert self._callback is not None
        trader = XtQuantTrader(self._path, self._session_id)
        trader.register_callback(self._callback)
        trader.start()
        rc = trader.connect()
        if rc != 0:
            raise BrokerError(
                f"QMT connect() 返回 {rc} —— 请确认 miniQMT 客户端已启动"
                f" 且 path={self._path} 正确"
            )
        account = StockAccount(str(self._account_id), "STOCK")
        sub_rc = trader.subscribe(account)
        if sub_rc != 0:
            raise BrokerError(
                f"QMT subscribe() 返回 {sub_rc} —— 账号 {self._account_id}"
                f" 订阅失败,请确认资金账号正确"
            )
        self._trader = trader
        self._account = account
        self._connected = True

    async def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None
        if self._trader is not None and self._loop is not None:
            await self._loop.run_in_executor(None, self._trader.stop)
        self._trader = None
        self._account = None
        await self._push_event(BrokerEventType.DISCONNECTED)
        logger.info("qmt.disconnected")

    async def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ 查询
    async def query_account(self) -> Account:
        self._require_connected()
        asset = await self._loop.run_in_executor(  # type: ignore[union-attr]
            None, self._trader.query_stock_asset, self._account
        )
        if asset is None:
            raise BrokerError("query_stock_asset 返回 None —— 查询失败或当日无数据")
        from finboard_broker_qmt._mapping import xt_asset_to_account

        assert self._account_id is not None
        return xt_asset_to_account(asset, self._account_id, self.kind)

    async def query_positions(self) -> list[Position]:
        self._require_connected()
        raw = await self._loop.run_in_executor(  # type: ignore[union-attr]
            None, self._trader.query_stock_positions, self._account
        )
        if raw is None:
            return []
        from finboard_broker_qmt._mapping import xt_position_to_position

        assert self._account_id is not None
        return [
            xt_position_to_position(p, self._account_id) for p in raw
        ]

    async def query_order(self, client_order_id: str) -> Order | None:
        """查单笔订单(含已终结),用于 UNKNOWN 恢复流程的兜底查询。

        与 ``query_active_orders`` 不同,本方法搜索**全部当日委托**(含 FILLED /
        CANCELLED),因为 UNKNOWN 订单可能在券商侧已成交/已撤,此时
        ``query_active_orders`` 不会返回它。
        """
        all_orders = await self._query_all_today_orders()
        for o in all_orders:
            if str(o.client_order_id) == client_order_id:
                return o
        return None

    async def query_active_orders(self) -> list[Order]:
        all_orders = await self._query_all_today_orders()
        return [o for o in all_orders if o.is_active]

    async def _query_all_today_orders(self) -> list[Order]:
        """查询全部当日委托(含已终结),供 query_order / query_active_orders 复用。"""
        self._require_connected()
        raw = await self._loop.run_in_executor(  # type: ignore[union-attr]
            None,
            lambda: self._trader.query_stock_orders(self._account, cancelable_only=False),
        )
        if raw is None:
            return []
        from finboard_broker_qmt._mapping import xt_order_to_order

        assert self._account_id is not None
        return [
            xt_order_to_order(
                o, account_id=self._account_id, broker_kind=self.kind
            )
            for o in raw
        ]

    # ------------------------------------------------------------------ 交易
    async def place_order(self, order: Order) -> SubmissionResult:
        """提交订单到 QMT。

        红线:
        * 超时 → ``BrokerTimeoutError``(不重试,由 OrderManager 置 UNKNOWN);
        * 不修改 ``order.status`` / ``order.filled_quantity``(契约一致);
        * ``client_order_id`` 编码进 ``order_remark``(24 字符限制)。

        xtquant ``order_stock`` 签名::

            order_stock(account, stock_code, order_type, order_volume,
                        price_type, price=0, strategy_name='', order_remark='')

        返回 ``int``: > 0 = order_id(成功), -1 = 失败。
        """
        self._require_connected()
        from finboard_broker_qmt._mapping import (
            encode_order_remark,
            order_type_to_xt_price_type,
            side_to_xt_order_type,
        )

        assert self._loop is not None
        assert self._trader is not None
        assert self._account is not None

        cid = str(order.client_order_id)
        remark = encode_order_remark(cid)
        xt_order_type = side_to_xt_order_type(order.side)
        xt_price_type = order_type_to_xt_price_type(order.order_type)
        price = float(order.price) if order.price is not None else 0.0

        try:
            async with asyncio.timeout(PLACE_ORDER_TIMEOUT):
                order_id = await self._loop.run_in_executor(
                    None,
                    self._trader.order_stock,
                    self._account,
                    str(order.symbol.code),
                    xt_order_type,
                    float(order.quantity),
                    xt_price_type,
                    price,
                    "",  # strategy_name
                    remark,
                )
        except TimeoutError:
            raise BrokerTimeoutError(
                f"QMT order_stock 超时({PLACE_ORDER_TIMEOUT}s): {cid}"
            ) from None

        if order_id is None or order_id < 0:
            logger.warning("qmt.place_order_rejected", extra={"cid": cid})
            return SubmissionResult(
                client_order_id=order.client_order_id,
                accepted=False,
                reject_reason=RejectReason.BROKER_REJECTED,
            )

        broker_order_id = str(order_id)
        # 契约:只允许写 broker_order_id,不碰 status / filled_quantity
        order.broker_order_id = broker_order_id
        self._record_oid_mapping(cid, broker_order_id)
        logger.info(
            "qmt.place_order_ok",
            extra={"cid": cid, "broker_order_id": broker_order_id},
        )
        return SubmissionResult(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            accepted=True,
        )

    async def cancel_order(self, client_order_id: str) -> None:
        """撤销订单。超时 → ``BrokerTimeoutError``(不重试)。"""
        self._require_connected()
        assert self._loop is not None
        assert self._trader is not None
        assert self._account is not None

        broker_order_id = self._cid_to_broker_id.get(client_order_id)
        if broker_order_id is None:
            raise OrderNotFoundError(
                f"无法撤单:找不到 {client_order_id} 对应的 broker_order_id"
            )

        try:
            async with asyncio.timeout(CANCEL_ORDER_TIMEOUT):
                rc = await self._loop.run_in_executor(
                    None,
                    self._trader.cancel_order_stock,
                    self._account,
                    int(broker_order_id),
                )
        except TimeoutError:
            raise BrokerTimeoutError(
                f"QMT cancel_order_stock 超时({CANCEL_ORDER_TIMEOUT}s): "
                f"{client_order_id}"
            ) from None

        if rc != 0:
            raise BrokerError(
                f"QMT cancel_order_stock 返回 {rc}: {client_order_id}"
            )
        logger.info(
            "qmt.cancel_order_ok",
            extra={"cid": client_order_id, "broker_order_id": broker_order_id},
        )

    # ------------------------------------------------------------------ 事件流
    def events(self) -> AsyncIterator[BrokerEvent]:
        return self._event_iterator()

    async def _event_iterator(self) -> AsyncIterator[BrokerEvent]:
        while self._connected or not self._event_queue.empty():
            event = await self._event_queue.get()
            yield event

    # ------------------------------------------------------------------ raw callback 消费
    async def _consume_raw_callbacks(self) -> None:
        """把 _callback._raw_queue 的 raw 回调转换为 BrokerEvent。

        回调转换逻辑(回报状态映射等)在 issue #4 补全;本 issue 只处理
        连接级事件(disconnected)。
        """
        assert self._callback is not None
        while True:
            try:
                name, data = await self._callback._raw_queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._handle_raw_callback(name, data)
            except Exception:
                logger.exception(
                    "qmt.callback_handler_failed", extra={"callback": name}
                )

    async def _handle_raw_callback(self, name: str, data: Any) -> None:
        """单个 raw 回调 → BrokerEvent。"""
        if name == "on_disconnected":
            self._connected = False
            await self._push_event(BrokerEventType.DISCONNECTED)
            logger.warning("qmt.on_disconnected_marked_offline")
        elif name == "on_stock_order":
            await self._on_xt_order(data)
        elif name == "on_stock_trade":
            await self._on_xt_trade(data)
        elif name == "on_order_error":
            await self._on_xt_order_error(data)
        elif name == "on_cancel_error":
            # 撤单错误 — 推 CANCEL_REJECTED,由 OrderManager 决定后续动作
            await self._on_xt_cancel_error(data)

    async def _on_xt_order(self, data: Any) -> None:
        """``on_stock_order(XtOrder)`` — 委托状态变化 → BrokerEvent。

        xtquant order_status 映射::

            50(已报)   → ORDER_ACCEPTED
            53/54(已撤) → ORDER_CANCELLED
            57(废单)   → ORDER_REJECTED
            55/56(部成/已成) → 不在这里推(成交明细由 on_stock_trade 驱动)
        """
        from finboard_broker_qmt._mapping import (
            _field,
            decode_order_remark,
            xt_order_status_to_finboard,
        )

        status_code = int(_field(data, "order_status", 255))
        status = xt_order_status_to_finboard(status_code)
        broker_order_id = str(_field(data, "order_id", ""))
        order_remark = str(_field(data, "order_remark", ""))
        cid_str = decode_order_remark(order_remark) if order_remark else ""

        if cid_str and broker_order_id:
            self._record_oid_mapping(cid_str, broker_order_id)

        if not cid_str:
            logger.warning(
                "qmt.order_callback_no_cid",
                extra={"broker_order_id": broker_order_id, "status_code": status_code},
            )
            return

        if status is OrderStatus.ACKNOWLEDGED:
            await self._push_event(
                BrokerEventType.ORDER_ACCEPTED,
                client_order_id=cid_str,
                broker_order_id=broker_order_id,
            )
        elif status is OrderStatus.CANCELLED:
            await self._push_event(
                BrokerEventType.ORDER_CANCELLED,
                client_order_id=cid_str,
                broker_order_id=broker_order_id,
            )
        elif status is OrderStatus.REJECTED:
            await self._push_event(
                BrokerEventType.ORDER_REJECTED,
                client_order_id=cid_str,
                broker_order_id=broker_order_id,
                reject_reason=RejectReason.BROKER_REJECTED,
                message=f"QMT 废单(order_status={status_code})",
            )
        # PARTIALLY_FILLED / FILLED:成交数据由 on_stock_trade 提供,不重复推

    async def _on_xt_trade(self, data: Any) -> None:
        """``on_stock_trade(XtTrade)`` — 单笔成交 → ORDER_FILLED。"""
        from finboard_broker_qmt._mapping import xt_trade_to_fill

        fill = xt_trade_to_fill(data, broker_kind=self.kind)
        if not fill.client_order_id:
            logger.warning("qmt.trade_callback_no_cid", extra={"fill_id": fill.fill_id})
            return
        await self._push_event(
            BrokerEventType.ORDER_FILLED,
            client_order_id=str(fill.client_order_id),
            broker_order_id=fill.broker_order_id,
            fill=fill,
        )

    async def _on_xt_order_error(self, data: Any) -> None:
        """``on_order_error(XtOrderError)`` — 下单/柜台错误 → ORDER_REJECTED。

        XtOrderError 字段:``order_id``、``error_id``、``error_msg``。
        没有 ``order_remark``,通过 ``order_id`` 反查 ``client_order_id``。
        """
        from finboard_broker_qmt._mapping import _field

        broker_order_id = str(_field(data, "order_id", ""))
        error_msg = str(_field(data, "error_msg", "")) or "未知错误"
        cid_str = self._broker_id_to_cid.get(broker_order_id)

        if cid_str is None:
            logger.warning(
                "qmt.order_error_no_cid",
                extra={"broker_order_id": broker_order_id, "error_msg": error_msg},
            )
            return

        await self._push_event(
            BrokerEventType.ORDER_REJECTED,
            client_order_id=cid_str,
            broker_order_id=broker_order_id,
            reject_reason=RejectReason.BROKER_REJECTED,
            message=f"QMT order_error: {error_msg}",
        )

    async def _on_xt_cancel_error(self, data: Any) -> None:
        """``on_cancel_error`` — 撤单失败。仅记录日志,不推事件。

        撤单的终态(成功/失败)最终会通过 ``on_stock_order`` 的状态变化体现
        (53/54 = 已撤,或回到 55/56 = 已成交无法撤)。
        """
        from finboard_broker_qmt._mapping import _field

        broker_order_id = str(_field(data, "order_id", ""))
        error_msg = str(_field(data, "error_msg", ""))
        logger.warning(
            "qmt.cancel_error",
            extra={"broker_order_id": broker_order_id, "error_msg": error_msg},
        )

    # ------------------------------------------------------------------ 内部
    def _record_oid_mapping(self, cid: str, broker_order_id: str) -> None:
        """记录 client_order_id ↔ broker_order_id 双向映射。"""
        self._cid_to_broker_id[cid] = broker_order_id
        self._broker_id_to_cid[broker_order_id] = cid

    async def _push_event(
        self,
        event_type: BrokerEventType,
        *,
        client_order_id: Any = None,
        broker_order_id: str | None = None,
        message: str = "",
        fill: Fill | None = None,
        reject_reason: RejectReason | None = None,
    ) -> None:
        from finboard_shared.identifiers import ClientOrderId

        await self._event_queue.put(
            BrokerEvent(
                type=event_type,
                client_order_id=ClientOrderId(client_order_id)
                if client_order_id
                else None,
                broker_order_id=broker_order_id,
                message=message,
                fill=fill,
                reject_reason=reject_reason,
            )
        )

    def _require_connected(self) -> None:
        if not self._connected or self._trader is None:
            raise BrokerError("QmtBroker 未连接")


# --------------------------------------------------------------------------- 工厂
def create_broker(**kwargs: Any) -> BrokerAdapter:
    """供 :func:`finboard_broker.factory.create_broker` 调用。

    由 bootstrap 通过 ``**broker_credentials(settings)`` 注入 path / session_id。
    """
    if not XTQUANT_AVAILABLE:
        raise ImportError(XTQUANT_INSTALL_HINT)
    path = kwargs.get("path", "")
    session_id = int(kwargs.get("session_id", 1))
    return QmtBroker(path=path, session_id=session_id)
