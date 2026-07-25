"""统一枚举。

约定:全部继承 ``StrEnum``,便于序列化为字符串与 JSON 字段对齐;
跨网络/持久化场景下永远以 ``.value`` 为准。
"""

from __future__ import annotations

from enum import StrEnum


class Market(StrEnum):
    A_SHARE = "a_share"
    HK = "hk"
    US = "us"
    FUTURE = "future"


class InstrumentType(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    INDEX = "index"
    FUTURES = "futures"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(StrEnum):
    """持仓方向。

    A 股一律为 LONG;期货区分多空,允许平仓时指定对应方向。
    """

    LONG = "long"
    SHORT = "short"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class TimeInForce(StrEnum):
    GFD = "GFD"  # Good-for-day,默认
    IOC = "IOC"  # Immediate-or-cancel
    FOK = "FOK"  # Fill-or-kill


class OrderStatus(StrEnum):
    """订单状态机(见 phase1_doc.md §4.2)。

    状态迁移图::

        CREATED → RISK_CHECKED → SUBMITTING → SUBMITTED → ACKNOWLEDGED
                                                        ├─ PARTIALLY_FILLED → ...
                                                        ├─ FILLED            (终态)
                                                        ├─ CANCEL_PENDING → CANCELLED (终态)
                                                        ├─ REJECTED           (终态)
                                                        └─ UNKNOWN            (超时兜底,需人工/查询驱动)
    """

    CREATED = "created"
    RISK_CHECKED = "risk_checked"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }

    @property
    def is_active(self) -> bool:
        return not self.is_terminal


class RejectReason(StrEnum):
    """订单被拒/风控失败的具体原因。

    风控层抛 ``RiskCheckError`` 时必须带 ``reason``;持久化到 ``orders.reject_reason`` 列。
    """

    RISK_CHECK_FAILED = "risk_check_failed"
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_POSITION = "insufficient_position"
    INVALID_PRICE = "invalid_price"
    INVALID_QUANTITY = "invalid_quantity"
    INVALID_SYMBOL = "invalid_symbol"
    DUPLICATE_ORDER = "duplicate_order"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    BROKER_REJECTED = "broker_rejected"
    SUBMIT_TIMEOUT = "submit_timeout"  # 触发 UNKNOWN 流程
    UNKNOWN = "unknown"


class BrokerKind(StrEnum):
    MOCK = "mock"
    QMT = "qmt"
    CTP = "ctp"
    BACKTEST = "backtest"

    @property
    def market(self) -> Market:
        return Market.A_SHARE if self in (BrokerKind.QMT, BrokerKind.BACKTEST) else Market.FUTURE


class KillSwitchLevel(StrEnum):
    """Kill Switch 分级(见 phase1_doc.md §6.3)。

    严格程度自上而下递增;高级别隐含低级别所有约束。
    必须由交易内核强制执行,不能只靠网页按钮。
    """

    OFF = "off"
    NO_NEW_ORDERS = "no_new_orders"  # 暂停新单(允许撤单、允许查询)
    REDUCE_ONLY = "reduce_only"  # 仅允许减仓
    CANCEL_ALL = "cancel_all"  # 撤销全部活动订单
    HALT = "halt"  # 全局停止


class BarPeriod(StrEnum):
    """K 线周期。

    值与 xtdata ``period`` 参数对齐,便于直接透传。
    """

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    D1 = "1d"


class TradingPhase(StrEnum):
    """交易日内系统的运行阶段。"""

    PRE_MARKET = "pre_market"
    AUCTION = "auction"  # 集合竞价
    CONTINUOUS = "continuous"  # 连续竞价
    POST_MARKET = "post_market"
    CLOSED = "closed"
