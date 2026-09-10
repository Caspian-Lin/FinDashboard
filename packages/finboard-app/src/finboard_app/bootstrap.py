"""依赖注入组装根(composition root)。

设计原则:
* **不依赖 async** 的对象在 :func:`build_kernel_components` 中同步创建;
* 需要 ``AsyncSession`` 的对象(``TradingKernel`` / ``ReconciliationEngine``)
  在 CLI/启动器进入 async 上下文后,通过 :func:`KernelComponents.new_kernel` /
  :func:`KernelComponents.new_reconciler` 在每个 session 内构造。

这样避免了"在同步 bootstrap 里强行套 event loop"的反模式。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_broker import BrokerAdapter, create_broker
from finboard_persistence import (
    AccountRepository,
    AuditLogRepository,
    FillRepository,
    OrderRepository,
    PositionRepository,
    ReconciliationLogRepository,
    create_async_engine,
    session_factory,
)
from finboard_reconcile import ReconciliationEngine, RecoveryEngine
from finboard_risk import PreTradeChecker, RiskConfig
from finboard_shared.identifiers import AccountId


@dataclass
class KernelComponents:
    """进程级单例集合 —— 不持有 ``AsyncSession``。"""

    settings: Settings
    broker: BrokerAdapter
    engine: AsyncEngine
    session_maker: async_sessionmaker[AsyncSession]
    risk_checker: PreTradeChecker
    account_id: AccountId
    credentials: dict[str, str]

    def new_kernel(self, session: AsyncSession):  # type: ignore[no-untyped-def]
        """在已有的 ``AsyncSession`` 上构造一个 :class:`TradingKernel`。

        由调用方负责 session 生命周期与 ``commit``。
        生产 kernel 会注入 :class:`ReconciliationEngine`,使 ``start`` 执行
        启动核对(交易安全红线:核对未通过禁止下单)。
        """
        from finboard_app.risk_context import SessionRiskContext
        from finboard_core import TradingKernel

        risk_context = SessionRiskContext(
            position_repo=PositionRepository(session),
            account_repo=AccountRepository(session),
            order_repo=OrderRepository(session),
            fill_repo=FillRepository(session),
            account_id=self.account_id,
        )
        self.risk_checker.bind_context(risk_context)

        reconciler = self.new_reconciler(session)
        recoverer = self.new_recoverer(session)
        return TradingKernel(
            broker=self.broker,
            account_id=self.account_id,
            credentials=self.credentials,
            order_repo=OrderRepository(session),
            fill_repo=FillRepository(session),
            position_repo=PositionRepository(session),
            account_repo=AccountRepository(session),
            audit_repo=AuditLogRepository(session),
            risk_checker=self.risk_checker,
            reconciler=reconciler,
            recoverer=recoverer,
        )

    def new_reconciler(self, session: AsyncSession) -> ReconciliationEngine:
        return ReconciliationEngine(
            broker=self.broker,
            account_id=self.account_id,
            order_repo=OrderRepository(session),
            position_repo=PositionRepository(session),
            account_repo=AccountRepository(session),
            log_repo=ReconciliationLogRepository(session),
        )

    def new_recoverer(self, session: AsyncSession) -> RecoveryEngine:
        return RecoveryEngine(
            broker=self.broker,
            account_id=self.account_id,
            order_repo=OrderRepository(session),
            audit_repo=AuditLogRepository(session),
        )


def build_kernel_components(settings: Settings) -> KernelComponents:
    """同步组装:不创建任何 session,只准备依赖工厂。"""
    engine = create_async_engine(
        settings.db_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    smaker = session_factory(engine)
    creds = broker_credentials(settings)
    broker = create_broker(settings.broker, **creds)
    risk_checker = PreTradeChecker(config=_build_risk_config(settings))

    # issue #396:交易日历 DB 优先存储钩子 —— 发布覆盖率审计 / 决策日推导
    # 等 DB 优先读取经此落库回写;未安装(测试等)时日历模块走历史同步路径。
    from finboard_data.trading_calendar import install_calendar_store
    from finboard_persistence import PgTradingCalendarStore

    install_calendar_store(PgTradingCalendarStore(smaker))

    return KernelComponents(
        settings=settings,
        broker=broker,
        engine=engine,
        session_maker=smaker,
        risk_checker=risk_checker,
        account_id=AccountId(settings.account_id),
        credentials=broker_credentials(settings),
    )


def _build_risk_config(settings: Settings) -> RiskConfig:
    return RiskConfig(
        max_order_value=settings.risk_max_order_value,
        max_symbol_position_value=settings.risk_max_symbol_position_value,
        max_daily_buy_value=settings.risk_max_daily_buy_value,
        max_active_orders=settings.risk_max_active_orders,
        max_orders_per_minute=settings.risk_max_orders_per_minute,
        allow_short=settings.risk_allow_short,
        allow_market_order=settings.risk_allow_market_order,
    )


def broker_credentials(settings: Settings) -> dict[str, str]:
    """根据 broker 类型返回脱敏前的凭据字典(仅供 broker 内部使用)。"""
    if settings.broker.value == "qmt":
        return {"path": settings.qmt_path, "session_id": settings.qmt_session_id}
    if settings.broker.value == "ctp":
        return {
            "front_addr": settings.ctp_front_addr,
            "broker_id": settings.ctp_broker_id,
            "user_id": settings.ctp_user_id,
            "password": settings.ctp_password,
            "app_id": settings.ctp_app_id,
            "auth_code": settings.ctp_auth_code,
        }
    return {}
