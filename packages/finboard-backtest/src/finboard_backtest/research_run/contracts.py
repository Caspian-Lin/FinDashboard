"""研究回测生命周期与逐决策血缘契约(issue #80)。

本模块只描述离线研究运行。研究订单和成交均使用 ``RR-`` 命名空间,不能写入
实盘 ``orders`` / ``fills`` / ``positions`` 表,也不能转换成 Broker 请求。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, cast

from finboard_backtest.strategy_spec.contracts import (
    ResearchStrategySpec,
    reject_executable_payload,
)

RESEARCH_RUN_SCHEMA_VERSION = "v2"
RESEARCH_PORTFOLIO_PIPELINE_VERSION = "v1"
MIN_RESEARCH_CAPITAL = Decimal("100000")
MAX_RESEARCH_CAPITAL = Decimal("500000")
MONEY_EPSILON = Decimal("0.01")

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

#: legacy 调仓频率(``parameters.rebalance_frequency``,issue #183)。
#: 单快照(冻结因子快照)frozen 路径不设该参数,恒为 single_shot。
#: issue #361 兼容扩展接受 daily / weekly(旧值 monthly/quarterly 零变化,
#: 与 ``decision_schedule`` 同名 kind 互映射),决策频率的完整表达走
#: ``parameters.decision_schedule``。
REBALANCE_FREQUENCIES: frozenset[str] = frozenset(
    {"daily", "weekly", "monthly", "quarterly"}
)

#: ``decision_schedule.kind`` 合法集合(issue #361):决策频率泛化为日历服务。
#: ``custom`` 必须显式声明 ``dates``(升序去重,入队时校验 ⊆ 发布交易日)。
DECISION_SCHEDULE_KINDS: frozenset[str] = frozenset(
    {"daily", "weekly", "monthly", "quarterly", "custom"}
)


class ResearchExecutionMode(StrEnum):
    """一次 research_run 的是单时点决策还是全区间多期回放。"""

    SINGLE_SHOT = "single_shot"
    MULTI_PERIOD = "multi_period"


class ResearchRunError(RuntimeError):
    """研究运行契约或生命周期不成立。"""


class ResearchRunConflictError(ResearchRunError):
    """幂等键、artifact 或重放内容发生冲突。"""


class UnsupportedResearchCapabilityError(ResearchRunError):
    """数据或适配器不支持策略声明的能力;必须失败关闭。"""


class ResearchRunInterruptedError(ResearchRunError):
    """可恢复的研究运行中断。"""


class ResearchConstraintViolationError(ResearchRunError):
    """正式组合流水线的硬约束或阶段完整性不成立。"""


class ResearchRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


#: 允许重放(replay)的源状态(issue #305):
#:
#: - ``COMPLETED`` —— 确定性重放对照(既有语义,expected_result_checksum 生效);
#: - ``INTERRUPTED`` —— 事故恢复通道:run 被标 interrupted 后 checkpoint 保留,
#:   重放自动继承原 manifest 全部冻结输入(含 factor_snapshots 全部 ID,零手工),
#:   血缘标注 ``replay_of_run_id`` + ``replay_source_status``。
#:
#: ``CANCELLED`` 是显式用户意图,不放开;queued/running 尚有归属、failed/rejected
#: 有专属错误处置路径,均不可重放。MCP/REST/Coordinator 三处守卫共用本判定。
REPLAYABLE_SOURCE_STATUSES: frozenset[ResearchRunStatus] = frozenset(
    {ResearchRunStatus.COMPLETED, ResearchRunStatus.INTERRUPTED}
)


def replay_guard_error(status: ResearchRunStatus) -> str:
    """不可重放源状态的统一解释文案(issue #305,三处守卫共用)。"""

    return (
        f"仅允许重放 completed 或 interrupted 运行,当前状态 {status.value} 不可重放;"
        "interrupted 运行可经 finboard_run_replay(MCP)或 REST "
        "POST /api/research/runs/{run_id}/replay 按冻结输入恢复,"
        "新 run 自动继承原 manifest 全部冻结输入,无需手工重填快照 ID"
    )


class ResearchRunStage(StrEnum):
    UNIVERSE = "universe"
    FEATURES = "features"
    SIGNALS = "signals"
    TARGETS_BEFORE_CONSTRAINTS = "targets_before_constraints"
    CONSTRAINTS = "constraints"
    TARGETS_AFTER_CONSTRAINTS = "targets_after_constraints"
    RISK_EXITS = "risk_exits"
    TARGETS_AFTER_RISK = "targets_after_risk"
    CAPITAL_FEASIBILITY = "capital_feasibility"
    REBALANCE_PLAN = "rebalance_plan"
    ORDERS = "orders"
    FILLS = "fills"
    LEDGER = "ledger"
    REPORT = "report"


class ResearchActorType(StrEnum):
    HUMAN = "human"
    SYSTEM = "system"
    LLM = "llm"
    # issue #312:外置研究 agent 经 MCP 自主执行研究写操作(#122 放开),
    # MCP 通道创建的 run/replay 归属 actor_type=agent,与 requested_by=agent:mcp
    # 对齐;llm 仍被 fail-closed 拒绝(红线不变),REST 默认 human 不变。
    AGENT = "agent"


class ResearchOrderStatus(StrEnum):
    CREATED = "created"
    REJECTED = "rejected"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ResearchPositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class ResearchFillAction(StrEnum):
    OPEN_LONG = "open_long"
    CLOSE_LONG = "close_long"
    OPEN_SHORT = "open_short"
    CLOSE_SHORT = "close_short"


@dataclass(frozen=True, slots=True)
class FrozenArtifactRef:
    """冻结数据/因子/模型输入的内容寻址引用。"""

    artifact_id: str
    version: str
    checksum: str
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.artifact_id or not self.version or not self.checksum:
            raise ValueError("冻结引用必须包含 artifact_id/version/checksum")


@dataclass(frozen=True, slots=True)
class ResearchRunManifest:
    """一次研究运行的不可变输入清单。"""

    run_id: str
    idempotency_key: str
    strategy_spec: ResearchStrategySpec
    strategy_spec_checksum: str
    dataset_releases: tuple[FrozenArtifactRef, ...]
    # 策略版本不是策略 payload 的一部分,必须单独冻结,便于 UI/worker 追踪.
    # 本次运行实际引用的已发布版本. None 兼容历史 manifest.
    strategy_version: int | None = None
    factor_snapshots: tuple[FrozenArtifactRef, ...] = ()
    # issue #360:内容寻址因子序列工件引用({series_id, content_checksum});
    # 声明时 u_ 因子观测按 (因子名, 决策日) 从 series.values 取(加载器双轨
    # 优先路径),未声明回退既有快照路径。空元组不入 checksum / input_checksum
    # —— 历史 manifest 的 checksum 零漂移(同 strategy_version 先例)。
    factor_series: tuple[FrozenArtifactRef, ...] = ()
    parameters: dict[str, JsonValue] = field(default_factory=dict)
    validation_config: dict[str, JsonValue] = field(default_factory=dict)
    portfolio_config: dict[str, JsonValue] = field(default_factory=dict)
    risk_config: dict[str, JsonValue] = field(default_factory=dict)
    execution_config: dict[str, JsonValue] = field(default_factory=dict)
    fee_config: dict[str, JsonValue] = field(default_factory=dict)
    benchmark_config: dict[str, JsonValue] = field(default_factory=dict)
    code_version: str = ""
    initial_capital: Decimal = MIN_RESEARCH_CAPITAL
    requested_by: str = ""
    actor_type: ResearchActorType = ResearchActorType.HUMAN
    replay_of_run_id: str | None = None
    # issue #305:重放源状态血缘标注(completed=确定性重放对照 /
    # interrupted=事故恢复)。仅随 replay_of_run_id 一起出现;不入 input_checksum,
    # None 时也不入 manifest checksum(保持历史 manifest checksum 不漂移)。
    replay_source_status: str | None = None
    schema_version: str = RESEARCH_RUN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.run_id.startswith("RR-"):
            raise ValueError("研究 run_id 必须使用 RR- 命名空间")
        if not self.idempotency_key:
            raise ValueError("idempotency_key 不能为空")
        if not self.dataset_releases:
            raise ValueError("必须冻结至少一个数据发布")
        if self.strategy_version is not None and self.strategy_version < 1:
            raise ValueError("strategy_version 必须大于等于 1")
        if self.strategy_spec_checksum != stable_checksum(self.strategy_spec.canonical_payload()):
            raise ValueError("strategy_spec_checksum 与策略规格内容不一致")
        frozen_release_ids = tuple(item.artifact_id for item in self.dataset_releases)
        if len(frozen_release_ids) != len(set(frozen_release_ids)):
            raise ValueError("dataset_releases 不允许重复")
        if sorted(frozen_release_ids) != sorted(
            self.strategy_spec.validation_plan.dataset_release_ids
        ):
            raise ValueError("冻结数据发布必须与策略验证计划完全一致")
        factor_ids = tuple(item.artifact_id for item in self.factor_snapshots)
        if len(factor_ids) != len(set(factor_ids)):
            raise ValueError("factor_snapshots 不允许重复")
        series_ids = tuple(item.artifact_id for item in self.factor_series)
        if len(series_ids) != len(set(series_ids)):
            raise ValueError("factor_series 不允许重复")
        if not self.code_version:
            raise ValueError("必须冻结 code_version")
        if not self.requested_by:
            raise ValueError("requested_by 不能为空")
        # 红线不变(issue #312):仅 llm 拒绝;agent(#122 MCP 自主执行)放行。
        if self.actor_type is ResearchActorType.LLM:
            raise ValueError("LLM 不能触发研究运行")
        if self.replay_source_status is not None:
            if self.replay_of_run_id is None:
                raise ValueError("replay_source_status 必须与 replay_of_run_id 一起声明")
            if self.replay_source_status not in REPLAYABLE_SOURCE_STATUSES:
                raise ValueError(
                    "replay_source_status 仅接受 completed/interrupted,"
                    f"收到 {self.replay_source_status!r}"
                )
        if not MIN_RESEARCH_CAPITAL <= self.initial_capital <= MAX_RESEARCH_CAPITAL:
            raise ValueError("研究资金必须位于 10 万至 50 万元")
        reject_executable_payload(self.parameters, path="$.parameters")
        reject_executable_payload(self.validation_config, path="$.validation_config")
        reject_executable_payload(self.portfolio_config, path="$.portfolio_config")
        reject_executable_payload(self.risk_config, path="$.risk_config")
        reject_executable_payload(self.execution_config, path="$.execution_config")
        reject_executable_payload(self.fee_config, path="$.fee_config")
        reject_executable_payload(self.benchmark_config, path="$.benchmark_config")

    @property
    def checksum(self) -> str:
        payload = asdict(self)
        # 旧 manifest 没有 strategy_version; 保持其历史 checksum 可被 worker
        # 校验. 新 manifest 在提供版本时把版本纳入冻结清单 checksum.
        if self.strategy_version is None:
            payload.pop("strategy_version", None)
        # issue #305:血缘标注仅在 replay 时出现;None 时弹出,历史(非重放)
        # manifest 的 checksum 不因新增字段而漂移(同 strategy_version 先例)。
        if self.replay_source_status is None:
            payload.pop("replay_source_status", None)
        # issue #360:序列引用仅在声明时入 checksum;空时不序列化,历史
        # manifest checksum 零漂移(同 replay_source_status 先例)。
        if not self.factor_series:
            payload.pop("factor_series", None)
        return stable_checksum(payload)

    @property
    def input_checksum(self) -> str:
        """计算与运行身份无关、可跨确定性重放复用的冻结输入校验和。"""
        payload: dict[str, object] = {
            "strategy_spec": self.strategy_spec,
            "strategy_spec_checksum": self.strategy_spec_checksum,
            "dataset_releases": self.dataset_releases,
            "factor_snapshots": self.factor_snapshots,
            "parameters": self.parameters,
            "validation_config": self.validation_config,
            "portfolio_config": self.portfolio_config,
            "risk_config": self.risk_config,
            "execution_config": self.execution_config,
            "fee_config": self.fee_config,
            "benchmark_config": self.benchmark_config,
            "code_version": self.code_version,
            "initial_capital": self.initial_capital,
            "schema_version": self.schema_version,
        }
        if self.strategy_version is not None:
            payload["strategy_version"] = self.strategy_version
        # issue #360:序列引用覆盖进 input_checksum(同 strategy_version 的
        # 「仅在存在时入键」先例)—— 序列内容变化(content_checksum)使冻结
        # 输入身份变化;未声明的 run 保持历史 input_checksum 零漂移。
        if self.factor_series:
            payload["factor_series"] = self.factor_series
        return stable_checksum(payload)

    @property
    def strategy_kind(self) -> str:
        return self.strategy_spec.strategy_kind


@dataclass(frozen=True, slots=True)
class UniverseCandidate:
    symbol: str
    included: bool
    reasons: tuple[str, ...]
    asset_class: str
    market: str

    def __post_init__(self) -> None:
        if not self.symbol or not self.reasons:
            raise ValueError("候选标的必须包含 symbol 和可解释原因")


@dataclass(frozen=True, slots=True)
class FeatureValue:
    symbol: str
    feature_id: str
    value: float | None
    source_artifact_ids: tuple[str, ...]
    available_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.available_at, "FeatureValue.available_at")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("feature value 必须有限或为 None")
        if not self.source_artifact_ids:
            raise ValueError("feature 必须关联冻结来源")


