"""Pydantic 请求 / 响应 schema。

领域 dataclass(``finboard_shared.models``)保持零依赖,API 边界在此做 pydantic 转换。
``Decimal`` 字段在 JSON 序列化时转为 ``str``,避免浮点精度丢失。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    source: str | None = None  # akshare / yfinance / tushare; None = 用服务端默认


class DataStatusOut(BaseSchema):
    symbol: str
    listing_board: str = "unknown"
    period: str
    adjust: str
    bar_count: int
    first_date: str | None = None
    last_date: str | None = None
    last_close: Decimal | None = None
    source: str | None = None


class DataStatusListOut(BaseSchema):
    items: list[DataStatusOut]
    total: int
    limit: int
    offset: int


class DataStatusSelectionOut(BaseSchema):
    """匹配缓存筛选条件的完整选择快照。"""

    items: list[DataStatusOut]
    total: int
    first_date: str | None = None
    last_date: str | None = None


class TushareQuotaOut(BaseSchema):
    """Tushare 本地请求预算,不冒充账户侧实时权限。"""

    date: str
    requests_per_minute: int
    daily_limit: int
    used: int
    remaining: int


class FetchResultOut(BaseSchema):
    symbol: str
    bar_count: int
    first_date: str | None = None
    last_date: str | None = None
    source: str | None = None
    fallback_used: bool = False
    fallback_source: str | None = None
    lifecycle_events: int = 0
    lifecycle_sync_failed: bool = False
    lifecycle_sync_error: str | None = None


class BarAnomalyOut(BaseSchema):
    date: str
    source: str
    reasons: list[str]


class QualityReportOut(BaseSchema):
    symbol: str
    total_bars: int
    anomaly_count: int
    duplicate_count: int = 0
    sources: list[str] = []
    anomalies: list[BarAnomalyOut] = []
    passed: bool = True
    primary_source: str = ""
    fallback_used: bool = False
    fallback_source: str | None = None
    corrected_dates: list[str] = []
    error: str | None = None


class QualityRepairRequest(BaseSchema):
    symbols: list[str]
    source: Literal["akshare", "yfinance", "tushare"]
    adjust: str = "qfq"


class QualityRepairResultOut(BaseSchema):
    total: int
    repaired: int
    failed: int
    corrected_bars: int
    reports: list[QualityReportOut]


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
    listing_board: str = "unknown"
    list_date: date | None = None
    delist_date: date | None = None
    status: str = "active"
    sector: str | None = None
    industry: str | None = None


class InstrumentListOut(BaseSchema):
    items: list[InstrumentOut]
    total: int
    limit: int
    offset: int


class InstrumentSummaryOut(BaseSchema):
    """标的字典的全量分布,用于元数据页解释数量口径。"""

    total: int
    active_total: int
    active_etf_total: int
    by_status: dict[str, int] = Field(default_factory=dict)
    by_market: dict[str, int] = Field(default_factory=dict)
    by_instrument_type: dict[str, int] = Field(default_factory=dict)
    by_listing_board: dict[str, int] = Field(default_factory=dict)


class BulkDownloadRequest(BaseSchema):
    market: str = "a_share"
    instrument_type: str | None = None
    exchange: str | None = None
    listing_boards: list[str] = Field(default_factory=list)
    start: str = "2015-01-01"
    source: str | None = None
    # symbols 子集重跑(#347):与 market/instrument_type/exchange/listing_boards
    # 过滤叠加(交集为空执行器按 no_instruments 拒);失败清单可直接回填。
    symbols: list[str] | None = None


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
class BenchmarkParams(BaseSchema):
    """基准选择(issue #184):显式指定基准标的或退回等权候选池。"""

    symbol: str | None = Field(default=None, max_length=32)
    equal_weight_universe: bool = True


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
    benchmark: BenchmarkParams | None = None


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
    sharpe_ratio: float = Field(
        default=0.0,
        description="夏普比率(引擎主口径:rf=risk_free_annual/年,按 rf/252 日化,"
        "总体标准差 ddof=0,√252 年化)",
    )
    # issue #262:rf=0 对照口径 + 主口径 rf 取值随指标序列化,防误读
    # (低收益策略的主口径 Sharpe 可能被 rf=3% 拖近 0)。
    sharpe_rf0: float = Field(
        default=0.0,
        description="夏普比率(rf=0 对照口径:样本标准差 ddof=1,√252 年化;"
        "与 research_run 报告 sharpe_ratio 同口径,跨报告同屏比较用本字段)",
    )
    risk_free_annual: float = Field(
        default=0.03,
        description="sharpe_ratio 实际使用的年化无风险利率",
    )
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    turnover: float = 0.0
    commission_paid: Decimal = Decimal("0")
    stamp_tax_paid: Decimal = Decimal("0")
    benchmark_return: float | None = None
    excess_return: float | None = None
    # issue #254:基准曲线实际来源(explicit_symbol:<code> /
    # equal_weight_selection_pool / equal_weight_static_pool / first_symbol)
    benchmark_source: str | None = None
    # issue #255:选股启用时的逐期诊断(选股数据集未发布/整期 SKIPPED 可见)
    selection_diagnostics: dict[str, Any] | None = None
    # issue #285:job 级分段耗时(total_elapsed_seconds / data_load_elapsed_seconds
    # / parquet_reads),纯可观测性,随 metrics JSON 自然携带,无迁移。
    timing: dict[str, Any] | None = None
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
    warnings: list[str] = Field(default_factory=list)


class FactorSnapshotSummaryOut(BaseSchema):
    """选股快照 summary 投影(issue #258):不含全量 selected_symbols 列表。"""

    decision_at: datetime
    business_date: str
    effective_date: str
    status: str
    skip_reason: str | None = None
    checksum: str
    selected_symbol_count: int


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
    # issue #258:selection_snapshots=summary 时逐条为 FactorSnapshotSummaryOut
    # (计数投影),full 才是 FactorSnapshotOut 全量;落库始终全量。
    selection_snapshots: list[FactorSnapshotOut | FactorSnapshotSummaryOut] = Field(
        default_factory=list
    )
    dataset_versions: dict[str, list[str]] = Field(default_factory=dict)
    factor_version: str | None = None
    matching_model: dict[str, Any] = Field(default_factory=dict)
    asset_rules: dict[str, Any] | None = None
    fee_assumptions: dict[str, Any] = Field(default_factory=dict)
    benchmark_config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    # 裁剪元信息(issue #206/#258 REST parity):全量计数,不受请求裁剪影响。
    equity_point_count: int | None = None
    fills_total: int | None = None
    fills_offset: int | None = None
    selection_snapshot_count: int | None = None


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
    # issue #219:user_code 的 OOS 结论必须绑定精确 artifact 与 commit。
    code_artifact_id: str | None = None
    code_artifact_name: str | None = None
    code_kind: str | None = None
    code_commit: str | None = None


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
    # issue #310:派生结论语义(supported|not_supported|inconclusive),仅在
    # list/get 读取时由 trial OOS 状态 + 揭盲指标推导,不落库;写路径回执为
    # None。validated_oos 只代表 OOS 流程完成,不代表假设获支持。
    oos_outcome: str | None = None


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


# --------------------------------------------------------------------------- Factor Lab (issue #78)
class FactorDefinitionOut(BaseSchema):
    name: str
    version: str
    role: str
    preference: str
    frequency: str
    unit: str
    source_fields: list[str]
    calculation_window: int | None = None
    default_transform: str
    default_neutralization: list[str]
    available_at_rule: str
    missing_policy: str
    economic_hypothesis: str
    expected_failure: str
    implementation: str
    signal_eligible: bool
    checksum: str
    # 展示注记:该因子输入字段来自哪些发布数据集(bars/daily_metrics/...),
    # 由 RESEARCH_RELEASE_FEATURE_NAMES 唯一事实来源派生,不属于目录 checksum。
    source_datasets: list[str] = Field(default_factory=list)


class FeatureSnapshotCreate(BaseSchema):
    """从一个已发布数据版本显式生成价格特征快照。"""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    dataset_release_id: str = Field(min_length=1, max_length=128)
    # issue #187:联合发布因子快照 —— 除 bars 主发布外,可附加
    # daily_metrics / financial_indicators 发布作为因子输入(可空表示纯价格)。
    additional_release_ids: list[str] = Field(default_factory=list, max_length=16)
    decision_at: datetime

    @field_validator("decision_at")
    @classmethod
    def validate_decision_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision_at 必须带时区,例如 2026-07-31T23:59:59+08:00")
        return value

    @field_validator("additional_release_ids")
    @classmethod
    def normalize_additional_release_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("附加数据发布不能重复")
        return normalized


class FeatureObservationOut(BaseSchema):
    symbol: str
    feature_name: str
    value: float
    observed_at: datetime
    available_at: datetime
    source: str
    source_version: str
    market: str | None = None
    asset_class: str | None = None
    industry: str | None = None


class FeatureSnapshotOut(BaseSchema):
    """特征快照全量视图(单查 / 创建端点;observations 逐条值恒返回)。"""

    snapshot_id: str
    # issue #217/#309:沙箱快照无发布锚点(dataset_release_id=None),
    # 改可空并携带 source_run_id;此前单查沙箱快照会在校验时 500。
    dataset_release_id: str | None = None
    source_run_id: str | None = None
    dataset_release_checksum: str
    decision_at: datetime
    published_at: datetime
    framework_version: str
    calculation_windows: dict[str, int]
    transformations: dict[str, str]
    neutralization: dict[str, list[str]]
    code_version: str
    observations: list[FeatureObservationOut]
    checksum: str
    issues: list[str] = Field(default_factory=list)


class FeatureSnapshotHeaderOut(BaseSchema):
    """特征快照头部视图(issue #309):list 端点默认返回。

    只含头部字段与覆盖统计(feature_names/symbol_count/observation_count),
    **不含 observations 逐条值**(大快照单条可达 MB 级,list 全量会被
    MCP 客户端截断);完整值走单查端点或 ``include_observations=true``。
    """

    snapshot_id: str
    dataset_release_id: str | None = None
    source_run_id: str | None = None
    dataset_release_checksum: str
    decision_at: datetime
    published_at: datetime
    framework_version: str
    feature_names: list[str] = Field(default_factory=list)
    symbol_count: int
    observation_count: int
    code_version: str
    checksum: str
    created_at: datetime | None = None


class FactorSignalItemOut(BaseSchema):
    symbol: str
    direction: str
    score: float
    confidence: float
    valid_from: datetime
    valid_until: datetime
    reason: str


class FactorSignalOut(BaseSchema):
    signal_id: str
    factor_name: str
    factor_version: str
    feature_snapshot_id: str
    feature_snapshot_checksum: str
    candidate_universe_version: str
    research_status: str
    validation_experiment_id: str | None = None
    created_at: datetime
    items: list[FactorSignalItemOut]
    checksum: str


class FactorExperimentPlanSchema(BaseSchema):
    in_sample_start: date
    in_sample_end: date
    oos_start: date
    oos_end: date
    trial_budget: int = Field(gt=0, le=10000)
    benchmark_symbol: str = Field(min_length=1, max_length=32)
    transaction_cost_bps: float = Field(ge=0)
    quantiles: int = Field(default=5, ge=2, le=20)


class FactorExperimentCreate(BaseSchema):
    """只登记冻结实验;不会启动回测或任何交易。"""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    hypothesis: str = Field(min_length=10, max_length=2000)
    factor_names: list[str] = Field(min_length=1)
    dataset_release_id: str = Field(min_length=1, max_length=128)
    feature_snapshot_id: str = Field(min_length=1, max_length=64)
    plan: FactorExperimentPlanSchema
    comparison_group: str = Field(min_length=1, max_length=64)
    validation_experiment_id: str | None = Field(default=None, max_length=32)


class FactorExperimentOut(BaseSchema):
    experiment_id: str
    hypothesis: str
    factor_names: list[str]
    dataset_release_id: str
    dataset_release_checksum: str
    feature_snapshot_id: str
    plan: dict[str, Any]
    comparison_group: str
    status: str
    validation_experiment_id: str | None = None
    result: dict[str, Any] | None = None
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- 多资产元数据(issue #58)


class EtfMetadataOut(BaseSchema):
    code: str
    fund_code: str
    category: str
    execution_profile: str | None = None
    underlying_market: str = "domestic"
    strategy_type: str = "index"
    underlying_index: str | None = None
    underlying_asset_class: str = "equity"
    management_fee_rate: Decimal | None = None
    custody_fee_rate: Decimal | None = None
    tracking_error: Decimal | None = None
    inception_date: date | None = None
    listing_date: date | None = None
    delisting_date: date | None = None
    iopv_available: bool = False
    allows_t_plus_0: bool = False
    dividend_policy: str = "cash"
    source: str = "manual"
    rule_version: str = ""
    confidence: Decimal = Decimal("0")
    review_status: str = "needs_review"
    evidence: list[str] = []
    manual_override: bool = False


class EtfClassificationUpdate(BaseSchema):
    """人工补齐或修正研究用 ETF 分类(issue #97 多维分类)。"""

    execution_profile: (
        Literal[
            "domestic_equity_etf",
            "cross_border_etf",
            "bond_etf",
            "money_market_etf",
            "commodity_etf",
        ]
        | None
    ) = None
    underlying_market: Literal["domestic", "hk", "overseas", "global"] | None = None
    strategy_type: Literal["index", "active"] | None = None
    underlying_index: str | None = Field(default=None, max_length=32)
    reason: str = Field(default="", max_length=500)

    @field_validator("underlying_index")
    @classmethod
    def normalize_underlying_index(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        return normalized or None


class EtfSyncPreviewOut(BaseSchema):
    total: int
    to_insert: int
    to_update: int
    skipped_override: int
    needs_review: int
    auto_adopted: int


class EtfSyncRequest(BaseSchema):
    """触发 ETF 元数据批量同步(dry-run 预览或实际写入)。"""

    dry_run: bool = True
    enrich_codes: list[str] = Field(default_factory=list)


class EtfBatchConfirmRequest(BaseSchema):
    codes: list[str] = Field(min_length=1, max_length=2000)
    reason: str = Field(default="", max_length=500)


class EtfMetadataSummaryOut(BaseSchema):
    total: int
    auto_adopted: int
    needs_review: int
    manually_confirmed: int
    manually_overridden: int
    missing_metadata: int


class EtfAuditOut(BaseSchema):
    id: int
    code: str
    field_name: str
    old_value: str | None = None
    new_value: str | None = None
    changed_by: str = "system"
    reason: str = ""
    changed_at: datetime


class BondMetadataOut(BaseSchema):
    code: str
    face_value: Decimal = Decimal("100")
    coupon_rate: Decimal | None = None
    coupon_frequency: str = "annual"
    issue_date: date | None = None
    maturity_date: date | None = None
    issuer: str | None = None
    credit_rating: str | None = None
    credit_entity_type: str | None = None
    duration_years: Decimal | None = None
    yield_to_maturity: Decimal | None = None


class ConvertibleMetadataOut(BaseSchema):
    code: str
    underlying_stock_code: str
    conversion_price: Decimal
    conversion_ratio: Decimal | None = None
    conversion_premium: Decimal | None = None
    issue_date: date | None = None
    maturity_date: date | None = None
    coupon_schedule: list[Any] = Field(default_factory=list)
    redemption_yield: Decimal | None = None
    forced_redeem_trigger: Decimal | None = None
    put_back_trigger: Decimal | None = None
    downward_revision_trigger: Decimal | None = None


class FuturesContractOut(BaseSchema):
    contract_code: str
    series_id: str
    underlying_symbol: str
    exchange: str
    multiplier: Decimal
    margin_rate: Decimal
    price_limit_pct: Decimal
    price_tick: Decimal
    listing_date: date | None = None
    last_trade_date: date | None = None
    delivery_date: date | None = None
    delivery_method: str = "cash"
    settle_price: Decimal | None = None
    open_interest: Decimal | None = None


class LifecycleEventOut(BaseSchema):
    id: int
    symbol: str
    event_type: str
    effective_date: date
    available_at: datetime
    source: str
    dataset_version: str
    details: dict[str, Any] = Field(default_factory=dict)


class DatasetManifestOut(BaseSchema):
    id: int
    dataset_name: str
    source: str
    version: str
    start_date: date | None = None
    end_date: date | None = None
    row_count: int = 0
    symbol_count: int = 0
    # issue #349:coverage_pct 用 float 声明,pydantic 会把 Decimal / manifest
    # 里的字符串("1")归一为数值序列化,避免 JSON/前端显示成字符串 "1"。
    coverage_pct: float = 0.0
    gaps: list[Any] = Field(default_factory=list)
    checksum: str = ""
    quality_status: str = "unknown"
    quality_report: dict[str, Any] = Field(default_factory=dict)
    published_at: datetime
    code_version: str = ""


class DatasetReleaseCapabilityOut(BaseSchema):
    key: str
    status: str
    symbol_count: int
    ready_count: int
    missing_requirements: list[str] = Field(default_factory=list)


class ResearchDatasetReleaseCreate(BaseSchema):
    """从本地行情缓存创建不可变研究数据发布。

    缓存目录、发布目录和代码版本均由服务端决定,网页不能提交文件路径或
    可执行内容。
    """

    release_id: str = Field(
        min_length=3,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    dataset_name: str = Field(
        default="multi_asset_daily_bars",
        min_length=3,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    release_kind: Literal[
        "a_share_tushare",
        "multi_asset_mixed",
        # issue #187:研究数据发布(daily_metrics / financial_indicators 从
        # research_* 表冻结,非本地行情缓存)。
        "daily_metrics",
        "financial_indicators",
        # issue #265:可转债派生指标发布(转股价值/转股溢价率,从本地缓存
        # bars x 冻结转股价元数据计算)。
        "convertible_metrics",
    ] = "a_share_tushare"
    source: Literal["akshare", "yfinance", "tushare", "mixed", "manual"] | None = None
    version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    # #261:标的集来源三选一(互斥)——内联 symbols / 从既有发布复制 /
    # instruments 全活跃展开。入队期由 ``resolve_release_symbols`` 解析成
    # 具体 symbols 进任务 payload,执行器零改动。
    symbols: list[str] | None = Field(default=None, max_length=10_000)
    symbols_from_release: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    full_market: bool = False
    start_date: date
    end_date: date
    adjustment: Literal["qfq", "hqfq", "none"] = "qfq"
    required_capabilities: list[
        Literal[
            "stock",
            "bond",
            "convertible",
            "futures",
            "index",
            "etf:index",
            "etf:cross_border",
            "etf:commodity",
            "etf:bond",
        ]
    ] = Field(default_factory=list, max_length=8)
    # #252:跨发布标的集一致性校验(可选)——指定同区间关联发布(如 bars 主
    # 发布)为基线做并集差集校验,差集具名可见;默认只 warning 不阻断,
    # consistency_fail_on_mismatch=true 时发布任务秒级失败。
    consistency_baseline_release_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    consistency_fail_on_mismatch: bool = False

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [symbol.strip().upper() for symbol in value if symbol.strip()]
        if not normalized:
            raise ValueError("至少选择一个已缓存标的")
        if len(normalized) != len(set(normalized)):
            raise ValueError("发布标的不能重复")
        return normalized

    @model_validator(mode="after")
    def validate_symbol_source(self) -> ResearchDatasetReleaseCreate:
        # #261:标的集来源必须恰好声明一种;来源解析(存在 / 可用 / 展开非空)
        # 由入队路径调 ``resolve_release_symbols`` 兜底(需查库)。
        declared = [
            name
            for name, value in (
                ("symbols", self.symbols is not None),
                ("symbols_from_release", self.symbols_from_release is not None),
                ("full_market", self.full_market),
            )
            if value
        ]
        if not declared:
            raise ValueError(
                "必须提供 symbols / symbols_from_release / full_market 之一"
            )
        if len(declared) > 1:
            raise ValueError(
                "symbols / symbols_from_release / full_market 只能三选一,"
                f"同时声明: {declared}"
            )
        return self

    @model_validator(mode="after")
    def validate_date_range(self) -> ResearchDatasetReleaseCreate:
        if self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        expected_source = _RELEASE_KIND_SOURCE[self.release_kind]
        if self.source is not None and self.source != expected_source:
            raise ValueError(
                f"{self.release_kind} 发布的数据来源必须是 {expected_source}"
            )
        return self


_RELEASE_KIND_SOURCE: dict[str, str] = {
    "a_share_tushare": "tushare",
    "multi_asset_mixed": "mixed",
    "daily_metrics": "tushare",
    "financial_indicators": "tushare",
    "convertible_metrics": "tushare",
}


class DatasetReleaseInstrumentOut(BaseSchema):
    code: str
    name: str
    market: str
    instrument_type: str
    asset_class: str
    available_at: datetime
    execution: dict[str, Any]
    artifact_path: str
    artifact_checksum: str
    artifact_size: int
    row_count: int
    start_date: date
    end_date: date
    expected_sessions: int
    missing_sessions: int
    suspended_sessions: int
    anomaly_count: int
    # issue #349:manifest 逐标的 coverage_pct 冻结为 str(Decimal),这里只在
    # API 层归一为 float 数值,manifest checksum 语义不受影响。
    coverage_pct: float
    category: str
    ready: bool
    issues: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    exchange: str | None = None
    listing_board: str = "unknown"
    currency: str = "CNY"
    etf_category: str | None = None
    list_date: date | None = None
    delist_date: date | None = None
    status: str
    metadata_complete: bool
    lifecycle_events: list[dict[str, Any]] = Field(default_factory=list)
    present_event_types: list[str] = Field(default_factory=list)
    required_event_types: list[str] = Field(default_factory=list)
    name_history: list[dict[str, Any]] = Field(default_factory=list)


class ResearchDatasetReleaseOut(BaseSchema):
    release_id: str
    dataset_name: str
    source: str
    version: str
    schema_version: str
    start_date: date
    end_date: date
    period: str
    adjustment: str
    fields: list[str]
    dataset_kind: str = "bars"
    availability_rules: list[dict[str, str]]
    code_version: str
    published_at: datetime
    instruments: list[DatasetReleaseInstrumentOut]
    capabilities: list[DatasetReleaseCapabilityOut]
    quality_status: str
    quality_report: dict[str, Any]
    symbol_count: int
    row_count: int
    # issue #349:数值化序列化,避免 pydantic 把 Decimal 输出成 JSON 字符串。
    coverage_pct: float
    known_limitations: list[str] = Field(default_factory=list)
    storage_uri: str
    metadata_version: str
    release_checksum: str


class ResearchDatasetReleaseSummaryOut(BaseSchema):
    release_id: str
    dataset_name: str
    source: str
    version: str
    schema_version: str
    start_date: date
    end_date: date
    period: str
    adjustment: str
    dataset_kind: str = "bars"
    code_version: str
    published_at: datetime
    symbol_count: int
    row_count: int
    # issue #349:数值化序列化,避免 pydantic 把 Decimal 输出成 JSON 字符串。
    coverage_pct: float
    capabilities: list[DatasetReleaseCapabilityOut]
    quality_status: str
    known_limitations: list[str] = Field(default_factory=list)
    metadata_version: str
    release_checksum: str


class DataPreviewOut(BaseSchema):
    """数据预览(只读):缓存/冻结发布 parquet 的尾部行采样。"""

    label: str
    columns: list[str]
    rows: list[dict[str, Any]]
    total_rows: int
    truncated: bool
    artifact: str


class DatasetReleaseSymbolCheckOut(BaseSchema):
    """轻量发布成员核对(issue #238,免全量 detail)。"""

    release_id: str
    requested: int
    matched: list[str]
    missing: list[str]


class DatasetReleaseSymbolDiffOut(BaseSchema):
    """两份发布标的集 diff(issue #252:计数精确,清单有界预览)。"""

    release_id: str
    other_release_id: str
    symbol_count_a: int
    symbol_count_b: int
    common_count: int
    only_in_release_count: int
    only_in_other_count: int
    only_in_release: list[str]
    only_in_other: list[str]
    preview_limit: int
    truncated: bool
    consistent: bool
