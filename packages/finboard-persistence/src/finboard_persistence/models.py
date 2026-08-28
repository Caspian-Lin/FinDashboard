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
    market: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)  # issue #58
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
    market: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)  # issue #58
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
    listing_board: Mapped[str] = mapped_column(
        String(16), default="unknown", server_default="unknown", index=True
    )
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delist_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # 连续未在发现列表中出现的次数(issue #35 退市二次确认)。
    # 达到阈值后 status -> delisted;再次出现时归零并回退。
    missing_runs: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sector: Mapped[str | None] = mapped_column(String(50), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(50), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_instruments_market_type", "market", "instrument_type"),
    )


class InstrumentNameModel(Base, IdMixin):
    """标的名称历史(issue #35 改名追踪)。

    每次 ``sync_with_diff`` 检测到名称变化时:
    * 旧名称记录的 ``valid_to`` 置为当天;
    * 插入新名称记录(``valid_from`` = 当天, ``valid_to`` = NULL)。
    ``instruments.name`` 始终保存最新名称用于查询。
    """

    __tablename__ = "instrument_names"

    instrument_code: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(100))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_instrument_names_code_valid", "instrument_code", "valid_from"),
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


class BacktestGridRunModel(Base, IdMixin):
    """批量参数网格回测定义(issue #175)。

    一次提交 N 组参数 → 网格行记录组合展开结果 + 逐组合的 ``backtest_run``
    任务 job_id(字符串引用,不建外键,与 ``background_jobs`` 同风格)。
    聚合对比表由 MCP ``grid_get`` 按 grid_id 实时计算,不在本表缓存结果。
    """

    __tablename__ = "backtest_grid_runs"

    grid_id: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    combos_checksum: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    symbols: Mapped[list] = mapped_column(JSON)  # type: ignore[type-arg]
    start: Mapped[str] = mapped_column(String(16))
    end: Mapped[str] = mapped_column(String(16))
    capital: Mapped[Decimal] = mapped_column(_numeric())
    adjust: Mapped[str] = mapped_column(String(8), default="qfq")
    base_params: Mapped[dict] = mapped_column(JSON)  # type: ignore[type-arg]
    selection: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    combos: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list, server_default=sql_text("'[]'::json")
    )
    combo_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_combos: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    requested_by: Mapped[str] = mapped_column(String(128))
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


class ResearchStrategySpecModel(Base, IdMixin):
    """无代码研究策略的追加式版本记录(issue #79)。"""

    __tablename__ = "research_strategy_specs"

    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(100))
    strategy_kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    change_type: Mapped[str] = mapped_column(String(24))
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    validation_errors: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list, server_default=sql_text("'[]'::json")
    )
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rollback_of_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "version",
            name="uq_research_strategy_spec_version",
        ),
        Index(
            "ix_research_strategy_spec_history",
            "strategy_id",
            "version",
        ),
    )


class ResearchRunModel(Base, IdMixin):
    """离线研究运行;与实盘账户/订单/成交/持仓表完全隔离(issue #80)。"""

    __tablename__ = "research_runs"

    run_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    replay_of_run_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    strategy_kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    schema_version: Mapped[str] = mapped_column(String(16))
    manifest_checksum: Mapped[str] = mapped_column(String(64), index=True)
    manifest: Mapped[dict[str, object]] = mapped_column(JSON)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    result_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    # issue #143:关联 background_jobs.job_id(字符串引用,不加外键,遵循
    # background_jobs 独立调度表约定)。queue 路由同事务双写时回填。
    job_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ResearchRunArtifactModel(Base, IdMixin):
    """ResearchRun 各阶段的追加式血缘 artifact。"""

    __tablename__ = "research_run_artifacts"

    run_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey("research_runs.run_id", ondelete="CASCADE"),
        index=True,
    )
    artifact_id: Mapped[str] = mapped_column(String(160))
    decision_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(48), index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_trace_ids: Mapped[list[str]] = mapped_column(
        JSON, default=list, server_default=sql_text("'[]'::json")
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id", "artifact_id", name="uq_research_run_artifact_id"
        ),
        UniqueConstraint(
            "run_id", "sequence", name="uq_research_run_artifact_sequence"
        ),
        UniqueConstraint(
            "run_id", "trace_id", name="uq_research_run_trace_id"
        ),
        Index(
            "ix_research_run_artifact_history",
            "run_id",
            "decision_id",
            "sequence",
        ),
    )