@dataclass(frozen=True, slots=True)
class NormalizedSignal:
    symbol: str
    score: float
    action: str
    rule_id: str
    factor_snapshot_id: str | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.score):
            raise ValueError("信号分数必须有限")
        if not self.rule_id or not self.rationale:
            raise ValueError("信号必须包含 rule_id 和 rationale")


@dataclass(frozen=True, slots=True)
class TargetPosition:
    symbol: str
    weight: float
    position_side: ResearchPositionSide = ResearchPositionSide.LONG

    def __post_init__(self) -> None:
        if not math.isfinite(self.weight):
            raise ValueError("目标权重必须有限")
        if self.position_side is ResearchPositionSide.LONG and self.weight < 0:
            raise ValueError("多头目标权重不能为负")
        if self.position_side is ResearchPositionSide.SHORT and self.weight > 0:
            raise ValueError("空头目标权重不能为正")


@dataclass(frozen=True, slots=True)
class ConstraintOutcome:
    constraint: str
    passed: bool
    before_value: float | None
    after_value: float | None
    limit: float | None
    reason: str
    hard: bool = True

    def __post_init__(self) -> None:
        if not self.constraint or not self.reason:
            raise ValueError("约束结果必须可解释")


@dataclass(frozen=True, slots=True)
class RiskExitOutcome:
    """风险退出规则在单个研究决策时点的可审计结果。"""

    rule_type: str
    symbol: str | None
    triggered: bool
    metric: float | None
    threshold: float | None
    before_weight: float
    after_weight: float
    reason: str

    def __post_init__(self) -> None:
        if not self.rule_type or not self.reason:
            raise ValueError("风险退出结果必须包含规则类型和原因")
        values = (
            self.metric,
            self.threshold,
            self.before_weight,
            self.after_weight,
        )
        if not all(value is None or math.isfinite(value) for value in values):
            raise ValueError("风险退出指标必须为有限数或 None")


