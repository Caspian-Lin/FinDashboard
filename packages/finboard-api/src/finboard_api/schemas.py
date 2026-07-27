"""Pydantic 请求 / 响应 schema。

领域 dataclass(``finboard_shared.models``)保持零依赖,API 边界在此做 pydantic 转换。
``Decimal`` 字段在 JSON 序列化时转为 ``str``,避免浮点精度丢失。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from finboard_app.selection_schema import FactorSelectionParams


class BaseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- Account
class AccountOut(BaseSchema):
    account_id: str
    broker_kind: str
    total_asset: Decimal
    cash: Decimal
    frozen_cash: Decimal
    margin_used: Decimal
    updated_at: datetime


# --------------------------------------------------------------------------- Position
class PositionOut(BaseSchema):
    symbol: str
    market: str
    position_side: str
    total_quantity: Decimal
    available_quantity: Decimal
    frozen_quantity: Decimal
    average_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    updated_at: datetime


# --------------------------------------------------------------------------- Order
class OrderCreate(BaseSchema):
    """手工下单请求体。

    红线:``client_order_id`` 由 OrderManager 内部生成,不接受外部传入。
    """

    symbol: str
    market: str = "a_share"
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None = None
    time_in_force: str = "GFD"


class OrderOut(BaseSchema):
    client_order_id: str
    account_id: str
    broker_kind: str
    symbol: str
    market: str
    side: str
    order_type: str
    quantity: Decimal
    strategy_id: str | None = None
    price: Decimal | None = None
    time_in_force: str
    position_side: str
    broker_order_id: str | None = None
    filled_quantity: Decimal
    average_fill_price: Decimal | None = None
    status: str
    reject_reason: str | None = None
    reject_message: str | None = None
    created_at: datetime
    risk_checked_at: datetime | None = None
    submitted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    updated_at: datetime
    is_active: bool
    is_terminal: bool
    remaining_quantity: Decimal


# --------------------------------------------------------------------------- Fill
class FillOut(BaseSchema):
    fill_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    tax: Decimal
    broker_order_id: str | None = None
    filled_at: datetime


# --------------------------------------------------------------------------- Kill Switch
class KillSwitchOut(BaseSchema):
    level: str
    allows_new_orders: bool
    allows_reduce_only: bool


class KillSwitchActivate(BaseSchema):
    level: str
    reason: str = ""


# --------------------------------------------------------------------------- Reconcile
class ReconcileResult(BaseSchema):
    ok: bool
    summary: str
    total_checks: int
    mismatches: int


# --------------------------------------------------------------------------- Health
class HealthOut(BaseSchema):
    status: str
    kernel_ready: bool
    kill_switch_level: str
    broker_connected: bool = False
    broker_kind: str = ""
    active_orders: int = 0
    started_at: datetime | None = None


# --------------------------------------------------------------------------- Risk Config
class RiskConfigOut(BaseSchema):
    max_order_value: Decimal
    max_symbol_position_value: Decimal
    max_daily_buy_value: Decimal
    max_active_orders: int
    max_orders_per_minute: int
    allow_short: bool
    allow_market_order: bool


# --------------------------------------------------------------------------- Audit
class AuditLogOut(BaseSchema):
    id: int
    actor: str
    action: str
    target: str | None = None
    payload: str | None = None
    created_at: datetime


# --------------------------------------------------------------------------- Pagination
class PageResponse[T](BaseSchema):
    items: list[T]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- WebSocket
class WsError(BaseSchema):
    detail: str


# --------------------------------------------------------------------------- Data
class DataFetchRequest(BaseSchema):
    symbol: str
    start: str
    end: str
    adjust: str = "qfq"


class DataStatusOut(BaseSchema):
    symbol: str
    period: str
    adjust: str
    bar_count: int
    first_date: str | None = None
    last_date: str | None = None
    last_close: Decimal | None = None


class DataStatusListOut(BaseSchema):
    items: list[DataStatusOut]
    total: int
    limit: int
    offset: int


class FetchResultOut(BaseSchema):
    symbol: str
    bar_count: int
    first_date: str | None = None
    last_date: str | None = None


class BatchFetchResultOut(BaseSchema):
    total: int
    success: int
    failed: int
    details: list[FetchResultOut]


class SymbolEntrySchema(BaseSchema):
    code: str
    name: str = ""


class SymbolPoolOut(BaseSchema):
    symbols: list[SymbolEntrySchema]
    fetch_period: str = "D1"
    fetch_lookback_days: int = 5
    fetch_adjust: str = "qfq"


class SymbolPoolUpdate(BaseSchema):
    symbols: list[SymbolEntrySchema]
    fetch_period: str = "D1"
    fetch_lookback_days: int = 5
    fetch_adjust: str = "qfq"


# --------------------------------------------------------------------------- Instrument
class InstrumentOut(BaseSchema):
    code: str
    name: str = ""
    market: str
    instrument_type: str
    exchange: str | None = None
    status: str = "active"


class InstrumentListOut(BaseSchema):
    items: list[InstrumentOut]
    total: int
    limit: int
    offset: int


class SyncResultOut(BaseSchema):
    total: int
    new: int
    updated: int


class BulkDownloadRequest(BaseSchema):
    market: str = "a_share"
    instrument_type: str | None = None
    start: str = "2015-01-01"


class BulkDownloadStatusOut(BaseSchema):
    status: str = "idle"  # idle / running / done / error
    done: int = 0
    total: int = 0
    success: int = 0
    failed: int = 0
    current_symbol: str | None = None
    phase: str | None = None
    error: str | None = None


class SchedulerConfigOut(BaseSchema):
    sync_enabled: bool = True
    sync_time: str = "15:35"
    download_enabled: bool = True
    download_time: str = "15:45"
    download_lookback_days: int = 5
    download_markets: list[str] = ["a_share"]
    download_types: list[str] = ["stock", "etf"]
    data_provider: str = "yfinance"


class SchedulerConfigUpdate(BaseSchema):
    sync_enabled: bool | None = None
    sync_time: str | None = None
    download_enabled: bool | None = None
    download_time: str | None = None
    download_lookback_days: int | None = None
    download_markets: list[str] | None = None
    download_types: list[str] | None = None


# --------------------------------------------------------------------------- Backtest
class BacktestRunRequest(BaseSchema):
    strategy: str
    symbols: list[str]
    start: str
    end: str
    capital: Decimal = Decimal("100000")
    adjust: str = "qfq"
    params: dict[str, Any] = {}
    selection: FactorSelectionParams = Field(default_factory=FactorSelectionParams)
    # 费用参数(可覆盖默认值)
    commission_rate: Decimal = Decimal("0.0003")  # 万 3
    commission_min: Decimal = Decimal("1")  # 最低 ¥1/笔
    stamp_tax_rate: Decimal = Decimal("0.0005")  # 万 5(卖出)
    slippage_bps: Decimal = Decimal("0")  # 滑点 bps


class StrategyParamInfo(BaseSchema):
    name: str
    label: str
    type: str
    default: Any = None
    required: bool = False
    description: str = ""
    enum: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = None
    exclusive_maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    nullable: bool = False
    ui_hidden: bool = False


class StrategyInfoOut(BaseSchema):
    kind: str
    name: str
    description: str
    supports_backtest: bool
    params: list[StrategyParamInfo]


class EquityPointOut(BaseSchema):
    date: str
    equity: float
    benchmark: float | None = None


class BacktestMetricsOut(BaseSchema):
    total_return: float = 0.0
    annualized_return: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    turnover: float = 0.0
    commission_paid: Decimal = Decimal("0")
    stamp_tax_paid: Decimal = Decimal("0")
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    initial_capital: Decimal = Decimal("0")
    final_equity: Decimal = Decimal("0")


class BacktestFillOut(BaseSchema):
    date: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal


class FactorSnapshotOut(BaseSchema):
    id: int | None = None
    decision_at: datetime
    business_date: str
    effective_date: str
    selected_symbols: list[str]
    status: str
    skip_reason: str | None = None
    dataset_versions: dict[str, str]
    factor_version: str
    checksum: str


class BacktestResultOut(BaseSchema):
    metrics: BacktestMetricsOut
    equity_curve: list[EquityPointOut]
    fills: list[BacktestFillOut]
    summary: str
    run_id: int | None = None
    selection_snapshots: list[FactorSnapshotOut] = Field(default_factory=list)
    dataset_versions: dict[str, list[str]] = Field(default_factory=dict)
    factor_version: str | None = None
    matching_model: dict[str, Any] = Field(default_factory=dict)
    asset_rules: dict[str, Any] | None = None
    fee_assumptions: dict[str, Any] = Field(default_factory=dict)
    benchmark_config: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- Backtest History
class BacktestHistoryItemOut(BaseSchema):
    id: int
    strategy: str
    symbols: list[str]
    start: str
    end: str
    capital: Decimal
    adjust: str
    metrics: dict[str, Any]
    factor_version: str | None = None
    created_at: datetime


class BacktestHistoryDetailOut(BaseSchema):
    id: int
    strategy: str
    symbols: list[str]
    start: str
    end: str
    capital: Decimal
    adjust: str
    params: dict[str, Any]
    selection: FactorSelectionParams = Field(default_factory=FactorSelectionParams)
    metrics: dict[str, Any]
    equity_curve: list[EquityPointOut]
    fills: list[BacktestFillOut]
    summary: str
    selection_snapshots: list[FactorSnapshotOut] = Field(default_factory=list)
    dataset_versions: dict[str, list[str]] = Field(default_factory=dict)
    factor_version: str | None = None
    matching_model: dict[str, Any] = Field(default_factory=dict)
    asset_rules: dict[str, Any] | None = None
    fee_assumptions: dict[str, Any] = Field(default_factory=dict)
    benchmark_config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# --------------------------------------------------------------------------- Strategy Preset
class StrategyPresetCreate(BaseSchema):
    name: str = Field(min_length=1, max_length=100)
    strategy: str
    params: dict[str, Any] = {}
    selection: FactorSelectionParams = Field(default_factory=FactorSelectionParams)


class StrategyPresetUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    strategy: str | None = None
    params: dict[str, Any] | None = None
    selection: FactorSelectionParams | None = None


class StrategyPresetOut(BaseSchema):
    id: int
    name: str
    strategy: str
    params: dict[str, Any]
    selection: FactorSelectionParams
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- Watchlist
class WatchlistCreate(BaseSchema):
    name: str
    description: str | None = None


class WatchlistUpdate(BaseSchema):
    name: str | None = None
    description: str | None = None


class WatchlistOut(BaseSchema):
    id: int
    name: str
    description: str | None = None
    item_count: int = 0
    created_at: datetime


class WatchlistDetailOut(WatchlistOut):
    symbols: list[str]


class WatchlistAddSymbols(BaseSchema):
    symbols: list[str]


# --------------------------------------------------------------------------- Research Experiment (issue #57)
class VersionStampSchema(BaseSchema):
    matching_model_version: str
    asset_rules_version: str
    factor_version: str | None = None
    dataset_versions: dict[str, str] = Field(default_factory=dict)
    selection_config: dict[str, Any] = Field(default_factory=dict)
    strategy_kind: str


class ValidationPlanSchema(BaseSchema):
    mode: str  # rolling / expanding
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    test_start: str
    test_end: str
    train_window_days: int = 504
    test_window_days: int = 63
    step_days: int = 63
    trial_budget: int = 50
    random_seed: int = 0
    benchmark_symbol: str | None = None


class AcceptanceThresholdsSchema(BaseSchema):
    min_in_sample_sharpe: float = 1.0
    min_oos_sharpe: float = 0.5
    max_oos_drawdown: float = 0.25
    min_oos_calmar: float = 0.5
    min_oos_information_ratio: float = 0.0
    max_param_sensitivity_sharpe_drop: float = 0.5
    min_pbo_pass: bool = True
    max_pbo: float = 0.5
    min_deflated_sharpe: float = 0.0
    min_probabilistic_sharpe: float = 0.95


class RobustnessPlanSchema(BaseSchema):
    neighbourhood_steps: int = 5
    neighbourhood_relative_step: float = 0.1
    cost_multipliers: list[float] = Field(default_factory=lambda: [1.0, 2.0, 3.0])
    slippage_stress_bps: list[float] = Field(default_factory=lambda: [0.0, 5.0, 10.0, 20.0])
    execution_delay_bars: list[int] = Field(default_factory=lambda: [1, 2])
    stress_phases: list[str] = Field(
        default_factory=lambda: [
            "2018-Q4",
            "2020-Q1",
            "2022-Q1",
            "2024-Q1",
        ]
    )


class ExperimentCreate(BaseSchema):
    """创建研究实验 —— 假设 / 计划 / 门必须一次性冻结。

    创建后 ``hypothesis`` 不可修改;若需要重新假设,创建新 experiment
    并设 ``supersedes_id`` 指向旧版本。
    """

    hypothesis: str = Field(min_length=10, max_length=2000)
    version_stamp: VersionStampSchema
    plan: ValidationPlanSchema
    thresholds: AcceptanceThresholdsSchema = Field(default_factory=AcceptanceThresholdsSchema)
    robustness: RobustnessPlanSchema = Field(default_factory=RobustnessPlanSchema)
    strategy_params_space: dict[str, Any] = Field(default_factory=dict)
    supersedes_id: str | None = None
    notes: str = ""


class ExperimentOut(BaseSchema):
    experiment_id: str
    hypothesis: str
    version_stamp: dict[str, Any]
    version_checksum: str
    plan: dict[str, Any]
    thresholds: dict[str, Any]
    robustness: dict[str, Any]
    strategy_params_space: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_at: datetime
    frozen_at: datetime
    finalized_at: datetime | None = None
    trials_used: int = 0
    final_test_unsealed: bool = False
    rejection_reason: str | None = None
    supersedes_id: str | None = None
    notes: str = ""


class TrialOut(BaseSchema):
    trial_id: str
    experiment_id: str
    trial_index: int
    parameters: dict[str, Any]
    status: str
    in_sample_metrics: dict[str, Any] | None = None
    oos_metrics: dict[str, Any] | None = None
    walk_forward_windows: list[dict[str, Any]] = Field(default_factory=list)
    robustness_probes: list[dict[str, Any]] = Field(default_factory=list)
    statistical_report: dict[str, Any] | None = None
    failure_reason: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class ExperimentDetailOut(ExperimentOut):
    """实验详情 + 全部 trial(包括失败)。"""

    trials: list[TrialOut] = Field(default_factory=list)


class TrialCreate(BaseSchema):
    """手动登记一次 trial(不通过 runner 自动跑)。"""

    parameters: dict[str, Any]
    status: str = "candidate"
    failure_reason: str | None = None


class ExperimentRejectIn(BaseSchema):
    reason: str = Field(min_length=1)