class SimulationAccountModel(Base, IdMixin):
    """与实盘账户表完全隔离的模拟资金账户(issue #83)。"""

    __tablename__ = "simulation_accounts"

    simulation_account_id: Mapped[str] = mapped_column(
        String(96), unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(String(16), default="simulation")
    status: Mapped[str] = mapped_column(String(24), index=True)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    initial_cash: Mapped[Decimal] = mapped_column(_research_numeric())
    cash: Mapped[Decimal] = mapped_column(_research_numeric())
    frozen_cash: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    margin_used: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    equity: Mapped[Decimal] = mapped_column(_research_numeric())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SimulationSessionModel(Base, IdMixin):
    """持久化模拟会话与可恢复市场时钟。"""

    __tablename__ = "simulation_sessions"

    simulation_session_id: Mapped[str] = mapped_column(
        String(96), unique=True, index=True
    )
    simulation_account_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey(
            "simulation_accounts.simulation_account_id", ondelete="RESTRICT"
        ),
        index=True,
    )
    mode: Mapped[str] = mapped_column(String(16), default="simulation")
    source_mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    strategy_version: Mapped[int] = mapped_column(Integer)
    strategy_checksum: Mapped[str] = mapped_column(String(64))
    validation_run_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey("research_runs.run_id", ondelete="RESTRICT"),
        index=True,
    )
    data_release_id: Mapped[str] = mapped_column(String(128), index=True)
    config: Mapped[dict[str, object]] = mapped_column(JSON)
    clock: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    promotion_status: Mapped[str] = mapped_column(
        String(24), default="not_evaluated", index=True
    )
    reset_of_session_id: Mapped[str | None] = mapped_column(
        String(96), nullable=True, index=True
    )
    recovery_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    last_sequence: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "ix_simulation_sessions_account_status",
            "simulation_account_id",
            "status",
        ),
    )


class SimulationDecisionModel(Base, IdMixin):
    """从机器验证产物进入模拟 runner 的结构化目标仓位决策。"""

    __tablename__ = "simulation_decisions"

    simulation_session_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey("simulation_sessions.simulation_session_id", ondelete="RESTRICT"),
        index=True,
    )
    decision_id: Mapped[str] = mapped_column(String(128))
    source_run_id: Mapped[str] = mapped_column(String(96), index=True)
    source_decision_id: Mapped[str] = mapped_column(String(128), index=True)
    source_signal_trace_ids: Mapped[list[str]] = mapped_column(JSON)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "simulation_session_id",
            "decision_id",
            name="uq_simulation_decision",
        ),
    )


class SimulationOrderModel(Base, IdMixin):
    """模拟订单; 不引用实盘 ``orders`` 表。"""

    __tablename__ = "simulation_orders"

    simulation_order_id: Mapped[str] = mapped_column(
        String(96), unique=True, index=True
    )
    intent_key: Mapped[str] = mapped_column(String(160), unique=True)
    simulation_account_id: Mapped[str] = mapped_column(String(96), index=True)
    simulation_session_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey("simulation_sessions.simulation_session_id", ondelete="RESTRICT"),
        index=True,
    )
    decision_id: Mapped[str] = mapped_column(String(128), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    signal_trace_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    market: Mapped[str] = mapped_column(String(16))
    instrument_type: Mapped[str] = mapped_column(String(24))
    asset_rule_key: Mapped[str] = mapped_column(String(32))
    position_side: Mapped[str] = mapped_column(String(8))
    position_effect: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(8))
    time_in_force: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(_research_numeric())
    price: Mapped[Decimal | None] = mapped_column(
        _research_numeric(), nullable=True
    )
    filled_quantity: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    average_fill_price: Mapped[Decimal | None] = mapped_column(
        _research_numeric(), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reject_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_snapshot: Mapped[dict[str, object]] = mapped_column(JSON)
    reserved_cash: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    reserved_quantity: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    submitted_market_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    eligible_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "ix_simulation_orders_session_status",
            "simulation_session_id",
            "status",
        ),
    )


