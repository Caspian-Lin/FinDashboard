"""``BrokerAdapter`` 抽象基类与下单返回值类型。

实现方义务(详见 AGENTS.md §交易安全红线):

* 所有方法 ``async``。底层为同步 SDK 时,在实现内部用 ``run_in_executor``
  或独立线程桥接,不要阻塞 event loop。
* 查询类方法(``query_*``)允许内部有限重试(指数退避,上限 3 次)。
* 交易类方法(``place_order`` / ``cancel_order``)**禁止**自动重试。
  超时必须抛 ``BrokerTimeoutError``,由上层把订单置入 ``UNKNOWN`` 流程。
* 回报事件必须可被 ``events()`` 异步消费,且要保证 at-least-once
  (实现方需在重连后重放当日本地未确认的事件)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

from finboard_broker.events import BrokerEvent
from finboard_shared.identifiers import AccountId, ClientOrderId
from finboard_shared.models import Account, Order, Position
from finboard_shared.types import BrokerKind, RejectReason


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """``place_order`` 的同步返回。

    表达"broker 已经接受了下单请求",**不等于**订单已成交。
    最终状态变化通过 ``events()`` 流回到 OrderManager。
    """

    client_order_id: ClientOrderId
    broker_order_id: str | None = None
    accepted: bool = True
    reject_reason: RejectReason | None = None


class BrokerAdapter(ABC):
    """券商接口统一抽象。"""

    @property
    @abstractmethod
    def kind(self) -> BrokerKind:
        """实现对应的 broker 类型,用于持久化 ``orders.broker_kind``。"""

    @abstractmethod
    async def connect(
        self, account_id: AccountId, credentials: Mapping[str, str]
    ) -> None:
        """建立连接;``credentials`` 由 ``finboard_app.config`` 注入(脱敏后不入日志)。"""

    @abstractmethod
    async def disconnect(self) -> None:
        """优雅断开;必须保证已 push 的事件不被吞掉。"""

    @abstractmethod
    async def is_connected(self) -> bool:
        """连接健康度;OrderManager 周期性探活用。"""

    # ----------------------------- 查询类 -----------------------------

    @abstractmethod
    async def query_account(self) -> Account:
        """查询账户资金快照。"""

    @abstractmethod
    async def query_positions(self) -> list[Position]:
        """查询全部持仓(券商侧真值)。"""

    @abstractmethod
    async def query_order(self, client_order_id: str) -> Order | None:
        """根据 ``client_order_id`` 查单笔订单;用于 UNKNOWN 流程的兜底查询。"""

    @abstractmethod
    async def query_active_orders(self) -> list[Order]:
        """查询当前活动订单。"""

    # ----------------------------- 交易类 -----------------------------
    # 注意:不要在超时后自动重试!见模块 docstring。

    @abstractmethod
    async def place_order(self, order: Order) -> SubmissionResult:
        """提交订单。``order`` 必须已携带 ``client_order_id``。"""

    @abstractmethod
    async def cancel_order(self, client_order_id: str) -> None:
        """撤销订单。同样适用超时不重试红线。"""

    # ----------------------------- 回报流 -----------------------------

    @abstractmethod
    def events(self) -> AsyncIterator[BrokerEvent]:
        """订阅回报事件流;返回值是 async generator,OrderManager ``async for`` 消费。"""
