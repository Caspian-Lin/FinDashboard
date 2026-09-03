"""无代码研究策略的版本化结构契约(issue #79)。

本模块只描述研究策略从候选池到验证计划的声明。不包含 Python 源码、动态导入、
Broker、订单或持仓操作。所有可选能力均由枚举和白名单字段表达。解析结果只能交给
后续研究/回测编排器。保存或发布规格本身不会启动任何运行。
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from finboard_shared.types import AssetClass, Market

STRATEGY_SPEC_SCHEMA_VERSION = "v1"

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_:-]*$",
    ),
]

_FORBIDDEN_KEYS = frozenset(
    {
        "callable",
        "class_path",
        "code",
        "eval",
        "exec",
        "expression",
        "file",
        "file_path",
        "function",
        "import",
        "imports",
        "module",
        "module_path",
        "python",
        "python_code",
        "source_code",
        "template",
    }
)
_EXECUTABLE_PATTERNS = (
    re.compile(r"__import__\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:eval|exec|compile)\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:os\.system|subprocess\.)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])[^/\\]+\.py(?:$|[?#])", re.IGNORECASE),
    re.compile(r"(?:\{\{|\{%|<%)"),
    re.compile(r"\blambda\s+[^:]+:", re.IGNORECASE),
)


class StrategySpecError(ValueError):
    """策略规格不完整、不安全或不可解析。"""


def reject_executable_payload(value: object, *, path: str = "$") -> None:
    """递归拒绝源码、模块路径、模板和可执行表达式。

    这是 schema 的纵深防御。``extra="forbid"`` 仍是拒绝越权字段的第一道边界。
    普通说明文字可以提到 Python。只有可执行形态会被拒绝。
    """

    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS:
                raise StrategySpecError(f"{path}.{key}: 禁止提交代码或可执行引用字段")
            reject_executable_payload(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            reject_executable_payload(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        for pattern in _EXECUTABLE_PATTERNS:
            if pattern.search(value):
                raise StrategySpecError(f"{path}: 禁止提交代码、模板或可执行表达式")


class NoCodeModel(BaseModel):
    """所有无代码规格节点的严格基类。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )

    @model_validator(mode="before")
    @classmethod
    def reject_executable_input(cls, value: object) -> object:
        reject_executable_payload(value)
        return value


class MissingDataPolicy(StrEnum):
    EXCLUDE = "exclude"
    RANK_WORST = "rank_worst"


class RankingDirection(StrEnum):
    TOP = "top"
    BOTTOM = "bottom"


class UniverseRanking(NoCodeModel):
    field: Identifier
    direction: RankingDirection = RankingDirection.TOP


class UniverseSpec(NoCodeModel):
    """可解释候选池与纳入/排除规则。"""

    markets: tuple[Market, ...] = Field(min_length=1)
    asset_classes: tuple[AssetClass, ...] = Field(min_length=1)
    explicit_symbols: tuple[str, ...] = ()
    min_listing_days: int = Field(default=60, ge=0, le=10000)
    min_average_amount: float | None = Field(default=None, ge=0)
    min_price: float | None = Field(default=None, gt=0)
    max_price: float | None = Field(default=None, gt=0)
    # 市值单位:人民币元,与 daily_metrics.total_market_cap 派生的
    # ``market_cap`` 特征观测一致(tushare total_mv 万元 x 1e4)。
    min_market_cap: float | None = Field(default=None, ge=0)
    max_market_cap: float | None = Field(default=None, gt=0)
    exclude_suspended: bool = True
    exclude_delisted: bool = True
    exclude_st: bool = True
    excluded_event_types: tuple[Identifier, ...] = ()
    required_data_fields: tuple[Identifier, ...] = ()
    min_data_completeness: float = Field(default=0.95, ge=0, le=1)
    ranking: UniverseRanking | None = None
    selection_limit: int = Field(default=20, ge=1, le=10000)
    missing_data_policy: MissingDataPolicy = MissingDataPolicy.EXCLUDE

    @model_validator(mode="after")
    def validate_bounds(self) -> UniverseSpec:
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price 不能大于 max_price")
        if (
            self.min_market_cap is not None
            and self.max_market_cap is not None
            and self.min_market_cap > self.max_market_cap
        ):
            raise ValueError("min_market_cap 不能大于 max_market_cap")
        if len(self.explicit_symbols) != len(set(self.explicit_symbols)):
            raise ValueError("explicit_symbols 不允许重复")
        return self