class SimulationFillModel(Base, IdMixin):
    """模拟成交明细; 事件键和成交 ID 双重幂等。"""

    __tablename__ = "simulation_fills"

    simulation_fill_id: Mapped[str] = mapped_column(
        String(96), unique=True, index=True
    )
    fill_event_key: Mapped[str] = mapped_column(String(192), unique=True)
    simulation_order_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey("simulation_orders.simulation_order_id", ondelete="RESTRICT"),
        index=True,
    )
    simulation_account_id: Mapped[str] = mapped_column(String(96), index=True)
    simulation_session_id: Mapped[str] = mapped_column(String(96), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    market: Mapped[str] = mapped_column(String(16))
    position_side: Mapped[str] = mapped_column(String(8))
    position_effect: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(_research_numeric())
    price: Mapped[Decimal] = mapped_column(_research_numeric())
    commission: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    tax: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    slippage_cost: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    filled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SimulationPositionModel(Base, IdMixin):
    """模拟持仓; 数量只能由 ``SimulationFill`` 驱动。"""

    __tablename__ = "simulation_positions"

    simulation_account_id: Mapped[str] = mapped_column(String(96), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    market: Mapped[str] = mapped_column(String(16))
    instrument_type: Mapped[str] = mapped_column(String(24))
    asset_rule_key: Mapped[str] = mapped_column(String(32))
    position_side: Mapped[str] = mapped_column(String(8))
    total_quantity: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    available_quantity: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    frozen_quantity: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    average_price: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    market_value: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    margin_used: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    last_price: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    last_settlement_price: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    last_settlement_date: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )
    lots: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list, server_default=sql_text("'[]'::json")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "simulation_account_id",
            "symbol",
            "position_side",
            name="uq_simulation_position",
        ),
    )


class SimulationLedgerModel(Base, IdMixin):
    """追加式模拟现金、权益和费用账本。"""

    __tablename__ = "simulation_ledger"

    ledger_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    simulation_account_id: Mapped[str] = mapped_column(String(96), index=True)
    simulation_session_id: Mapped[str] = mapped_column(String(96), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    reference_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    cash_delta: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    margin_delta: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    commission: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    tax: Mapped[Decimal] = mapped_column(
        _research_numeric(), default=Decimal("0")
    )
    cash_after: Mapped[Decimal] = mapped_column(_research_numeric())
    frozen_cash_after: Mapped[Decimal] = mapped_column(_research_numeric())
    margin_used_after: Mapped[Decimal] = mapped_column(_research_numeric())
    equity_after: Mapped[Decimal] = mapped_column(_research_numeric())
    payload: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "simulation_session_id",
            "sequence",
            name="uq_simulation_ledger_sequence",
        ),
    )


class SimulationMarketEventModel(Base, IdMixin):
    """可重放且幂等的模拟行情输入。"""

    __tablename__ = "simulation_market_events"

    simulation_session_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey("simulation_sessions.simulation_session_id", ondelete="RESTRICT"),
        index=True,
    )
    source_event_id: Mapped[str] = mapped_column(String(160))
    checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    market: Mapped[str] = mapped_column(String(16))
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "simulation_session_id",
            "source_event_id",
            name="uq_simulation_market_event",
        ),
        Index(
            "ix_simulation_market_symbol_time",
            "simulation_session_id",
            "symbol",
            "timestamp",
        ),
    )


class SimulationAuditModel(Base, IdMixin):
    """模拟域独立审计, 不写入实盘 ``audit_logs``。"""

    __tablename__ = "simulation_audit"

    audit_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    event_key: Mapped[str] = mapped_column(String(192), unique=True)
    simulation_account_id: Mapped[str] = mapped_column(String(96), index=True)
    simulation_session_id: Mapped[str | None] = mapped_column(
        String(96), nullable=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(48), index=True)
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
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
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
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


# ---------------------------------------------------------------------------
# 多资产元数据(issue #58)
# ---------------------------------------------------------------------------


