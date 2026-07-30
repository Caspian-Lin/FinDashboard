"""持久化模拟交易 REST 契约(issue #83)。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finboard_shared.models import Bar, Symbol
from finboard_shared.types import (
    BarPeriod,
    InstrumentType,
    Market,
    OrderType,
    PositionSide,
    TimeInForce,
)
from finboard_simulation import (
    SimulationBarEvent,
    SimulationConfig,
    SimulationDecision,
    SimulationMatchingConfig,
    SimulationRiskLimits,
    SimulationSourceMode,
    SimulationTarget,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SimulationAccountCreateIn(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    initial_cash: Decimal = Field(gt=0)
    actor: str = Field(min_length=1, max_length=128)
    currency: str = Field(default="CNY", pattern="^CNY$")


class SimulationAccountOut(OrmModel):
    simulation_account_id: str
    name: str
    mode: str
    status: str
    currency: str
    initial_cash: Decimal
    cash: Decimal
    frozen_cash: Decimal
    margin_used: Decimal
    equity: Decimal
    created_at: datetime
    archived_at: datetime | None
    updated_at: datetime


class SimulationRiskIn(StrictModel):
    max_order_value: Decimal = Field(default=Decimal("100000"), gt=0)
    max_symbol_position_value: Decimal = Field(default=Decimal("150000"), gt=0)
    max_gross_exposure: Decimal = Field(default=Decimal("1"), gt=0)
    max_margin_usage: Decimal = Field(default=Decimal("0.8"), gt=0, le=1)
    max_active_orders: int = Field(default=20, ge=1, le=10000)
    allow_short: bool = False

    def to_domain(self) -> SimulationRiskLimits:
        return SimulationRiskLimits(**self.model_dump())


class SimulationMatchingIn(StrictModel):
    fill_timing: str = Field(
        default="next_bar_open",
        pattern="^next_bar_(open|close|vwap_proxy)$",
    )
    next_bar_only: bool = True
    max_participation: Decimal = Field(default=Decimal("0.1"), gt=0, le=1)
    allow_partial_fill: bool = True
    honour_gaps: bool = True
    enforce_suspension: bool = True
    enforce_price_limit: bool = True
    enforce_lot_rounding: bool = True
    slippage_bps: Decimal = Field(default=Decimal("5"), ge=0, le=10000)
    market_price_buffer_bps: Decimal = Field(default=Decimal("200"), ge=0, le=10000)

    @model_validator(mode="after")
    def validate_next_bar(self) -> SimulationMatchingIn:
        if not self.next_bar_only:
            raise ValueError("产品模拟盘强制 next_bar_only=true")
        if self.market_price_buffer_bps < self.slippage_bps:
            raise ValueError("市价预占缓冲不得小于滑点")
        return self

    def to_domain(self) -> SimulationMatchingConfig:
        from finboard_backtest.config import FillTiming

        return SimulationMatchingConfig(
            fill_timing=FillTiming(self.fill_timing),
            next_bar_only=self.next_bar_only,
            max_participation=self.max_participation,
            allow_partial_fill=self.allow_partial_fill,
            honour_gaps=self.honour_gaps,
            enforce_suspension=self.enforce_suspension,
            enforce_price_limit=self.enforce_price_limit,
            enforce_lot_rounding=self.enforce_lot_rounding,
            slippage_bps=self.slippage_bps,
            market_price_buffer_bps=self.market_price_buffer_bps,
        )


class SimulationSessionCreateIn(StrictModel):
    simulation_account_id: str = Field(pattern=r"^SIM-A-")
    strategy_id: str = Field(min_length=1, max_length=64)
    strategy_version: int = Field(ge=1)
    validation_run_id: str = Field(pattern=r"^RR-")
    data_release_id: str = Field(min_length=1, max_length=128)
    source_mode: SimulationSourceMode
    matching: SimulationMatchingIn = Field(default_factory=SimulationMatchingIn)
    risk: SimulationRiskIn = Field(default_factory=SimulationRiskIn)
    clock_speed: Decimal = Field(default=Decimal("1"), gt=0, le=1000)
    actor: str = Field(min_length=1, max_length=128)

    def to_config(self) -> SimulationConfig:
        return SimulationConfig(
            matching=self.matching.to_domain(),
            risk=self.risk.to_domain(),
            clock_speed=self.clock_speed,
        )


class SimulationSessionOut(OrmModel):
    simulation_session_id: str
    simulation_account_id: str
    mode: str
    source_mode: str
    status: str
    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    validation_run_id: str
    data_release_id: str
    config: dict[str, object]
    clock: dict[str, object]
    promotion_status: str
    reset_of_session_id: str | None
    recovery_count: int
    created_at: datetime
    started_at: datetime | None
    paused_at: datetime | None
    stopped_at: datetime | None
    archived_at: datetime | None
    updated_at: datetime


class SimulationActorIn(StrictModel):
    actor: str = Field(min_length=1, max_length=128)


class SimulationResetIn(SimulationActorIn):
    initial_cash: Decimal | None = Field(default=None, gt=0)


class SimulationResetOut(BaseModel):
    account: SimulationAccountOut
    session: SimulationSessionOut


class SimulationTargetIn(StrictModel):
    symbol: str = Field(min_length=1, max_length=32)
    market: Market
    instrument_type: InstrumentType
    asset_rule_key: str = Field(min_length=1, max_length=32)
    position_side: PositionSide = PositionSide.LONG
    target_quantity: Decimal = Field(ge=0)
    signal_trace_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = Field(default=None, gt=0)
    time_in_force: TimeInForce = TimeInForce.GFD

    def to_domain(self) -> SimulationTarget:
        return SimulationTarget(**self.model_dump())


class SimulationDecisionIn(StrictModel):
    decision_id: str = Field(min_length=1, max_length=128)
    source_run_id: str = Field(pattern=r"^RR-")
    source_decision_id: str = Field(min_length=1, max_length=128)
    targets: tuple[SimulationTargetIn, ...] = Field(min_length=1)
    actor: str = Field(min_length=1, max_length=128)

    def to_domain(self) -> SimulationDecision:
        return SimulationDecision(
            decision_id=self.decision_id,
            source_run_id=self.source_run_id,
            source_decision_id=self.source_decision_id,
            targets=tuple(item.to_domain() for item in self.targets),
            actor=self.actor,
        )


class SimulationDecisionOut(OrmModel):
    decision_id: str
    source_run_id: str
    source_decision_id: str
    source_signal_trace_ids: list[str]
    checksum: str
    status: str
    created_at: datetime


class SimulationOrderOut(OrmModel):
    simulation_order_id: str
    simulation_account_id: str
    simulation_session_id: str
    decision_id: str
    strategy_id: str
    signal_trace_id: str
    symbol: str
    market: str
    instrument_type: str
    asset_rule_key: str
    position_side: str
    position_effect: str
    side: str
    order_type: str
    time_in_force: str
    quantity: Decimal
    price: Decimal | None
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    status: str
    reject_reason: str | None
    reject_message: str | None
    reserved_cash: Decimal
    reserved_quantity: Decimal
    submitted_market_at: datetime
    eligible_after: datetime
    created_at: datetime
    updated_at: datetime


class SimulationDecisionResultOut(BaseModel):
    decision: SimulationDecisionOut
    orders: list[SimulationOrderOut]
    duplicate: bool


class SimulationBarIn(StrictModel):
    source_event_id: str = Field(min_length=1, max_length=160)
    symbol: str = Field(min_length=1, max_length=32)
    market: Market
    period: BarPeriod = BarPeriod.D1
    timestamp: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(default=Decimal("0"), ge=0)
    amount: Decimal = Field(default=Decimal("0"), ge=0)
    actor: str = Field(default="market-data", min_length=1, max_length=128)
    contract_id: str | None = Field(default=None, max_length=64)

    def to_domain(self) -> SimulationBarEvent:
        return SimulationBarEvent(
            source_event_id=self.source_event_id,
            bar=Bar(
                symbol=Symbol(code=self.symbol, market=self.market),
                period=self.period,
                timestamp=self.timestamp,
                open=self.open,
                high=self.high,
                low=self.low,
                close=self.close,
                volume=self.volume,
                amount=self.amount,
            ),
            actor=self.actor,
            contract_id=self.contract_id,
        )


class SimulationProcessOut(BaseModel):
    source_event_id: str
    duplicate: bool
    fill_ids: tuple[str, ...]
    rejected_order_ids: tuple[str, ...]
    equity: Decimal
    clock_at: datetime


class SimulationFillOut(OrmModel):
    simulation_fill_id: str
    simulation_order_id: str
    simulation_account_id: str
    simulation_session_id: str
    symbol: str
    market: str
    position_side: str
    position_effect: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    tax: Decimal
    slippage_cost: Decimal
    filled_at: datetime


class SimulationPositionOut(OrmModel):
    simulation_account_id: str
    symbol: str
    market: str
    instrument_type: str
    asset_rule_key: str
    position_side: str
    total_quantity: Decimal
    available_quantity: Decimal
    frozen_quantity: Decimal
    average_price: Decimal
    market_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    margin_used: Decimal
    last_price: Decimal
    last_settlement_price: Decimal
    last_settlement_date: date | None
    updated_at: datetime


class SimulationLedgerOut(OrmModel):
    ledger_id: str
    simulation_account_id: str
    simulation_session_id: str
    sequence: int
    event_type: str
    reference_id: str | None
    cash_delta: Decimal
    margin_delta: Decimal
    realized_pnl: Decimal
    commission: Decimal
    tax: Decimal
    cash_after: Decimal
    frozen_cash_after: Decimal
    margin_used_after: Decimal
    equity_after: Decimal
    payload: dict[str, object]
    occurred_at: datetime


class SimulationAuditOut(OrmModel):
    audit_id: str
    simulation_account_id: str
    simulation_session_id: str | None
    sequence: int
    actor: str
    action: str
    target: str | None
    payload: dict[str, object]
    checksum: str
    created_at: datetime


class SimulationEvaluateIn(SimulationActorIn):
    minimum_trading_days: int = Field(default=2, ge=2, le=10000)


__all__ = [
    "SimulationAccountCreateIn",
    "SimulationAccountOut",
    "SimulationActorIn",
    "SimulationAuditOut",
    "SimulationBarIn",
    "SimulationDecisionIn",
    "SimulationDecisionOut",
    "SimulationDecisionResultOut",
    "SimulationEvaluateIn",
    "SimulationFillOut",
    "SimulationLedgerOut",
    "SimulationOrderOut",
    "SimulationPositionOut",
    "SimulationProcessOut",
    "SimulationResetIn",
    "SimulationResetOut",
    "SimulationSessionCreateIn",
    "SimulationSessionOut",
]