class FeatureKind(StrEnum):
    FACTOR = "factor"
    MARKET_INPUT = "market_input"
    RISK_FACTOR = "risk_factor"
    TRANSFORM = "transform"
    COMPOSITE = "composite"


class FeatureOperator(StrEnum):
    IDENTITY = "identity"
    SIMPLE_MOVING_AVERAGE = "sma"
    EXPONENTIAL_MOVING_AVERAGE = "ema"
    RETURN = "return"
    VOLATILITY = "volatility"
    ZSCORE = "zscore"
    CROSS_SECTION_RANK = "cross_section_rank"
    WINSORIZE = "winsorize"
    NEGATE = "negate"
    SUBTRACT = "subtract"
    RATIO = "ratio"
    WEIGHTED_SUM = "weighted_sum"


class FeatureNode(NoCodeModel):
    """白名单特征节点。不接受通用参数字典或表达式。"""

    node_id: Identifier
    label: str = Field(min_length=1, max_length=100)
    kind: FeatureKind
    operator: FeatureOperator
    source: Identifier | None = None
    inputs: tuple[Identifier, ...] = ()
    window: int | None = Field(default=None, ge=2, le=1000)
    weights: tuple[float, ...] = ()
    lower_percentile: float | None = Field(default=None, ge=0, le=1)
    upper_percentile: float | None = Field(default=None, ge=0, le=1)
    lag_bars: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def validate_operator_contract(self) -> FeatureNode:
        unary_window = {
            FeatureOperator.SIMPLE_MOVING_AVERAGE,
            FeatureOperator.EXPONENTIAL_MOVING_AVERAGE,
            FeatureOperator.RETURN,
            FeatureOperator.VOLATILITY,
            FeatureOperator.ZSCORE,
        }
        unary_no_window = {
            FeatureOperator.CROSS_SECTION_RANK,
            FeatureOperator.NEGATE,
        }
        binary = {FeatureOperator.SUBTRACT, FeatureOperator.RATIO}

        if self.operator is FeatureOperator.IDENTITY:
            if self.source is None or self.inputs:
                raise ValueError("identity 节点必须声明 source 且不能声明 inputs")
            if (
                self.window is not None
                or self.weights
                or self.lower_percentile is not None
                or self.upper_percentile is not None
            ):
                raise ValueError("identity 节点包含不适用参数")
        elif self.operator in unary_window:
            if len(self.inputs) != 1 or self.window is None or self.source is not None:
                raise ValueError(f"{self.operator.value} 必须有一个 input 和 window")
            if (
                self.weights
                or self.lower_percentile is not None
                or self.upper_percentile is not None
            ):
                raise ValueError(f"{self.operator.value} 包含不适用参数")
        elif self.operator in unary_no_window:
            if len(self.inputs) != 1 or self.source is not None:
                raise ValueError(f"{self.operator.value} 必须有且只有一个 input")
            if (
                self.window is not None
                or self.weights
                or self.lower_percentile is not None
                or self.upper_percentile is not None
            ):
                raise ValueError(f"{self.operator.value} 包含不适用参数")
        elif self.operator in binary:
            if len(self.inputs) != 2 or self.source is not None:
                raise ValueError(f"{self.operator.value} 必须有两个 inputs")
            if (
                self.window is not None
                or self.weights
                or self.lower_percentile is not None
                or self.upper_percentile is not None
            ):
                raise ValueError(f"{self.operator.value} 包含不适用参数")
        elif self.operator is FeatureOperator.WEIGHTED_SUM:
            if len(self.inputs) < 2 or len(self.weights) != len(self.inputs):
                raise ValueError("weighted_sum 的 weights 必须与至少两个 inputs 一一对应")
            if abs(sum(self.weights) - 1.0) > 1e-6:
                raise ValueError("weighted_sum 的 weights 总和必须为 1")
            if self.source is not None:
                raise ValueError("weighted_sum 不能声明 source")
            if (
                self.window is not None
                or self.lower_percentile is not None
                or self.upper_percentile is not None
            ):
                raise ValueError("weighted_sum 包含不适用参数")
        elif self.operator is FeatureOperator.WINSORIZE:
            if len(self.inputs) != 1 or self.source is not None:
                raise ValueError("winsorize 必须有且只有一个 input")
            if self.lower_percentile is None or self.upper_percentile is None:
                raise ValueError("winsorize 必须声明上下百分位")
            if self.lower_percentile >= self.upper_percentile:
                raise ValueError("winsorize 下百分位必须小于上百分位")
            if self.window is not None or self.weights:
                raise ValueError("winsorize 包含不适用参数")
        return self


