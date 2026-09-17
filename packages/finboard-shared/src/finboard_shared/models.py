"""领域数据模型(``@dataclass``)。

* 全部为可变/不可变 ``dataclass``,不引入 pydantic —— 数据模型保持零依赖。
* 配置加载与外部接口由 ``finboard_app`` / ``finboard_api`` 在边界做 pydantic 转换。
* 货币/数量字段一律 ``Decimal``(严禁 ``float``),避免浮点累计误差污染资金类计算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self

from finboard_shared.identifiers import AccountId, ClientOrderId, StrategyId
from finboard_shared.types import (
    BarPeriod,
    BrokerKind,
    Market,
    OrderStatus,
    OrderType,
    PositionSide,
    RejectReason,
    Side,
    TimeInForce,
)


def _utcnow() -> datetime:
    """统一时区 aware 的 UTC now,避免 dataclass default_factory 的隐式 local time。"""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Symbol:
    """统一标的概念。

    ``code`` 是券商识别用的合约代码,例如 ``510300.SH``、``IF2406.CFFEX``;
    ``market`` 指示其所属市场,影响交易规则(T+1 / 开平方向等)。
    """

    code: str
    market: Market

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("Symbol.code 不能为空")

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """策略或人工发出的下单意图。

    尚未通过风控、尚未生成 ``client_order_id``。

    设计说明:本 dataclass 仅做"结构不变量"校验(symbol 非空、限价单必须带 price);
    业务规则(quantity > 0、价格上限、金额上限等)由
    :class:`finboard_risk.PreTradeChecker` 负责,从而保持本类为纯数据载体。
    """

    account_id: AccountId
    symbol: Symbol
    side: Side
    order_type: OrderType
    quantity: Decimal
    strategy_id: StrategyId | None = None
    price: Decimal | None = None  # 市价单为 None
    time_in_force: TimeInForce = TimeInForce.GFD
    position_side: PositionSide = PositionSide.LONG  # 期货区分;A股忽略

    def __post_init__(self) -> None:
        if not self.symbol.code:
            raise ValueError("Symbol.code 不能为空")
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("限价单必须提供 price")


@dataclass(slots=True)
class Order:
    """本地订单的最终状态对象,贯穿整个生命周期。

    风格说明:这是一个**可变** dataclass —— 状态迁移会就地修改字段
    (``status`` / ``filled_quantity`` / ``updated_at`` 等),以便 OrderManager
    集中维护单实例。持久化由 ``finboard_persistence`` 负责快照写入。
    """

    client_order_id: ClientOrderId
    account_id: AccountId
    broker_kind: BrokerKind
    symbol: Symbol
    side: Side
    order_type: OrderType
    quantity: Decimal
    strategy_id: StrategyId | None = None
    price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GFD
    position_side: PositionSide = PositionSide.LONG
    broker_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    status: OrderStatus = OrderStatus.CREATED
    reject_reason: RejectReason | None = None
    reject_message: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    risk_checked_at: datetime | None = None
    submitted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def is_active(self) -> bool:
        return self.status.is_active

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    def touch(self) -> None:
        """显式更新 ``updated_at``;状态迁移后调用。"""
        self.updated_at = _utcnow()


@dataclass(frozen=True, slots=True)
class Fill:
    """单笔成交回报。一笔 ``Order`` 可对应多笔 ``Fill``。"""

    fill_id: str  # 券商成交编号
    client_order_id: ClientOrderId
    symbol: Symbol
    side: Side
    quantity: Decimal
    price: Decimal
    position_side: PositionSide = PositionSide.LONG
    commission: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")  # 印花税等
    broker_order_id: str | None = None
    filled_at: datetime = field(default_factory=_utcnow)


@dataclass(slots=True)
class Position:
    """持仓快照。

    **本地持仓不是真实来源**,只用于实时响应 / 风控 / 异常检测
    (见 AGENTS.md §交易安全红线)。最终真值由券商查询结果覆盖。
    """

    account_id: AccountId
    symbol: Symbol
    position_side: PositionSide = PositionSide.LONG
    total_quantity: Decimal = Decimal("0")
    available_quantity: Decimal = Decimal("0")
    frozen_quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    market_value: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    updated_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def empty(cls, account_id: AccountId, symbol: Symbol) -> Self:
        return cls(account_id=account_id, symbol=symbol)

    def touch(self) -> None:
        """显式更新 ``updated_at``。"""
        self.updated_at = _utcnow()


@dataclass(slots=True)
class Account:
    """账户资金快照。"""

    account_id: AccountId
    broker_kind: BrokerKind
    total_asset: Decimal = Decimal("0")  # 总资产
    cash: Decimal = Decimal("0")  # 可用资金
    frozen_cash: Decimal = Decimal("0")  # 冻结资金
    margin_used: Decimal = Decimal("0")  # 已用保证金(期货)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class Tick:
    """Level-1 行情快照(逐笔 / 盘口)。

    A 股 Tick 通常 3 秒推送一次;字段对应 xtdata ``subscribe_quote`` tick 回调。
    五档买卖盘为可选字段,部分行情源可能不提供。
    """

    symbol: Symbol
    last_price: Decimal
    open: Decimal = Decimal("0")
    high: Decimal = Decimal("0")
    low: Decimal = Decimal("0")
    pre_close: Decimal = Decimal("0")
    volume: Decimal = Decimal("0")  # 持仓量(合约)或最新成交量(股票累计)
    amount: Decimal = Decimal("0")  # 成交额
    last_volume: Decimal = Decimal("0")  # 本笔成交量
    bid_price: Decimal = Decimal("0")
    bid_volume: Decimal = Decimal("0")
    ask_price: Decimal = Decimal("0")
    ask_volume: Decimal = Decimal("0")
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class Bar:
    """K 线(OHLCV)。

    ``timestamp`` 为 bar 的起始时间。
    """

    symbol: Symbol
    period: BarPeriod
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")
    amount: Decimal = Decimal("0")
    source: str = ""
