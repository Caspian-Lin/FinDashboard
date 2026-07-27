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
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.orm import Mapped, mapped_column

from finboard_persistence.base import Base, IdMixin


def _numeric() -> Any:
    return Numeric(20, 4, decimal_return_scale=4)


def _research_numeric() -> Any:
    """研究数据保留换算后的 8 位小数与更大的市值范围。"""
    return Numeric(28, 8, decimal_return_scale=8)


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
    selection: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    dataset_versions: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    factor_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    selection_snapshots: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list, server_default=sql_text("'[]'::json")
    )
    # issue #56: 研究级成交语义归档
    matching_model: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    asset_rules: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True
    )
    fee_assumptions: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    benchmark_config: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
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
    selection: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ResearchSyncBatchModel(Base, IdMixin):
    """一次研究数据摄取、质量检查和发布批次。"""

    __tablename__ = "research_sync_batches"

    dataset: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    dataset_version: Mapped[str] = mapped_column(String(128))
    code_version: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    raw_payload: Mapped[object | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    quality_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    expected_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    received_rows: Mapped[int] = mapped_column(Integer, default=0)
    accepted_rows: Mapped[int] = mapped_column(Integer, default=0)
    rejected_rows: Mapped[int] = mapped_column(Integer, default=0)
    quality_report: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "dataset",
            "source",
            "dataset_version",
            name="uq_research_batch_dataset_source_version",
        ),
        Index(
            "ix_research_batch_lookup",
            "dataset",
            "source",
            "status",
            "published_at",
        ),
    )


class ResearchInstrumentProfileModel(Base, IdMixin):
    """带来源和版本的标的档案快照。"""

    __tablename__ = "research_instrument_profiles"

    batch_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("research_sync_batches.id", ondelete="CASCADE"),
        index=True,
    )
    source: Mapped[str] = mapped_column(String(32))
    dataset_version: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    exchange: Mapped[str] = mapped_column(String(16))
    market: Mapped[str] = mapped_column(String(32))
    list_status: Mapped[str] = mapped_column(String(8))
    list_date: Mapped[date] = mapped_column(Date)
    delist_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "source",
            "dataset_version",
            "symbol",
            name="uq_research_profile_source_version_symbol",
        ),
        Index("ix_research_profile_symbol_date", "symbol", "list_date"),
        Index("ix_research_profile_date_symbol", "list_date", "symbol"),
    )


class ResearchDailyMetricModel(Base, IdMixin):
    """版本化的单日证券估值、流动性、股本和市值指标。"""

    __tablename__ = "research_daily_metrics"

    batch_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("research_sync_batches.id", ondelete="CASCADE"),
        index=True,
    )
    source: Mapped[str] = mapped_column(String(32))
    dataset_version: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(20))
    trade_date: Mapped[date] = mapped_column(Date)
    close: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    turnover_rate: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    turnover_rate_free: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    volume_ratio: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    pe: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    pe_ttm: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    pb: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    ps: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    ps_ttm: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    dividend_yield: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    dividend_yield_ttm: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    total_shares: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    float_shares: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    free_shares: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    total_market_cap: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    circulating_market_cap: Mapped[Decimal | None] = mapped_column(
        _research_numeric(), nullable=True
    )
    limit_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "source",
            "dataset_version",
            "symbol",
            "trade_date",
            name="uq_research_daily_source_version_symbol_date",
        ),
        Index("ix_research_daily_symbol_date", "symbol", "trade_date"),
        Index("ix_research_daily_date_symbol", "trade_date", "symbol"),
    )


class ResearchFinancialIndicatorModel(Base, IdMixin):
    """财务指标公告版本;同一报告期的修订不会相互覆盖。"""

    __tablename__ = "research_financial_indicators"

    batch_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("research_sync_batches.id", ondelete="CASCADE"),
        index=True,
    )
    source: Mapped[str] = mapped_column(String(32))
    dataset_version: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(20))
    announcement_date: Mapped[date] = mapped_column(Date)
    report_period: Mapped[date] = mapped_column(Date)
    update_flag: Mapped[str] = mapped_column(String(16), default="")
    eps: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    diluted_eps: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    book_value_per_share: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    operating_cash_flow_per_share: Mapped[Decimal | None] = mapped_column(
        _research_numeric(), nullable=True
    )
    return_on_equity: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    weighted_return_on_equity: Mapped[Decimal | None] = mapped_column(
        _research_numeric(), nullable=True
    )
    gross_profit_margin: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    net_profit_margin: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    debt_to_assets: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    revenue_yoy: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    net_profit_yoy: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    operating_cash_flow_yoy: Mapped[Decimal | None] = mapped_column(
        _research_numeric(), nullable=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "source",
            "dataset_version",
            "symbol",
            "report_period",
            "announcement_date",
            "update_flag",
            name="uq_research_financial_revision",
        ),
        Index(
            "ix_research_financial_symbol_period",
            "symbol",
            "report_period",
        ),
        Index(
            "ix_research_financial_period_symbol",
            "report_period",
            "symbol",
        ),
    )


