"""ORM 模型。

风格说明:
* ``Mapped[]`` + ``mapped_column()`` (SQLAlchemy 2.0 风格);
* 金额/数量一律 ``Numeric(20, 4)`` —— Decimal 直接 round-trip,不走 float;
* 时间戳全部 ``timezone=True``,UTC 写入,UTC 读出;
* ``__table_args__`` 集中声明 UNIQUE / 索引,避免散落在字段上。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from finboard_persistence.base import Base, IdMixin


def _numeric() -> Any:
    return Numeric(20, 4, decimal_return_scale=4)


class AccountModel(Base, IdMixin):
    """账户资金快照(本地缓存;真值在券商侧)。"""

    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    broker_kind: Mapped[str] = mapped_column(String(16))
    total_asset: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    cash: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    frozen_cash: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    margin_used: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OrderModel(Base, IdMixin):
    """本地订单全生命周期。"""

    __tablename__ = "orders"

    # 防重复下单的 DB 闸门:同一 client_order_id 不允许出现两次
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    broker_kind: Mapped[str] = mapped_column(String(16))
    strategy_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    market: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(8))
    time_in_force: Mapped[str] = mapped_column(String(8))
    position_side: Mapped[str] = mapped_column(String(8))
    price: Mapped[Decimal | None] = mapped_column(_numeric(), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(_numeric())
    filled_quantity: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    average_fill_price: Mapped[Decimal | None] = mapped_column(_numeric(), nullable=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reject_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    risk_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_orders_account_status", "account_id", "status"),
        Index("ix_orders_symbol_created", "symbol", "created_at"),
    )


class FillModel(Base, IdMixin):
    """成交明细;一笔订单可对应多笔成交。"""

    __tablename__ = "fills"

    # 防回报重放导致重复入账
    fill_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    broker_fill_id: Mapped[str | None] = mapped_column(String(64), index=True)
    client_order_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("orders.client_order_id", ondelete="RESTRICT"),
        index=True,
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    position_side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(_numeric())
    price: Mapped[Decimal] = mapped_column(_numeric())
    commission: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    tax: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    filled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PositionModel(Base, IdMixin):
    """持仓快照,按 ``source`` 区分本地推导与券商查询。

    红线:本地持仓不是真值;Reconciliation 在两者不一致时以 broker 行覆盖。
    """

    __tablename__ = "positions"

    account_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    position_side: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(8))  # local / broker
    total_quantity: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    available_quantity: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    frozen_quantity: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    average_price: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    market_value: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    unrealized_pnl: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "symbol",
            "position_side",
            "source",
            name="uq_positions_account_symbol_side_source",
        ),
    )


class ReconciliationLogModel(Base, IdMixin):
    """每次 Reconciliation 的差异记录,供后续审计与回放。"""

    __tablename__ = "reconciliation_logs"

    account_id: Mapped[str] = mapped_column(String(64), index=True)
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    kind: Mapped[str] = mapped_column(String(24))  # orders / positions / account
    key: Mapped[str] = mapped_column(String(128))  # 通常是 client_order_id 或 symbol
    local_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_taken: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLogModel(Base, IdMixin):
    """关键状态变化与人工操作的审计行。"""

    __tablename__ = "audit_logs"

    actor: Mapped[str] = mapped_column(String(64))  # system / strategy_id / user_id
    action: Mapped[str] = mapped_column(String(32), index=True)  # place_order / kill_switch / ...
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class InstrumentModel(Base, IdMixin):
    """标的元数据 —— 全市场标的字典(A 股 / ETF / 港股 / 美股 ...)。

    由 :mod:`finboard_data.discovery` 自动发现并 upsert,替代手工 symbols.yaml。
    """

    __tablename__ = "instruments"

    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    market: Mapped[str] = mapped_column(String(16), index=True)  # a_share / hk / us
    instrument_type: Mapped[str] = mapped_column(String(16), index=True)  # stock / etf
    exchange: Mapped[str | None] = mapped_column(String(16), nullable=True)  # SSE / SZSE
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delist_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    sector: Mapped[str | None] = mapped_column(String(50), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(50), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_instruments_market_type", "market", "instrument_type"),
    )


class WatchlistModel(Base, IdMixin):
    """用户标的组(watchlist)——保存常用回测标的集合。"""

    __tablename__ = "watchlists"

    name: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WatchlistItemModel(Base, IdMixin):
    """标的组内的成员。"""

    __tablename__ = "watchlist_items"

    watchlist_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("watchlists.id", ondelete="CASCADE"),
        index=True,
    )
    symbol_code: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("watchlist_id", "symbol_code", name="uq_watchlist_item"),
    )


class BacktestRunModel(Base, IdMixin):
    """回测运行记录——保存参数 + 完整结果,支持历史切换查看。"""

    __tablename__ = "backtest_runs"

    strategy: Mapped[str] = mapped_column(String(64), index=True)
    symbols: Mapped[list] = mapped_column(JSON)  # type: ignore[type-arg]
    start: Mapped[str] = mapped_column(String(16))
    end: Mapped[str] = mapped_column(String(16))
    capital: Mapped[Decimal] = mapped_column(_numeric())
    adjust: Mapped[str] = mapped_column(String(8), default="qfq")
    params: Mapped[dict] = mapped_column(JSON)  # type: ignore[type-arg]
    metrics: Mapped[dict] = mapped_column(JSON)  # type: ignore[type-arg]
    equity_curve: Mapped[list] = mapped_column(JSON)  # type: ignore[type-arg]
    fills: Mapped[list] = mapped_column(JSON)  # type: ignore[type-arg]
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class StrategyPresetModel(Base, IdMixin):
    """经过 schema 校验的内置策略参数预设。"""

    __tablename__ = "strategy_presets"

    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    params: Mapped[dict] = mapped_column(JSON)  # type: ignore[type-arg]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