class EtfMetadataModel(Base, IdMixin):
    """ETF 子描述 —— 多维分类 / 跟踪指数 / 费率 / T+N 规则(issue #97 扩展)。

    ``category`` 保留为兼容旧版单维度分类;新增 ``execution_profile`` /
    ``underlying_market`` / ``strategy_type`` 正交维度 + 溯源 + 审核状态。
    ``manual_override=True`` 的记录不会被自动同步覆盖。
    """

    __tablename__ = "etf_metadata"

    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    fund_code: Mapped[str] = mapped_column(String(20), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)  # 兼容旧 EtfCategory
    execution_profile: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    underlying_market: Mapped[str] = mapped_column(String(16), default="domestic")
    strategy_type: Mapped[str] = mapped_column(String(16), default="index")
    underlying_index: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    underlying_asset_class: Mapped[str] = mapped_column(String(16), default="equity")
    management_fee_rate: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    custody_fee_rate: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    tracking_error: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    inception_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delisting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    iopv_available: Mapped[bool] = mapped_column(Boolean, default=False)
    allows_t_plus_0: Mapped[bool] = mapped_column(Boolean, default=False)
    dividend_policy: Mapped[str] = mapped_column(String(16), default="cash")
    source: Mapped[str] = mapped_column(String(32), default="manual")
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rule_version: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[Decimal] = mapped_column(_research_numeric(), default=Decimal("0"))
    review_status: Mapped[str] = mapped_column(String(32), default="needs_review", index=True)
    evidence: Mapped[list[object]] = mapped_column(
        JSON, default=list, server_default=sql_text("'[]'::json")
    )
    manual_override: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_payload: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    dataset_version: Mapped[str] = mapped_column(String(128), default="v1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EtfMetadataAuditModel(Base, IdMixin):
    """ETF 分类人工覆盖审计流水(issue #97)。

    每次人工修改记录一条变更:操作者 / 时间 / 理由 / 前后值,
    使自动同步不会静默回滚已确认的分类。
    """

    __tablename__ = "etf_metadata_audits"

    code: Mapped[str] = mapped_column(String(20), index=True)
    field_name: Mapped[str] = mapped_column(String(32))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str] = mapped_column(String(64), default="system")
    reason: Mapped[str] = mapped_column(Text, default="")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_etf_audit_code_changed", "code", "changed_at"),
    )


class BondMetadataModel(Base, IdMixin):
    """交易所债券子描述 —— 票息 / 到期日 / 久期 / 信用主体。"""

    __tablename__ = "bond_metadata"

    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    face_value: Mapped[Decimal] = mapped_column(_numeric(), default=Decimal("100"))
    coupon_rate: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    coupon_frequency: Mapped[str] = mapped_column(String(16), default="annual")
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    maturity_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    issuer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    credit_rating: Mapped[str | None] = mapped_column(String(16), nullable=True)
    credit_entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    duration_years: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    yield_to_maturity: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    dataset_version: Mapped[str] = mapped_column(String(128), default="v1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConvertibleMetadataModel(Base, IdMixin):
    """可转债子描述 —— 转股价 / 强赎 / 回售 / 下修条件。"""

    __tablename__ = "convertible_metadata"

    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    underlying_stock_code: Mapped[str] = mapped_column(String(20), index=True)
    conversion_price: Mapped[Decimal] = mapped_column(_numeric())
    conversion_ratio: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    conversion_premium: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    maturity_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    coupon_schedule: Mapped[list[object]] = mapped_column(
        JSON, default=list, server_default=sql_text("'[]'::json")
    )
    redemption_yield: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    forced_redeem_trigger: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    put_back_trigger: Mapped[Decimal | None] = mapped_column(_research_numeric(), nullable=True)
    downward_revision_trigger: Mapped[Decimal | None] = mapped_column(
        _research_numeric(), nullable=True
    )
    source: Mapped[str] = mapped_column(String(32), default="manual")
    dataset_version: Mapped[str] = mapped_column(String(128), default="v1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FuturesContractModel(Base, IdMixin):
    """期货合约链 —— 每条记录对应一份具体月份合约。"""

    __tablename__ = "futures_contracts"

    contract_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    series_id: Mapped[str] = mapped_column(String(16), index=True)  # IF / T / CU ...
    underlying_symbol: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str] = mapped_column(String(16), index=True)
    multiplier: Mapped[Decimal] = mapped_column(_numeric())
    margin_rate: Mapped[Decimal] = mapped_column(_research_numeric())
    price_limit_pct: Mapped[Decimal] = mapped_column(_research_numeric())
    price_tick: Mapped[Decimal] = mapped_column(_numeric())
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delivery_method: Mapped[str] = mapped_column(String(16), default="cash")
    settle_price: Mapped[Decimal | None] = mapped_column(_numeric(), nullable=True)
    open_interest: Mapped[Decimal | None] = mapped_column(_numeric(), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    dataset_version: Mapped[str] = mapped_column(String(128), default="v1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_futures_series_exchange", "series_id", "exchange"),
    )


class ContinuousFuturesRuleModel(Base, IdMixin):
    """连续期货拼接规则。"""

    __tablename__ = "continuous_futures_rules"

    series_id: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    roll_method: Mapped[str] = mapped_column(String(16))
    adjustment_method: Mapped[str] = mapped_column(String(16))
    roll_day_offset: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    rule_version: Mapped[str] = mapped_column(String(16), default="v1")
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InstrumentLifecycleEventModel(Base, IdMixin):
    """时点化的公司行为 / 合约事件(issue #58)。

    ``available_at`` 严格 >= ``effective_date`` 开盘,防止未来信息泄漏。
    """

    __tablename__ = "instrument_lifecycle_events"

    symbol: Mapped[str] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(32))
    dataset_version: Mapped[str] = mapped_column(String(128))
    details: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "event_type",
            "effective_date",
            "source",
            "dataset_version",
            name="uq_instrument_lifecycle_event",
        ),
        Index("ix_lifecycle_symbol_effective", "symbol", "effective_date"),
        Index("ix_lifecycle_effective_symbol", "effective_date", "symbol"),
    )


