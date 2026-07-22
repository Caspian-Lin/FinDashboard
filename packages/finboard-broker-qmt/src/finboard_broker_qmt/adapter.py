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
* ``order_status == 255 (UNKNOWN)`` 是超时兜底,不重试。
  (下单/撤单/回报转换在 issue #4 实现,本 issue 只覆盖连接 + 查询。)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

from finboard_broker.base import BrokerAdapter
from finboard_broker.events import BrokerEvent, BrokerEventType
from finboard_shared.exceptions import BrokerError
from finboard_shared.identifiers import AccountId
from finboard_shared.types import BrokerKind

if TYPE_CHECKING:
    from finboard_shared.models import Account, Order, Position

logger = logging.getLogger(__name__)

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
        orders = await self.query_active_orders()
        for o in orders:
            if str(o.client_order_id) == client_order_id:
                return o
        return None

    async def query_active_orders(self) -> list[Order]:
        self._require_connected()
        raw = await self._loop.run_in_executor(  # type: ignore[union-attr]
            None,
            lambda: self._trader.query_stock_orders(self._account, cancelable_only=False),
        )
        if raw is None:
            return []
        from finboard_broker_qmt._mapping import xt_order_to_order

        assert self._account_id is not None
        orders = [
            xt_order_to_order(
                o, account_id=self._account_id, broker_kind=self.kind
            )
            for o in raw
        ]
        # 只返回活动订单(query 接口返回全部当日委托)
        return [o for o in orders if o.is_active]

    # ------------------------------------------------------------------ 交易(issue #4)
    async def place_order(self, order: Order) -> Any:
        raise NotImplementedError(
            "place_order 将在 issue #4(QMT 交易接口 + 回报流)实现"
        )

    async def cancel_order(self, client_order_id: str) -> None:
        raise NotImplementedError(
            "cancel_order 将在 issue #4(QMT 交易接口 + 回报流)实现"
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
        """单个 raw 回调 → BrokerEvent(连接级;交易回报在 issue #4)。"""
        if name == "on_disconnected":
            self._connected = False
            await self._push_event(BrokerEventType.DISCONNECTED)
            logger.warning("qmt.on_disconnected_marked_offline")
        # on_stock_order / on_stock_trade / on_order_error → issue #4
        # 暂时忽略(避免 log noise)

    # ------------------------------------------------------------------ 内部
    async def _push_event(
        self,
        event_type: BrokerEventType,
        *,
        client_order_id: Any = None,
        broker_order_id: str | None = None,
        message: str = "",
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
            )
        )

    def _require_connected(self) -> None:
        if not self._connected or self._trader is None:
            raise BrokerError("QmtBroker 未连接")


# --------------------------------------------------------------------------- 工厂
def create_broker(**kwargs: Any) -> BrokerAdapter:
    """供 :func:`finboard_broker.factory.create_broker` 调用。

    factory 不传参数,这里用 kwargs 接收 path / session_id(由 bootstrap
    通过 credentials 注入)。P1 集成(issue #5)会改为从 credentials 读取。
    """
    if not XTQUANT_AVAILABLE:
        raise ImportError(XTQUANT_INSTALL_HINT)
    path = kwargs.get("path", "")
    session_id = int(kwargs.get("session_id", 1))
    return QmtBroker(path=path, session_id=session_id)
