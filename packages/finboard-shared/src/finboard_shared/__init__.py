"""finboard-shared:跨模块共享的领域原语。

只暴露稳定契约,不包含任何 IO / 业务逻辑。
"""

from finboard_shared.exceptions import (
    BrokerError,
    BrokerTimeoutError,
    FinboardError,
    KillSwitchActiveError,
    OrderNotFoundError,
    ReconcileMismatchError,
    RiskCheckError,
)
from finboard_shared.identifiers import (
    AccountId,
    ClientOrderId,
    StrategyId,
    generate_client_order_id,
)
from finboard_shared.models import (
    Account,
    Fill,
    Order,
    OrderRequest,
    Position,
    Symbol,
)
from finboard_shared.types import (
    BrokerKind,
    KillSwitchLevel,
    Market,
    OrderStatus,
    OrderType,
    PositionSide,
    RejectReason,
    Side,
    TimeInForce,
    TradingPhase,
)

__all__ = [
    # 数据模型
    "Account",
    # 强类型 ID
    "AccountId",
    # 异常
    "BrokerError",
    # 枚举
    "BrokerKind",
    "BrokerTimeoutError",
    "ClientOrderId",
    "Fill",
    "FinboardError",
    "KillSwitchActiveError",
    "KillSwitchLevel",
    "Market",
    "Order",
    "OrderNotFoundError",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "Position",
    "PositionSide",
    "ReconcileMismatchError",
    "RejectReason",
    "RiskCheckError",
    "Side",
    "StrategyId",
    "Symbol",
    "TimeInForce",
    "TradingPhase",
    "generate_client_order_id",
]
