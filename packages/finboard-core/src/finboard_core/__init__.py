"""finboard-core:交易内核。"""

from finboard_core.account_manager import AccountManager
from finboard_core.bus import EventBus
from finboard_core.events import (
    AccountSnapshotUpdated,
    KillSwitchActivated,
    OrderCancelled,
    OrderCreated,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
    PositionUpdated,
)
from finboard_core.kernel import TradingKernel
from finboard_core.order_manager import OrderManager
from finboard_core.position_manager import PositionManager
from finboard_core.protocols import RiskChecker
from finboard_core.state_machine import InvalidStateTransitionError, OrderStateMachine
from finboard_core.strategy import (
    OrderEvent,
    Strategy,
    StrategyContext,
    StrategyRunner,
    TimerEvent,
    UniverseSelectionEvent,
)

__all__ = [
    "AccountManager",
    "AccountSnapshotUpdated",
    "EventBus",
    "InvalidStateTransitionError",
    "KillSwitchActivated",
    "OrderCancelled",
    "OrderCreated",
    "OrderEvent",
    "OrderFilled",
    "OrderManager",
    "OrderRejected",
    "OrderStateMachine",
    "OrderSubmitted",
    "PositionManager",
    "PositionUpdated",
    "RiskChecker",
    "Strategy",
    "StrategyContext",
    "StrategyRunner",
    "TimerEvent",
    "TradingKernel",
    "UniverseSelectionEvent",
]