class FeatureGraph(NoCodeModel):
    nodes: tuple[FeatureNode, ...]
    outputs: tuple[Identifier, ...]

    @model_validator(mode="after")
    def validate_graph(self) -> FeatureGraph:
        node_map = {node.node_id: node for node in self.nodes}
        if len(node_map) != len(self.nodes):
            raise ValueError("FeatureGraph node_id 不允许重复")
        unknown_outputs = sorted(set(self.outputs) - set(node_map))
        if unknown_outputs:
            raise ValueError(f"FeatureGraph outputs 引用了未知节点: {unknown_outputs}")

        for node in self.nodes:
            missing = sorted(set(node.inputs) - set(node_map))
            if missing:
                raise ValueError(f"节点 {node.node_id} 引用了未知依赖: {missing}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError(f"FeatureGraph 存在循环依赖: {node_id}")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in node_map[node_id].inputs:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in node_map:
            visit(node_id)
        return self


class SignalAction(StrEnum):
    BUY = "buy"
    SELL = "sell"
    NEUTRAL = "neutral"


class SignalComparator(StrEnum):
    GREATER_THAN = "gt"
    GREATER_THAN_OR_EQUAL = "gte"
    LESS_THAN = "lt"
    LESS_THAN_OR_EQUAL = "lte"
    BETWEEN = "between"
    CROSS_ABOVE = "cross_above"
    CROSS_BELOW = "cross_below"
    RANK_TOP = "rank_top"
    RANK_BOTTOM = "rank_bottom"


class SignalRule(NoCodeModel):
    rule_id: Identifier
    feature_id: Identifier
    comparator: SignalComparator
    action: SignalAction
    threshold: float | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    reference_feature_id: Identifier | None = None
    validity_bars: int = Field(default=1, ge=1, le=1000)
    priority: int = Field(default=100, ge=0, le=10000)
    conflict_group: Identifier = "default"
    rationale: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_comparator_contract(self) -> SignalRule:
        threshold_comparators = {
            SignalComparator.GREATER_THAN,
            SignalComparator.GREATER_THAN_OR_EQUAL,
            SignalComparator.LESS_THAN,
            SignalComparator.LESS_THAN_OR_EQUAL,
            SignalComparator.RANK_TOP,
            SignalComparator.RANK_BOTTOM,
        }
        cross_comparators = {
            SignalComparator.CROSS_ABOVE,
            SignalComparator.CROSS_BELOW,
        }
        if self.comparator in threshold_comparators and self.threshold is None:
            raise ValueError(f"{self.comparator.value} 必须声明 threshold")
        if self.comparator in threshold_comparators and (
            self.lower_bound is not None
            or self.upper_bound is not None
            or self.reference_feature_id is not None
        ):
            raise ValueError(f"{self.comparator.value} 包含不适用参数")
        if self.comparator is SignalComparator.BETWEEN:
            if self.lower_bound is None or self.upper_bound is None:
                raise ValueError("between 必须声明 lower_bound 和 upper_bound")
            if self.lower_bound >= self.upper_bound:
                raise ValueError("lower_bound 必须小于 upper_bound")
            if self.threshold is not None or self.reference_feature_id is not None:
                raise ValueError("between 包含不适用参数")
        if self.comparator in cross_comparators and self.reference_feature_id is None:
            raise ValueError(f"{self.comparator.value} 必须声明 reference_feature_id")
        if self.comparator in cross_comparators and (
            self.threshold is not None
            or self.lower_bound is not None
            or self.upper_bound is not None
        ):
            raise ValueError(f"{self.comparator.value} 包含不适用参数")
        if self.comparator in {SignalComparator.RANK_TOP, SignalComparator.RANK_BOTTOM}:
            assert self.threshold is not None
            if not 0 < self.threshold <= 1:
                raise ValueError("排名阈值必须落在 (0, 1]")
        return self


class SignalConflictPolicy(StrEnum):
    HIGHEST_PRIORITY = "highest_priority"
    NEUTRALIZE = "neutralize"


class SignalRules(NoCodeModel):
    rules: tuple[SignalRule, ...]
    conflict_policy: SignalConflictPolicy = SignalConflictPolicy.HIGHEST_PRIORITY
    default_action: SignalAction = SignalAction.NEUTRAL

    @model_validator(mode="after")
    def validate_unique_rules(self) -> SignalRules:
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("SignalRules rule_id 不允许重复")
        return self


class AllocationMethod(StrEnum):
    EQUAL_WEIGHT = "equal_weight"
    SIGNAL_WEIGHT = "signal_weight"
    INVERSE_VOLATILITY = "inverse_volatility"
    EQUAL_RISK_CONTRIBUTION = "equal_risk_contribution"
    VOLATILITY_SCALED = "volatility_scaled"
    # issue #266:最大 IR(切点)组合,依赖协方差(LW 估计)可用;
    # 仅新增枚举成员,旧规格未声明该值,序列化/checksum 零漂移。
    MAX_IR = "max_ir"


class ConstraintName(StrEnum):
    CAPITAL_TIER = "capital_tier"
    LOT_SIZE = "lot_size"
    LIQUIDITY = "liquidity"
    ASSET_CLASS_CAP = "asset_class_cap"
    TURNOVER = "turnover"
    MARGIN = "margin"
    LONG_ONLY = "long_only"


class PortfolioConstraintRef(NoCodeModel):
    name: ConstraintName
    limit: float | None = Field(default=None, ge=0)
    rationale: str = Field(min_length=1, max_length=500)


class PortfolioPolicy(NoCodeModel):
    """信号到目标权重的映射。不包含订单数量、方向或 Broker 字段。"""

    allocation_method: AllocationMethod
    investable_actions: tuple[SignalAction, ...] = (SignalAction.BUY,)
    max_positions: int = Field(default=20, ge=1, le=10000)
    min_target_weight: float = Field(default=0, ge=0, le=1)
    max_target_weight: float = Field(default=0.1, gt=0, le=1)
    target_gross_exposure: float = Field(default=0.95, ge=0, le=10)
    target_net_exposure: float = Field(default=0.95, ge=-10, le=10)
    cash_buffer: float = Field(default=0.05, ge=0, lt=1)
    rebalance_threshold: float = Field(default=0.02, ge=0, le=1)
    constraint_refs: tuple[PortfolioConstraintRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_policy(self) -> PortfolioPolicy:
        if self.min_target_weight > self.max_target_weight:
            raise ValueError("min_target_weight 不能大于 max_target_weight")
        if abs(self.target_net_exposure) > self.target_gross_exposure:
            raise ValueError("target_net_exposure 绝对值不能大于 target_gross_exposure")
        if self.target_gross_exposure + self.cash_buffer > 1.000001:
            margin_enabled = any(
                ref.name is ConstraintName.MARGIN for ref in self.constraint_refs
            )
            if not margin_enabled:
                raise ValueError("无保证金约束时目标总敞口与现金缓冲之和不能超过 1")
        return self


class RiskExitType(StrEnum):
    PRICE_STOP_LOSS = "price_stop_loss"
    VOLATILITY_STOP = "volatility_stop"
    TAKE_PROFIT = "take_profit"
    MAX_HOLDING_DAYS = "max_holding_days"
    PORTFOLIO_DRAWDOWN_DERISK = "portfolio_drawdown_derisk"
    COOLDOWN = "cooldown"


class RiskExitRule(NoCodeModel):
    rule_type: RiskExitType
    enabled: bool = False
    threshold: float | None = Field(default=None, gt=0)
    lookback: int | None = Field(default=None, ge=2, le=1000)
    days: int | None = Field(default=None, ge=0, le=10000)
    target_gross_exposure: float | None = Field(default=None, ge=0, le=10)
    rationale: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_rule(self) -> RiskExitRule:
        if not self.enabled:
            return self
        threshold_rules = {
            RiskExitType.PRICE_STOP_LOSS,
            RiskExitType.VOLATILITY_STOP,
            RiskExitType.TAKE_PROFIT,
            RiskExitType.PORTFOLIO_DRAWDOWN_DERISK,
        }
        day_rules = {RiskExitType.MAX_HOLDING_DAYS, RiskExitType.COOLDOWN}
        if self.rule_type in threshold_rules and self.threshold is None:
            raise ValueError(f"{self.rule_type.value} 启用时必须声明 threshold")
        if self.rule_type in day_rules and self.days is None:
            raise ValueError(f"{self.rule_type.value} 启用时必须声明 days")
        if self.rule_type is RiskExitType.VOLATILITY_STOP and self.lookback is None:
            raise ValueError("volatility_stop 启用时必须声明 lookback")
        if (
            self.rule_type is RiskExitType.PORTFOLIO_DRAWDOWN_DERISK
            and self.target_gross_exposure is None
        ):
            raise ValueError("portfolio_drawdown_derisk 必须声明 target_gross_exposure")
        return self


class RiskExitPolicy(NoCodeModel):
    rules: tuple[RiskExitRule, ...]

    @model_validator(mode="after")
    def validate_unique_rules(self) -> RiskExitPolicy:
        kinds = [rule.rule_type for rule in self.rules]
        if len(kinds) != len(set(kinds)):
            raise ValueError("同一种退出/风控规则只能声明一次")
        return self


class ExecutionTiming(StrEnum):
    NEXT_OPEN = "next_open"
    NEXT_CLOSE = "next_close"


class ExecutionModel(NoCodeModel):
    """研究成交假设。不生成订单、不引用 Broker。"""

    timing: ExecutionTiming = ExecutionTiming.NEXT_OPEN
    matching_model_version: str = Field(default="v2", min_length=1, max_length=32)
    commission_rate: float = Field(default=0.0003, ge=0, le=0.1)
    minimum_commission: float = Field(default=5, ge=0)
    sell_tax_rate: float = Field(default=0.0005, ge=0, le=0.1)
    slippage_bps: float = Field(default=5, ge=0, le=10000)
    max_volume_participation: float = Field(default=0.1, gt=0, le=1)
    enforce_lot_size: bool = True
    enforce_price_limits: bool = True
    reject_same_bar_fill: Literal[True] = True


class ValidationMode(StrEnum):
    HOLDOUT = "holdout"
    ROLLING = "rolling"
    ANCHORED = "anchored"


class ValidationPlanSpec(NoCodeModel):
    dataset_release_ids: tuple[str, ...] = Field(min_length=1)
    mode: ValidationMode = ValidationMode.ROLLING
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    trial_budget: int = Field(default=20, ge=1, le=10000)
    benchmark_symbol: str = Field(min_length=1, max_length=32)
    cost_multipliers: tuple[float, ...] = (1.0, 2.0)
    require_out_of_sample: Literal[True] = True
    require_walk_forward: bool = True

    @model_validator(mode="after")
    def validate_periods(self) -> ValidationPlanSpec:
        boundaries = (
            self.train_start,
            self.train_end,
            self.validation_start,
            self.validation_end,
            self.test_start,
            self.test_end,
        )
        if list(boundaries) != sorted(boundaries):
            raise ValueError("训练、验证、测试日期必须按时间顺序且不重叠")
        if any(multiplier < 1 for multiplier in self.cost_multipliers):
            raise ValueError("成本压力倍数必须 >= 1")
        if len(self.dataset_release_ids) != len(set(self.dataset_release_ids)):
            raise ValueError("dataset_release_ids 不允许重复")
        return self


class LegacyCompatibility(NoCodeModel):
    issue: int = Field(ge=1)
    legacy_kind: Identifier
    legacy_config_version: str = Field(min_length=1, max_length=32)


class StrategyCodeArtifactRef(NoCodeModel):
    """``user_code`` 策略引用的研究代码 artifact(issue #218)。

    只携带**引用**(name + 可选 commit),不携带任何源码 —— web 通道的
    无代码边界不变:代码本体只能经 ``finboard_research_code_submit``
    (MCP 通道,#215)进入独立 git 仓库。``commit`` 省略 = 引用 active
    版本(入队期解析冻结进 manifest;指定历史 commit 须先 rollback)。
    ``artifact_id``(#234)由入队期冻结:显式 screen 绑定放行时写入精确
    产物行,普通(active 引用)入队不携带,保持 None。
    """

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    commit: str | None = Field(default=None, min_length=8, max_length=64)
    artifact_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
        pattern=r"^RC-[0-9a-f]{16,32}$",
    )


class ScreenArtifactBinding(NoCodeModel):
    """screen 用途的 draft 代码产物显式绑定(issue #234)。

    #219 晋级门要求完成的 ResearchRun 提供 screen 证据,而编译期/入队名单
    只认 ``active + promotion_status=passed`` —— draft 产物永远拿不到 screen
    证据(鸡生蛋)。本绑定声明「该 screen 规格显式引用这个精确产物」:
    **只携带引用(kind/name/artifact_id/可选 commit),不携带任何源码**,web
    通道无代码边界不变。编译期把绑定名并入可引用名单(仅本规格生效);
    入队期(REST+MCP 共用门控)按 DB 逐条实绑校验(存在 / 非 retired /
    kind 与 name 一致 / commit 一致)并把 commit + artifact_id 冻结进
    manifest;promote 侧四向一致性校验(name/kind/artifact_id/commit +
    快照 source_run_id 追溯)兜底。非 screen 引用门行为完全不变。
    """

    kind: Literal["factor", "strategy"]
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    artifact_id: str = Field(pattern=r"^RC-[0-9a-f]{16,32}$")
    commit: str | None = Field(default=None, min_length=8, max_length=64)


class ResearchStrategySpec(NoCodeModel):
    """研究策略的完整闭环声明。"""

    schema_version: Literal["v1"] = "v1"
    strategy_id: Identifier
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    strategy_kind: Identifier
    universe: UniverseSpec
    feature_graph: FeatureGraph
    signal_rules: SignalRules
    portfolio_policy: PortfolioPolicy
    risk_exit_policy: RiskExitPolicy
    execution_model: ExecutionModel
    validation_plan: ValidationPlanSpec
    compatibility: LegacyCompatibility | None = None
    # issue #218:user_code 策略的代码 artifact 引用(其余 kind 必须为 None)。
    code_artifact: StrategyCodeArtifactRef | None = None
    # issue #234:screen 用途 draft 产物显式绑定(空 = 普通规格,引用门不变)。
    screen_artifact_bindings: tuple[ScreenArtifactBinding, ...] = ()

    @model_validator(mode="after")
    def validate_cross_references(self) -> ResearchStrategySpec:
        # issue #218:user_code 的目标权重由沙箱 decide 产出,feature_graph/
        # signal_rules 允许为空(特征由代码自行从挂载数据计算);其余 kind
        # 保持既有非空契约(原 Field(min_length=1) 移到这里统一执行)。
        if self.strategy_kind == "user_code":
            if self.code_artifact is None:
                raise ValueError(
                    "user_code 策略必须声明 code_artifact(研究代码 artifact 引用,"
                    "经 finboard_research_code_submit 提交 kind=strategy)"
                )
        else:
            if self.code_artifact is not None:
                raise ValueError("code_artifact 仅用于 user_code 策略")
            if not self.feature_graph.nodes:
                raise ValueError("feature_graph.nodes 不能为空")
            if not self.feature_graph.outputs:
                raise ValueError("feature_graph.outputs 不能为空")
            if not self.signal_rules.rules:
                raise ValueError("signal_rules.rules 不能为空")
        # issue #234:screen 绑定的结构一致性(kind 唯一性 + strategy 绑定
        # 必须落在 user_code 的 code_artifact 上)。与 DB 的一致性(存在/
        # 非 retired/name 一致/commit 一致)在入队期由共享门控校验。
        seen_refs: set[tuple[str, str]] = set()
        seen_artifacts: set[str] = set()
        for binding in self.screen_artifact_bindings:
            ref = (binding.kind, binding.name)
            if ref in seen_refs:
                raise ValueError(
                    f"screen_artifact_bindings 重复绑定: kind={binding.kind} "
                    f"name={binding.name}"
                )
            if binding.artifact_id in seen_artifacts:
                raise ValueError(
                    f"screen_artifact_bindings 重复绑定 artifact: "
                    f"{binding.artifact_id}"
                )
            seen_refs.add(ref)
            seen_artifacts.add(binding.artifact_id)
            if binding.kind == "strategy":
                if self.strategy_kind != "user_code" or self.code_artifact is None:
                    raise ValueError(
                        "strategy 类 screen 绑定仅适用于 user_code 策略"
                        "(须声明 code_artifact)"
                    )
                if binding.name != self.code_artifact.name:
                    raise ValueError(
                        f"strategy 类 screen 绑定 name={binding.name} 与 "
                        f"code_artifact.name={self.code_artifact.name} 不一致"
                    )
        feature_ids = {node.node_id for node in self.feature_graph.nodes}
        for rule in self.signal_rules.rules:
            references = {rule.feature_id}
            if rule.reference_feature_id is not None:
                references.add(rule.reference_feature_id)
            missing = sorted(references - feature_ids)
            if missing:
                raise ValueError(f"信号 {rule.rule_id} 引用了未知特征: {missing}")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def migrate_strategy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把已知旧版 JSON 纯数据迁移到当前 schema。

    v0 只在早期草案中使用 ``risk_policy`` 字段。迁移是固定字段重命名、不加载
    用户代码。未知版本 fail closed。
    """

    reject_executable_payload(payload)
    version = payload.get("schema_version", "v0")
    if version == STRATEGY_SPEC_SCHEMA_VERSION:
        return dict(payload)
    if version != "v0":
        raise StrategySpecError(f"不支持的策略规格版本: {version}")
    migrated = dict(payload)
    if "risk_policy" in migrated and "risk_exit_policy" not in migrated:
        migrated["risk_exit_policy"] = migrated.pop("risk_policy")
    migrated["schema_version"] = STRATEGY_SPEC_SCHEMA_VERSION
    return migrated


__all__ = [
    "STRATEGY_SPEC_SCHEMA_VERSION",
    "AllocationMethod",
    "ConstraintName",
    "ExecutionModel",
    "ExecutionTiming",
    "FeatureGraph",
    "FeatureKind",
    "FeatureNode",
    "FeatureOperator",
    "LegacyCompatibility",
    "MissingDataPolicy",
    "NoCodeModel",
    "PortfolioConstraintRef",
    "PortfolioPolicy",
    "RankingDirection",
    "ResearchStrategySpec",
    "RiskExitPolicy",
    "RiskExitRule",
    "RiskExitType",
    "ScreenArtifactBinding",
    "SignalAction",
    "SignalComparator",
    "SignalConflictPolicy",
    "SignalRule",
    "SignalRules",
    "StrategyCodeArtifactRef",
    "StrategySpecError",
    "UniverseRanking",
    "UniverseSpec",
    "ValidationMode",
    "ValidationPlanSpec",
    "migrate_strategy_payload",
    "reject_executable_payload",
]