@dataclass(frozen=True, slots=True)
class ResearchRiskState:
    """可由 artifact 恢复的版本化研究退出状态。"""

    cooldown_until: dict[str, date]
    opened_on: dict[str, date]
    high_water_prices: dict[str, float]
    portfolio_equity_high_water: Decimal
    portfolio_drawdown: float
    portfolio_paused: bool
    state_version: str = "v1"

    def __post_init__(self) -> None:
        if not self.state_version:
            raise ValueError("risk state_version 不能为空")
        if self.portfolio_equity_high_water < 0:
            raise ValueError("组合权益高水位不能为负")
        if not 0 <= self.portfolio_drawdown <= 1:
            raise ValueError("组合回撤必须落在 [0, 1]")
        if any(value <= 0 or not math.isfinite(value) for value in self.high_water_prices.values()):
            raise ValueError("持仓价格高水位必须为正且有限")


@dataclass(frozen=True, slots=True)
class CapitalTierOutcome:
    """同一冻结输入下单个资金档位的可执行性摘要。"""

    tier: str
    capital: Decimal
    feasible: bool
    cash_utilization: float
    tracking_error: float
    unfillable_symbols: tuple[str, ...]
    capacity_pressure: float
    margin_required: Decimal
    estimated_costs: Decimal
    reasons: tuple[str, ...]
    input_checksum: str

    def __post_init__(self) -> None:
        if not self.tier or not self.input_checksum or not self.reasons:
            raise ValueError("资金档位结果缺少 tier/input_checksum/reasons")
        if self.capital <= 0:
            raise ValueError("资金档位 capital 必须为正")
        if min(self.margin_required, self.estimated_costs) < 0:
            raise ValueError("保证金和预计成本不能为负")
        metrics = (
            self.cash_utilization,
            self.tracking_error,
            self.capacity_pressure,
        )
        if not all(math.isfinite(value) and value >= 0 for value in metrics):
            raise ValueError("资金档位指标必须为非负有限数")


