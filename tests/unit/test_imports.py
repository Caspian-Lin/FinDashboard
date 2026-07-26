"""跨包导入 smoke 测试。

确保 uv workspace 安装后,所有包都可以 import 而不缺依赖。
"""

from __future__ import annotations


def test_shared_imports() -> None:
    from finboard_shared import (
        BrokerKind,
        KillSwitchLevel,
        OrderStatus,
        generate_client_order_id,
    )

    assert BrokerKind.MOCK.value == "mock"
    assert OrderStatus.CREATED.is_active
    assert OrderStatus.FILLED.is_terminal
    assert KillSwitchLevel.HALT.value == "halt"
    cid = generate_client_order_id()
    assert cid.startswith("F-")


def test_broker_imports() -> None:
    from finboard_broker import BrokerAdapter, MockBroker, create_broker
    from finboard_broker.events import BrokerEventType

    assert BrokerEventType.ORDER_FILLED.value == "order_filled"
    broker = create_broker("mock")
    assert isinstance(broker, MockBroker)
    assert isinstance(broker, BrokerAdapter)


def test_broker_stub_imports() -> None:
    # CTP 仍是 stub;QMT 需要 path/session_id 参数且依赖 xtquant,
    # 此处只验证模块可导入 + BrokerKind 枚举值,不实例化。
    from finboard_broker_ctp import CtpBroker
    from finboard_shared.types import BrokerKind

    assert CtpBroker().kind.value == "ctp"
    assert BrokerKind.QMT.value == "qmt"


def test_persistence_imports() -> None:
    from finboard_persistence import (
        AccountModel,
        Base,
        FillModel,
        OrderModel,
        PositionModel,
        ResearchDataSyncService,
        ResearchSyncBatchModel,
        create_async_engine,
    )

    engine = create_async_engine("postgresql+psycopg://x:x@127.0.0.1/x")
    assert engine is not None
    # 所有模型必须注册到同一 metadata
    table_names = set(Base.metadata.tables)
    assert {
        "accounts",
        "orders",
        "fills",
        "positions",
        "research_sync_batches",
        "research_daily_metrics",
        "research_financial_indicators",
        "research_industry_classifications",
        "research_industry_memberships",
        "research_instrument_profiles",
    } <= table_names
    assert OrderModel.__tablename__ == "orders"
    assert FillModel.__tablename__ == "fills"
    assert PositionModel.__tablename__ == "positions"
    assert AccountModel.__tablename__ == "accounts"
    assert ResearchSyncBatchModel.__tablename__ == "research_sync_batches"
    assert ResearchDataSyncService is not None


def test_core_imports() -> None:
    from finboard_core import (
        EventBus,
        OrderManager,
        OrderStateMachine,
        PositionManager,
        TradingKernel,
    )

    assert EventBus is not None
    assert OrderManager is not None
    assert PositionManager is not None
    assert TradingKernel is not None
    assert OrderStateMachine is not None


def test_risk_imports() -> None:
    from finboard_risk import PreTradeChecker, RiskConfig

    cfg = RiskConfig()
    assert cfg.max_order_value > 0
    assert PreTradeChecker(config=cfg) is not None


def test_reconcile_imports() -> None:
    from finboard_reconcile import ReconciliationEngine, ReconciliationReport

    assert ReconciliationReport().ok is True
    assert ReconciliationEngine is not None


def test_app_imports() -> None:
    from finboard_app import Settings, load_settings
    from finboard_app.cli import app

    assert app is not None
    s = load_settings()
    assert s.broker.value == "mock"
    assert isinstance(s, Settings)