class DatasetManifestModel(Base, IdMixin):
    """数据集发布清单 —— 覆盖率 / 校验和 / 质量状态。"""

    __tablename__ = "dataset_manifests"

    dataset_name: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32))
    version: Mapped[str] = mapped_column(String(128))
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    symbol_count: Mapped[int] = mapped_column(Integer, default=0)
    coverage_pct: Mapped[Decimal] = mapped_column(_research_numeric(), default=Decimal("0"))
    gaps: Mapped[list[object]] = mapped_column(
        JSON, default=list, server_default=sql_text("'[]'::json")
    )
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    quality_status: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    quality_report: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    code_version: Mapped[str] = mapped_column(String(64), default="")

    __table_args__ = (
        UniqueConstraint(
            "dataset_name",
            "source",
            "version",
            name="uq_dataset_manifest",
        ),
    )


class ResearchDatasetReleaseModel(Base, IdMixin):
    """不可变研究数据集发布登记(issue #77)。

    ``manifest`` 保存完整逐标的资产规则、覆盖审计和文件校验和。关系型列保留
    常用筛选与唯一性闸门;Repository 禁止更新已发布行。
    """

    __tablename__ = "research_dataset_releases"

    release_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    dataset_name: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[str] = mapped_column(String(128))
    schema_version: Mapped[str] = mapped_column(String(32))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    period: Mapped[str] = mapped_column(String(8))
    adjustment: Mapped[str] = mapped_column(String(8))
    code_version: Mapped[str] = mapped_column(String(64))
    metadata_version: Mapped[str] = mapped_column(String(32))
    symbol_count: Mapped[int] = mapped_column(Integer)
    row_count: Mapped[int] = mapped_column(BigInteger)
    coverage_pct: Mapped[Decimal] = mapped_column(_research_numeric())
    quality_status: Mapped[str] = mapped_column(String(16), index=True)
    capabilities: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    quality_report: Mapped[dict[str, object]] = mapped_column(JSON)
    known_limitations: Mapped[list[str]] = mapped_column(JSON)
    storage_uri: Mapped[str] = mapped_column(Text)
    release_checksum: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    manifest: Mapped[dict[str, object]] = mapped_column(JSON)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "dataset_name",
            "source",
            "version",
            name="uq_research_dataset_release_version",
        ),
        Index(
            "ix_research_dataset_release_lookup",
            "dataset_name",
            "source",
            "quality_status",
            "published_at",
        ),
    )


# ---------------------------------------------------------------------------
# 因子实验室(issue #78)
# ---------------------------------------------------------------------------


class FactorFeatureSnapshotModel(Base, IdMixin):
    """不可变 FeatureSnapshot;逐条数据保存在完整 payload 中。"""

    __tablename__ = "factor_feature_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    dataset_release_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("research_dataset_releases.release_id", ondelete="RESTRICT"),
        index=True,
    )
    dataset_release_checksum: Mapped[str] = mapped_column(String(64), index=True)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    framework_version: Mapped[str] = mapped_column(String(32))
    feature_names: Mapped[list[str]] = mapped_column(JSON)
    symbol_count: Mapped[int] = mapped_column(Integer)
    observation_count: Mapped[int] = mapped_column(Integer)
    code_version: Mapped[str] = mapped_column(String(64))
    checksum: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_factor_feature_release_decision",
            "dataset_release_id",
            "decision_at",
        ),
    )


class FactorSignalModel(Base, IdMixin):
    """不可变 FactorSignal;不包含订单或交易执行字段。"""

    __tablename__ = "factor_signals"

    signal_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    factor_name: Mapped[str] = mapped_column(String(64), index=True)
    factor_version: Mapped[str] = mapped_column(String(32))
    feature_snapshot_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("factor_feature_snapshots.snapshot_id", ondelete="RESTRICT"),
        index=True,
    )
    feature_snapshot_checksum: Mapped[str] = mapped_column(String(64))
    candidate_universe_version: Mapped[str] = mapped_column(String(128))
    research_status: Mapped[str] = mapped_column(String(24), index=True)
    validation_experiment_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("research_experiments.experiment_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    symbol_count: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        Index(
            "ix_factor_signal_factor_status_created",
            "factor_name",
            "research_status",
            "created_at",
        ),
    )