@dataclass(frozen=True, slots=True)
class ResearchPipelineEvidence:
    """证明关键阶段由正式组合流水线生成且未被事后改写。"""

    manifest_input_checksum: str
    input_checksum: str
    output_checksum: str
    hard_constraints_passed: bool
    pipeline_version: str = RESEARCH_PORTFOLIO_PIPELINE_VERSION

    def __post_init__(self) -> None:
        if (
            not self.manifest_input_checksum
            or not self.input_checksum
            or not self.output_checksum
            or not self.pipeline_version
        ):
            raise ValueError("组合流水线证据字段不能为空")


@dataclass(frozen=True, slots=True)
class RebalanceInstruction:
    instruction_id: str
    symbol: str
    action: ResearchFillAction
    target_quantity: Decimal
    current_quantity: Decimal
    delta_quantity: Decimal
    lot_size: int
    estimated_value: Decimal
    reason: str

    def __post_init__(self) -> None:
        if not self.instruction_id.startswith("RR-"):
            raise ValueError("研究调仓指令必须使用 RR- 命名空间")
        if self.lot_size <= 0:
            raise ValueError("lot_size 必须为正")
        if self.target_quantity < 0 or self.current_quantity < 0:
            raise ValueError("目标/当前数量不能为负")
        if self.delta_quantity == 0:
            raise ValueError("调仓数量不能为零")


@dataclass(frozen=True, slots=True)
class ResearchOrder:
    research_order_id: str
    instruction_id: str
    symbol: str
    action: ResearchFillAction
    quantity: Decimal
    status: ResearchOrderStatus
    reject_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.research_order_id.startswith("RR-"):
            raise ValueError("研究订单必须使用 RR- 命名空间")
        if self.quantity <= 0:
            raise ValueError("订单数量必须为正")
        if self.status is ResearchOrderStatus.REJECTED and not self.reject_reason:
            raise ValueError("拒单必须记录原因")


@dataclass(frozen=True, slots=True)
class ResearchFill:
    research_fill_id: str
    research_order_id: str
    symbol: str
    action: ResearchFillAction
    quantity: Decimal
    price: Decimal
    commission: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    filled_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.research_fill_id.startswith("RR-"):
            raise ValueError("研究成交必须使用 RR- 命名空间")
        if self.quantity <= 0 or self.price <= 0:
            raise ValueError("成交数量和价格必须为正")
        if min(self.commission, self.tax, self.slippage) < 0:
            raise ValueError("成交费用不能为负")
        _require_aware(self.filled_at, "ResearchFill.filled_at")


@dataclass(frozen=True, slots=True)
class ResearchPosition:
    symbol: str
    position_side: ResearchPositionSide
    quantity: Decimal
    average_price: Decimal
    market_price: Decimal
    market_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    margin_used: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError("持仓数量不能为负")
        if min(self.average_price, self.market_price, self.margin_used) < 0:
            raise ValueError("持仓价格/保证金不能为负")


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    cash: Decimal
    market_value: Decimal
    margin_used: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    fees_paid: Decimal
    tax_paid: Decimal
    slippage_paid: Decimal
    fill_shortfall: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if (
            min(
                self.margin_used,
                self.fees_paid,
                self.tax_paid,
                self.slippage_paid,
                self.fill_shortfall,
            )
            < 0
        ):
            raise ValueError("保证金、费用和未成交缺口不能为负")
        expected = self.cash + self.market_value
        if abs(expected - self.equity) > MONEY_EPSILON:
            raise ValueError(
                f"权益恒等式失败: cash({self.cash}) + market_value"
                f"({self.market_value}) != equity({self.equity})"
            )


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """权益曲线单点(单快照路径不产出本数据,issue #183)。"""

    trade_date: date
    equity: Decimal

    def __post_init__(self) -> None:
        if self.equity < 0:
            raise ValueError("权益不能为负")