class ResearchIndustryClassificationModel(Base, IdMixin):
    """一个版本内的行业分类字典。"""

    __tablename__ = "research_industry_classifications"

    batch_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("research_sync_batches.id", ondelete="CASCADE"),
        index=True,
    )
    source: Mapped[str] = mapped_column(String(32))
    dataset_version: Mapped[str] = mapped_column(String(128))
    taxonomy: Mapped[str] = mapped_column(String(32))
    level: Mapped[int] = mapped_column(Integer)
    industry_code: Mapped[str] = mapped_column(String(32))
    industry_name: Mapped[str] = mapped_column(String(100))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "source",
            "dataset_version",
            "taxonomy",
            "level",
            "industry_code",
            name="uq_research_industry_classification",
        ),
        Index(
            "ix_research_industry_taxonomy_level",
            "taxonomy",
            "level",
            "industry_code",
        ),
    )


class ResearchIndustryMembershipModel(Base, IdMixin):
    """版本化的行业成员历史区间。"""

    __tablename__ = "research_industry_memberships"

    batch_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("research_sync_batches.id", ondelete="CASCADE"),
        index=True,
    )
    source: Mapped[str] = mapped_column(String(32))
    dataset_version: Mapped[str] = mapped_column(String(128))
    taxonomy: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(20))
    security_name: Mapped[str] = mapped_column(String(100))
    level1_code: Mapped[str] = mapped_column(String(32))
    level2_code: Mapped[str] = mapped_column(String(32))
    level3_code: Mapped[str] = mapped_column(String(32))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "source",
            "dataset_version",
            "taxonomy",
            "symbol",
            "level3_code",
            "valid_from",
            name="uq_research_industry_membership",
        ),
        Index(
            "ix_research_membership_symbol_valid",
            "symbol",
            "valid_from",
            "valid_to",
        ),
        Index(
            "ix_research_membership_valid_symbol",
            "valid_from",
            "valid_to",
            "symbol",
        ),
    )


class FactorSnapshotModel(Base, IdMixin):
    """冻结的 T 日决策、T+1 生效因子快照。"""

    __tablename__ = "factor_snapshots"

    decision_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    business_date: Mapped[date] = mapped_column(Date)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    dataset_versions: Mapped[dict[str, str]] = mapped_column(JSON)
    factor_version: Mapped[str] = mapped_column(String(32))
    static_universe: Mapped[list[str]] = mapped_column(JSON)
    selected_symbols: Mapped[list[str]] = mapped_column(JSON)
    config: Mapped[dict[str, object]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), index=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_factor_snapshot_decision_effective",
            "decision_at",
            "effective_date",
        ),
        Index(
            "ix_factor_snapshot_effective_decision",
            "effective_date",
            "decision_at",
        ),
    )


class FactorValueModel(Base, IdMixin):
    """快照内的规范化因子值与全市场/行业排名。"""

    __tablename__ = "factor_values"

    snapshot_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("factor_snapshots.id", ondelete="CASCADE"),
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(20))
    factor_name: Mapped[str] = mapped_column(String(64))
    factor_version: Mapped[str] = mapped_column(String(32))
    value: Mapped[Decimal] = mapped_column(_research_numeric())
    global_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    industry_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    industry_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "symbol",
            "factor_name",
            name="uq_factor_value_snapshot_symbol_factor",
        ),
        Index("ix_factor_value_symbol_factor", "symbol", "factor_name"),
        Index("ix_factor_value_factor_symbol", "factor_name", "symbol"),
    )


# ---------------------------------------------------------------------------
# 样本外验证(issue #57)
# ---------------------------------------------------------------------------


class ResearchExperimentModel(Base, IdMixin):
    """研究实验登记 —— 假设、计划、门、揭盲状态。

    生命周期:HYPOTHESIS → IN_SAMPLE → VALIDATED_OOS / REJECTED → SUPERSEDED。
    假设冻结后不可修改;新建实验必须 ``supersedes_id`` 关联旧版本。
    """

    __tablename__ = "research_experiments"

    experiment_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    hypothesis: Mapped[str] = mapped_column(Text)
    version_stamp: Mapped[dict[str, object]] = mapped_column(JSON)
    version_checksum: Mapped[str] = mapped_column(String(64), index=True)
    plan: Mapped[dict[str, object]] = mapped_column(JSON)
    thresholds: Mapped[dict[str, object]] = mapped_column(JSON)
    robustness: Mapped[dict[str, object]] = mapped_column(JSON)
    strategy_params_space: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trials_used: Mapped[int] = mapped_column(Integer, default=0)
    final_test_unsealed: Mapped[bool] = mapped_column(Boolean, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("ix_research_experiment_status_created", "status", "created_at"),
    )


class ResearchTrialModel(Base, IdMixin):
    """研究试验记录 —— 包括赢家 + 输家 + 失败。

    ``status`` ∈ {candidate, running, selected, rejected, failed, skipped}。
    失败 trial 必须入库,多重试验修正需要真实试验总数。
    """

    __tablename__ = "research_trials"

    trial_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    experiment_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("research_experiments.experiment_id", ondelete="CASCADE"),
        index=True,
    )
    trial_index: Mapped[int] = mapped_column(Integer)
    parameters: Mapped[dict[str, object]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), index=True)
    in_sample_metrics: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True
    )
    oos_metrics: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    walk_forward_windows: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list, server_default=sql_text("'[]'::json")
    )
    robustness_probes: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list, server_default=sql_text("'[]'::json")
    )
    statistical_report: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "trial_index",
            name="uq_research_trial_experiment_index",
        ),
        Index("ix_research_trial_experiment_status", "experiment_id", "status"),
    )