class FactorExperimentModel(Base, IdMixin):
    """因子研究实验;失败、拒绝和中断记录不得删除或只保留赢家。"""

    __tablename__ = "factor_experiments"

    experiment_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    hypothesis: Mapped[str] = mapped_column(Text)
    factor_names: Mapped[list[str]] = mapped_column(JSON)
    dataset_release_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("research_dataset_releases.release_id", ondelete="RESTRICT"),
        index=True,
    )
    dataset_release_checksum: Mapped[str] = mapped_column(String(64))
    feature_snapshot_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("factor_feature_snapshots.snapshot_id", ondelete="RESTRICT"),
        index=True,
    )
    plan: Mapped[dict[str, object]] = mapped_column(JSON)
    comparison_group: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    validation_experiment_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("research_experiments.experiment_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    result: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "ix_factor_experiment_group_status_created",
            "comparison_group",
            "status",
            "created_at",
        ),
    )


class McpAuditEventModel(Base, IdMixin):
    """MCP 工具调用审计事件持久化(issue #157,``mcp_audit_persist`` 接线)。

    只追加,不修改,不删除。``FINBOARD_MCP_AUDIT_PERSIST=true`` 时由
    ``finboard_mcp.audit.AuditRecorder`` 写入;默认关闭(仅 structlog + 内存副本)。
    入参必须先经 ``summarize_arguments`` 脱敏 / 截断(不记录 API Key / 原始凭证 /
    未脱敏思考内容)。不写入实盘 ``audit_logs`` 表。
    """

    __tablename__ = "mcp_audit_events"

    operation_id: Mapped[str] = mapped_column(String(64), index=True)
    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24))
    latency_ms: Mapped[int] = mapped_column(Integer)
    error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    caller: Mapped[str | None] = mapped_column(String(128), nullable=True)
    arguments_summary: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchMemoryModel(Base, IdMixin):
    """研究长期记忆 / 研究笔记持久化(issue #110)。

    让 OpenCode 研究 Agent 跨会话积累结构化研究上下文,记忆通过
    ``source_refs`` 关联数据集 / 策略 / 实验 / ResearchRun / Simulation 产物。

    生命周期:``active`` → ``forgotten``(软删除)/ ``archived``(归档);
    ``correct`` 创建新 active 记忆并经 ``supersedes_id`` 链接被纠正的旧记忆。

    红线:不写入实盘 orders/fills/positions/audit_logs;``source_refs`` 只是
    引用,不修改被引用产物本身。
    """

    __tablename__ = "research_memories"

    memory_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    memory_type: Mapped[str] = mapped_column(String(24), index=True)
    content: Mapped[str] = mapped_column(Text)
    source_refs: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(128))
    conversation_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    supersedes_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_research_memory_status_type_created",
            "status",
            "memory_type",
            "created_at",
        ),
    )


class BackgroundJobModel(Base, IdMixin):
    """统一持久化后台任务队列(issue #117 / #142)。

    与实盘 orders/fills/positions 完全隔离 —— 是独立调度表,不建任何外键:
    ``result_ref`` 只以字符串形式引用产物(run_id / snapshot_id 等)。
    Worker 用 PostgreSQL ``FOR UPDATE SKIP LOCKED`` 领取 queued 任务;
    status 列存 ``BackgroundJobStatus.value`` 字符串(对齐现有约定,不用 sa.Enum)。
    """

    __tablename__ = "background_jobs"

    job_id: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    queue: Mapped[str] = mapped_column(
        String(32), index=True, default="default", server_default="default"
    )
    status: Mapped[str] = mapped_column(String(24), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    payload: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=sql_text("'{}'::json")
    )
    payload_checksum: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    progress_total: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    progress_done: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requested_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 归档时间(issue #221):NULL=未归档;非 NULL=已归档(从默认列表隐藏,不删除)。
    # 仅终态任务可归档;归档后 worker 维护路径(requeue_due)跳过该行。
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "ix_background_jobs_kind_status_created",
            "kind",
            "status",
            "created_at",
        ),
        Index(
            "ix_background_jobs_status_lease",
            "status",
            "lease_until",
        ),
        Index(
            "ix_background_jobs_queue_priority_created",
            "queue",
            "priority",
            "created_at",
        ),
    )
