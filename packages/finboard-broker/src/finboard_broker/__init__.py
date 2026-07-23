"""finboard-broker:BrokerAdapter / MarketDataAdapter 抽象与 Mock 实现。"""

from finboard_broker.base import BrokerAdapter, SubmissionResult
from finboard_broker.events import BrokerEvent, BrokerEventType
from finboard_broker.factory import create_broker
from finboard_broker.fault import FaultInjectionBroker
from finboard_broker.market_base import MarketDataAdapter, MarketDataEvent, MarketDataEventType
from finboard_broker.mock import MockBroker
from finboard_broker.mock_market import MockMarketData

__all__ = [
    "BrokerAdapter",
    "BrokerEvent",
    "BrokerEventType",
    "FaultInjectionBroker",
    "MarketDataAdapter",
    "MarketDataEvent",
    "MarketDataEventType",
    "MockBroker",
    "MockMarketData",
    "SubmissionResult",
    "create_broker",
]