@dataclass(frozen=True, slots=True)
class DecisionSchedule:
    """多期回放的决策日历声明(issue #361,决策频率泛化为日历服务)。

    * ``kind`` ∈ :data:`DECISION_SCHEDULE_KINDS`;
    * ``dates`` 仅 ``custom`` 必填(严格升序、去重;入队期另校验 ⊆ 发布
      交易日),其余 kind 恒为空(决策日由发布交易日历推导);
    * legacy ``parameters.rebalance_frequency``(daily/weekly/monthly/
      quarterly)解析为同名 kind 的 schedule,旧值语义零变化。

    manifest 冻结 ``parameters`` 原文(含 ``decision_schedule`` 原始 dict),
    归一化只发生在消费端(确定性重放,#183 语义不变)。
    """

    kind: str
    dates: tuple[date, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in DECISION_SCHEDULE_KINDS:
            raise ValueError(
                f"decision_schedule.kind 仅支持 {sorted(DECISION_SCHEDULE_KINDS)}: {self.kind!r}"
            )
        if self.kind == "custom" and not self.dates:
            raise ValueError("decision_schedule.kind=custom 必须声明非空 dates")
        if self.kind != "custom" and self.dates:
            raise ValueError(
                f"decision_schedule.kind={self.kind} 不接受 dates(仅 custom 声明)"
            )
        if list(self.dates) != sorted(set(self.dates)):
            raise ValueError("decision_schedule.dates 必须严格升序且不重复")


def parse_decision_schedule(raw: object) -> DecisionSchedule:
    """解析 ``parameters.decision_schedule`` 声明;非法形状抛 ``ValueError``。

    REST ``ResearchRunQueueIn`` schema 校验与执行期 :func:`resolve_decision_schedule`
    共用本函数(形状规则单一来源,不双份漂移):kind 白名单、多余键拒绝、
    custom 缺 dates 拒绝、非 custom 带 dates 拒绝、dates 须为 YYYY-MM-DD
    字符串且严格升序去重。
    """
    if not isinstance(raw, Mapping):
        raise ValueError(f"decision_schedule 须为对象(kind/dates): {raw!r}")
    extra = sorted(set(raw) - {"kind", "dates"})
    if extra:
        raise ValueError(f"decision_schedule 不支持的字段: {extra}")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in DECISION_SCHEDULE_KINDS:
        raise ValueError(
            f"decision_schedule.kind 仅支持 {sorted(DECISION_SCHEDULE_KINDS)}: {kind!r}"
        )
    dates_raw = raw.get("dates")
    if dates_raw is None:
        dates_raw = ()
    if isinstance(dates_raw, (str, bytes)) or not isinstance(dates_raw, Sequence):
        raise ValueError(
            f"decision_schedule.dates 须为 YYYY-MM-DD 字符串列表: {dates_raw!r}"
        )
    dates: list[date] = []
    for item in dates_raw:
        if not isinstance(item, str):
            raise ValueError(f"decision_schedule.dates 须为 YYYY-MM-DD 字符串: {item!r}")
        try:
            dates.append(date.fromisoformat(item))
        except ValueError as exc:
            raise ValueError(
                f"decision_schedule.dates 含无法解析的日期: {item!r}"
            ) from exc
    return DecisionSchedule(kind=kind, dates=tuple(dates))


def resolve_decision_schedule(
    parameters: Mapping[str, object],
) -> DecisionSchedule | None:
    """从 run ``parameters`` 解析决策日历声明;未声明返回 ``None``。

    优先 ``decision_schedule``;legacy ``rebalance_frequency`` 映射为同 kind
    的 schedule(旧值零变化)。非法值 / 两键同时声明 fail-closed 抛
    ``ValueError``(与既有执行期口径一致,入队侧 schema 先行拒绝)。
    """
    has_schedule = "decision_schedule" in parameters
    has_frequency = parameters.get("rebalance_frequency") is not None
    if has_schedule and has_frequency:
        raise ValueError(
            "parameters.decision_schedule 与 legacy rebalance_frequency"
            " 不可同时声明(声明 decision_schedule 即 multi_period)"
        )
    if has_schedule:
        return parse_decision_schedule(parameters["decision_schedule"])
    if not has_frequency:
        return None
    frequency = parameters["rebalance_frequency"]
    if not isinstance(frequency, str) or frequency not in REBALANCE_FREQUENCIES:
        raise ValueError(
            f"rebalance_frequency 仅支持 {sorted(REBALANCE_FREQUENCIES)}: {frequency!r}"
        )
    return DecisionSchedule(kind=frequency)


def execution_mode_for(parameters: Mapping[str, object]) -> ResearchExecutionMode:
    """按 ``parameters`` 的决策日历声明解析研究运行执行模式。

    多期回放(multi_period)必须显式声明决策日历 —— ``decision_schedule``
    (issue #361,含 custom)或 legacy ``rebalance_frequency``(daily/weekly/
    monthly/quarterly);未声明或非法值一律按单时点(single_shot)处理,
    与既有单快照路径保持一致(非法值在执行期由
    :func:`resolve_decision_schedule` fail-closed)。
    """
    if isinstance(parameters.get("decision_schedule"), Mapping):
        return ResearchExecutionMode.MULTI_PERIOD
    if parameters.get("rebalance_frequency") in REBALANCE_FREQUENCIES:
        return ResearchExecutionMode.MULTI_PERIOD
    return ResearchExecutionMode.SINGLE_SHOT


@dataclass(frozen=True, slots=True)
class DecisionBundle:
    """单个决策时点的完整闭环。每个阶段都会独立持久化。"""

    business_date: date
    decision_at: datetime
    candidates: tuple[UniverseCandidate, ...]
    features: tuple[FeatureValue, ...]
    signals: tuple[NormalizedSignal, ...]
    targets_before_constraints: tuple[TargetPosition, ...]
    constraints: tuple[ConstraintOutcome, ...]
    targets_after_constraints: tuple[TargetPosition, ...]
    risk_exits: tuple[RiskExitOutcome, ...]
    targets_after_risk: tuple[TargetPosition, ...]
    risk_state: ResearchRiskState
    capital_feasibility: tuple[CapitalTierOutcome, ...]
    rebalance_plan: tuple[RebalanceInstruction, ...]
    orders: tuple[ResearchOrder, ...]
    fills: tuple[ResearchFill, ...]
    positions: tuple[ResearchPosition, ...]
    ledger: LedgerSnapshot
    pipeline_evidence: ResearchPipelineEvidence | None = None
    decision_id: str = ""

    def __post_init__(self) -> None:
        _require_aware(self.decision_at, "DecisionBundle.decision_at")
        if self.decision_at.date() < self.business_date:
            raise ValueError("decision_at 不能早于业务日期")
        if not self.candidates:
            raise ValueError("每个决策必须记录候选池")
        _assert_unique((item.symbol for item in self.candidates), "候选标的")
        _assert_unique((item.research_order_id for item in self.orders), "研究订单")
        _assert_unique((item.research_fill_id for item in self.fills), "研究成交")
        _assert_unique(
            (f"{item.symbol}:{item.position_side.value}" for item in self.positions),
            "持仓",
        )
        _assert_unique((item.tier for item in self.capital_feasibility), "资金档位")


def pipeline_output_checksum(decision: DecisionBundle) -> str:
    """对组合流水线的关键输出计算稳定校验和,排除自身证据和 decision_id。"""
    return stable_checksum(
        {
            "signals": decision.signals,
            "targets_before_constraints": decision.targets_before_constraints,
            "constraints": decision.constraints,
            "targets_after_constraints": decision.targets_after_constraints,
            "risk_exits": decision.risk_exits,
            "targets_after_risk": decision.targets_after_risk,
            "risk_state": decision.risk_state,
            "capital_feasibility": decision.capital_feasibility,
            "rebalance_plan": decision.rebalance_plan,
            "orders": decision.orders,
            "fills": decision.fills,
            "positions": decision.positions,
            "ledger": decision.ledger,
        }
    )


@dataclass(frozen=True, slots=True)
class ResearchRunReport:
    strategy_kind: str
    strategy_return: float
    benchmark_symbol: str
    # issue #184:基准缺失时 benchmark_return/excess_return 为 None,禁止静默 0.0
    benchmark_return: float | None
    excess_return: float | None
    sharpe_ratio: float
    max_drawdown: float
    final_equity: Decimal
    final_cash: Decimal
    commission_paid: Decimal
    tax_paid: Decimal
    slippage_paid: Decimal
    fill_shortfall: Decimal
    constraint_impact: dict[str, float]
    decision_count: int
    order_count: int
    fill_count: int
    accounting_invariants_passed: bool = True
    # issue #183:多期回放绩效。single_shot 路径保持既有字段,不产出曲线。
    execution_mode: ResearchExecutionMode = ResearchExecutionMode.SINGLE_SHOT
    annualized_return: float = 0.0
    equity_curve: tuple[EquityPoint, ...] = ()
    # issue #217:因子筛选指标(IC/IR/分层收益/换手率/与既有因子相关性矩阵)。
    # 仅当 run 引用用户自定义因子(u_ 前缀,沙箱执行产出)时非空;计算
    # 失败不阻塞 run(尽力而为,失败原因记 issues)。
    factor_screen: dict[str, JsonValue] | None = None
    # issue #219:user_code 策略把逐决策目标权重作为截面 score 计算的
    # IC/换手/相关性 screen,晋级时与 #57 OOS 一起作为机器证据。
    strategy_screen: dict[str, JsonValue] | None = None
    # issue #218:user_code 策略的沙箱 provenance —— 所用 code commit、
    # 镜像 digest 与逐决策 targets checksum;非 user_code run 恒为 None。
    sandbox_provenance: dict[str, JsonValue] | None = None
    # issue #262:Sharpe 口径标注 —— 本报告 sharpe_ratio 的实际 rf 取值(恒
    # 0.0,口径 rf=0 / 样本标准差 ddof=1 / √252 年化)。与引擎报告同屏比较
    # 时,引擎侧用 sharpe_rf0(同口径),不用引擎主口径 sharpe_ratio(rf=3%)。
    risk_free_annual: float = 0.0

    def __post_init__(self) -> None:
        metrics = (
            self.strategy_return,
            self.sharpe_ratio,
            self.max_drawdown,
            self.annualized_return,
            self.risk_free_annual,
            *self.constraint_impact.values(),
        )
        if self.benchmark_return is not None:
            metrics = (*metrics, self.benchmark_return)
        if not all(math.isfinite(value) for value in metrics):
            raise ValueError("报告指标必须为有限数")
        if (self.benchmark_return is None) != (self.excess_return is None):
            raise ValueError("基准缺失时 benchmark_return 与 excess_return 必须同为 None")
        if (
            self.benchmark_return is not None
            and self.excess_return is not None
            and abs(self.strategy_return - self.benchmark_return - self.excess_return) > 1e-9
        ):
            raise ValueError("excess_return 必须等于策略收益减基准收益")
        if (
            min(
                self.final_equity,
                self.final_cash,
                self.commission_paid,
                self.tax_paid,
                self.slippage_paid,
                self.fill_shortfall,
            )
            < 0
        ):
            raise ValueError("报告金额不能为负")
        if min(self.decision_count, self.order_count, self.fill_count) < 0:
            raise ValueError("报告计数不能为负")
        if self.equity_curve:
            days = tuple(item.trade_date for item in self.equity_curve)
            if days != tuple(sorted(days)):
                raise ValueError("equity_curve 必须按交易日升序")
            if any(item.equity <= 0 for item in self.equity_curve):
                raise ValueError("equity_curve 权益必须为正")
            if self.execution_mode is not ResearchExecutionMode.MULTI_PERIOD:
                raise ValueError("只有多期回放可以产出 equity_curve")


@dataclass(frozen=True, slots=True)
class ResearchArtifact:
    artifact_id: str
    run_id: str
    decision_id: str | None
    sequence: int
    stage: ResearchRunStage
    trace_id: str
    parent_trace_ids: tuple[str, ...]
    payload: dict[str, JsonValue]
    checksum: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class ResearchRunRecord:
    manifest: ResearchRunManifest
    status: ResearchRunStatus = ResearchRunStatus.QUEUED
    result: ResearchRunReport | None = None
    result_checksum: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    # issue #143:关联 background_jobs.job_id(字符串引用);None 表示尚未接入统一队列。
    job_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # issue #285:job 级分段耗时(decision_load / decision_execute 聚合 / report),
    # 纯可观测性 —— 不进 report、不参与任何 checksum;PostgreSQL 路径冗余存放
    # 在 research_runs.result JSON 的 "timing" 键下。
    timing: dict[str, JsonValue] | None = None


def canonical_json(value: object) -> str:
    return json.dumps(
        to_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_checksum(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def manifest_from_json(payload: Mapping[str, object]) -> ResearchRunManifest:
    refs = tuple(
        FrozenArtifactRef(
            artifact_id=str(item["artifact_id"]),
            version=str(item["version"]),
            checksum=str(item["checksum"]),
            capabilities=tuple(
                str(value) for value in cast(list[object], item.get("capabilities", []))
            ),
        )
        for item in cast(list[dict[str, object]], payload["dataset_releases"])
    )
    factor_refs = tuple(
        FrozenArtifactRef(
            artifact_id=str(item["artifact_id"]),
            version=str(item["version"]),
            checksum=str(item["checksum"]),
            capabilities=tuple(
                str(value) for value in cast(list[object], item.get("capabilities", []))
            ),
        )
        for item in cast(list[dict[str, object]], payload.get("factor_snapshots", []))
    )
    series_refs = tuple(
        FrozenArtifactRef(
            artifact_id=str(item["artifact_id"]),
            version=str(item["version"]),
            checksum=str(item["checksum"]),
            capabilities=tuple(
                str(value) for value in cast(list[object], item.get("capabilities", []))
            ),
        )
        for item in cast(list[dict[str, object]], payload.get("factor_series", []))
    )
    return ResearchRunManifest(
        run_id=str(payload["run_id"]),
        idempotency_key=str(payload["idempotency_key"]),
        strategy_spec=ResearchStrategySpec.model_validate(payload["strategy_spec"]),
        strategy_spec_checksum=str(payload["strategy_spec_checksum"]),
        dataset_releases=refs,
        strategy_version=(
            int(str(payload["strategy_version"]))
            if payload.get("strategy_version") is not None
            else None
        ),
        factor_snapshots=factor_refs,
        factor_series=series_refs,
        parameters=cast(dict[str, JsonValue], payload.get("parameters", {})),
        validation_config=cast(dict[str, JsonValue], payload.get("validation_config", {})),
        portfolio_config=cast(dict[str, JsonValue], payload.get("portfolio_config", {})),
        risk_config=cast(dict[str, JsonValue], payload.get("risk_config", {})),
        execution_config=cast(dict[str, JsonValue], payload.get("execution_config", {})),
        fee_config=cast(dict[str, JsonValue], payload.get("fee_config", {})),
        benchmark_config=cast(dict[str, JsonValue], payload.get("benchmark_config", {})),
        code_version=str(payload["code_version"]),
        initial_capital=Decimal(str(payload["initial_capital"])),
        requested_by=str(payload["requested_by"]),
        actor_type=ResearchActorType(str(payload["actor_type"])),
        replay_of_run_id=(
            str(payload["replay_of_run_id"])
            if payload.get("replay_of_run_id") is not None
            else None
        ),
        replay_source_status=(
            str(payload["replay_source_status"])
            if payload.get("replay_source_status") is not None
            else None
        ),
        schema_version=str(payload.get("schema_version", RESEARCH_RUN_SCHEMA_VERSION)),
    )


def report_from_json(payload: dict[str, object]) -> ResearchRunReport:
    raw_curve = payload.get("equity_curve", [])
    curve = tuple(
        EquityPoint(
            trade_date=date.fromisoformat(str(item["trade_date"])),
            equity=Decimal(str(item["equity"])),
        )
        for item in cast(list[dict[str, object]], raw_curve)
    )
    mode_raw = payload.get("execution_mode", ResearchExecutionMode.SINGLE_SHOT.value)
    benchmark_return_raw = payload.get("benchmark_return")
    # issue #184 前的历史 report 恒为数值;新 report 基准缺失时为 null。
    benchmark_return = (
        float(str(benchmark_return_raw)) if benchmark_return_raw is not None else None
    )
    return ResearchRunReport(
        strategy_kind=str(payload["strategy_kind"]),
        strategy_return=float(str(payload["strategy_return"])),
        benchmark_symbol=str(payload["benchmark_symbol"]),
        benchmark_return=benchmark_return,
        excess_return=(
            float(str(payload["excess_return"])) if benchmark_return is not None else None
        ),
        sharpe_ratio=float(str(payload["sharpe_ratio"])),
        max_drawdown=float(str(payload["max_drawdown"])),
        final_equity=Decimal(str(payload["final_equity"])),
        final_cash=Decimal(str(payload["final_cash"])),
        commission_paid=Decimal(str(payload["commission_paid"])),
        tax_paid=Decimal(str(payload["tax_paid"])),
        slippage_paid=Decimal(str(payload["slippage_paid"])),
        fill_shortfall=Decimal(str(payload["fill_shortfall"])),
        constraint_impact={
            str(key): float(value)
            for key, value in cast(dict[str, float], payload.get("constraint_impact", {})).items()
        },
        decision_count=int(str(payload["decision_count"])),
        order_count=int(str(payload["order_count"])),
        fill_count=int(str(payload["fill_count"])),
        accounting_invariants_passed=bool(payload.get("accounting_invariants_passed", True)),
        execution_mode=ResearchExecutionMode(str(mode_raw)),
        annualized_return=float(str(payload.get("annualized_return", 0.0))),
        equity_curve=curve,
        factor_screen=_optional_json_dict(payload.get("factor_screen")),
        strategy_screen=_optional_json_dict(payload.get("strategy_screen")),
        sandbox_provenance=_optional_json_dict(payload.get("sandbox_provenance")),
        # issue #262:历史 report 无该字段 → 默认 0.0(历史口径即 rf=0)。
        risk_free_annual=float(str(payload.get("risk_free_annual", 0.0))),
    )


def _optional_json_dict(value: object) -> dict[str, JsonValue] | None:
    """可选的 JSON dict 段(report 反序列化;非 dict / 空 → None)。"""
    if not isinstance(value, dict):
        return None
    return cast(dict[str, JsonValue], value)


def to_json_value(value: object) -> JsonValue:
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, ResearchStrategySpec):
        return to_json_value(value.model_dump(mode="json"))
    if is_dataclass(value):
        return to_json_value(asdict(cast(Any, value)))
    if isinstance(value, tuple | list):
        return [to_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_json_value(child) for key, child in value.items()}
    raise TypeError(f"不支持序列化的研究值: {type(value)!r}")


def _assert_unique(values: Any, label: str) -> None:
    materialized = list(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{label} 不允许重复")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 必须带时区")


__all__ = [
    "DECISION_SCHEDULE_KINDS",
    "MAX_RESEARCH_CAPITAL",
    "MIN_RESEARCH_CAPITAL",
    "REBALANCE_FREQUENCIES",
    "REPLAYABLE_SOURCE_STATUSES",
    "RESEARCH_PORTFOLIO_PIPELINE_VERSION",
    "RESEARCH_RUN_SCHEMA_VERSION",
    "CapitalTierOutcome",
    "ConstraintOutcome",
    "DecisionBundle",
    "DecisionSchedule",
    "EquityPoint",
    "FeatureValue",
    "FrozenArtifactRef",
    "JsonValue",
    "LedgerSnapshot",
    "NormalizedSignal",
    "RebalanceInstruction",
    "ResearchActorType",
    "ResearchArtifact",
    "ResearchConstraintViolationError",
    "ResearchExecutionMode",
    "ResearchFill",
    "ResearchFillAction",
    "ResearchOrder",
    "ResearchOrderStatus",
    "ResearchPipelineEvidence",
    "ResearchPosition",
    "ResearchPositionSide",
    "ResearchRiskState",
    "ResearchRunConflictError",
    "ResearchRunError",
    "ResearchRunInterruptedError",
    "ResearchRunManifest",
    "ResearchRunRecord",
    "ResearchRunReport",
    "ResearchRunStage",
    "ResearchRunStatus",
    "RiskExitOutcome",
    "TargetPosition",
    "UniverseCandidate",
    "UnsupportedResearchCapabilityError",
    "canonical_json",
    "execution_mode_for",
    "manifest_from_json",
    "parse_decision_schedule",
    "pipeline_output_checksum",
    "replay_guard_error",
    "report_from_json",
    "resolve_decision_schedule",
    "stable_checksum",
    "to_json_value",
]
