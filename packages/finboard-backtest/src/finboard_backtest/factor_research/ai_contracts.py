"""AI 研究助手输出契约(Issue #84)。

AI 助手只服务研究与教育。所有输出必须满足红线:

* 不含可执行代码 / 模块路径 / 任意表达式 —— 受 #79 schema 和白名单约束
* 显示来源(provider / model / prompt 版本)、不确定性、审批状态
* 金融问答引用项目文档 / 数据字典 / 策略版本 / 研究结果来源
* 无法确认或数据不足时明确说明,不编造市场数据 / 收益 / 监管结论
* AI 永远不位于订单执行链路中,不连接实盘账户 / 订单 / 持仓 / Kill Switch

详见 :mod:`finboard_backtest.factor_research`。
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from finboard_backtest.factor_research.hypothesis import (
    FactorHypothesis,
    Reference,
)
from finboard_backtest.strategy_spec.contracts import reject_executable_payload


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_draft_id() -> str:
    return f"ai-draft-{uuid.uuid4().hex[:16]}"


class DraftStatus(StrEnum):
    """AI 草案审批状态(单向流)。

    ::

        proposed ──approve──▶ approved ──consume──▶ consumed
           │                     │
           │                  reject
           ▼                     ▼
        rejected             rejected

    草案审批后才可被采纳到正式研究流水线(创建 #79 spec / 登记 #65 假设)。
    AI 不能自动审批或采纳自己的草案。
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"


_VALID_DRAFT_TRANSITIONS: dict[DraftStatus, frozenset[DraftStatus]] = {
    DraftStatus.PROPOSED: frozenset({DraftStatus.APPROVED, DraftStatus.REJECTED}),
    DraftStatus.APPROVED: frozenset({DraftStatus.CONSUMED, DraftStatus.REJECTED}),
    DraftStatus.REJECTED: frozenset(),
    DraftStatus.CONSUMED: frozenset(),
}


class DraftKind(StrEnum):
    """AI 草案种类。"""

    HYPOTHESIS = "hypothesis"
    STRATEGY_COMPONENT = "strategy_component"
    STRATEGY_DIFF = "strategy_diff"
    ANSWER = "answer"


class UncertaintyLevel(StrEnum):
    """AI 回答的不确定性等级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CitationSource(StrEnum):
    """引用来源类型。"""

    PROJECT_DOC = "project_doc"
    DATA_DICTIONARY = "data_dictionary"
    STRATEGY_SPEC = "strategy_spec"
    RESEARCH_RUN = "research_run"
    FACTOR_DEFINITION = "factor_definition"
    VALIDATION_EXPERIMENT = "validation_experiment"
    EXTERNAL_REFERENCE = "external_reference"


@dataclass(frozen=True, slots=True)
class Provenance:
    """AI 输出来源元数据(用于审计与可复现)。

    所有 AI 草案 / 回答都必须携带 provenance,持久化后不可篡改。
    """

    provider: str
    model_version: str
    prompt_version: str
    generated_at: datetime = field(default_factory=_utcnow)
    latency_ms: int = 0
    request_checksum: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "generated_at": self.generated_at.isoformat(),
            "latency_ms": self.latency_ms,
            "request_checksum": self.request_checksum,
        }


@dataclass(frozen=True, slots=True)
class Citation:
    """引用来源。

    AI 回答必须引用项目内可定位的来源(文档 / 数据字典 / 策略版本 / 研究 run),
    或明确标注为外部参考文献。不允许无来源的断言。
    """

    source_type: CitationSource
    title: str
    locator: str = ""
    snippet: str = ""

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Citation.title 不能为空")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "title": self.title,
            "locator": self.locator,
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """金融问答结果。

    设计原则(红线):
    * ``answer`` 是通俗解释(面向金融知识有限的用户);
    * ``technical_detail`` 是可选的技术细节;
    * ``citations`` 必须非空(引用项目来源或标注外部参考);
    * ``data_sufficient=False`` 时必须给出 ``disclaimer``,明确说明数据不足,
      不编造市场数据 / 收益 / 监管结论;
    * ``uncertainty`` 反映 AI 对自身回答的置信度。
    """

    answer: str
    citations: tuple[Citation, ...]
    uncertainty: UncertaintyLevel = UncertaintyLevel.MEDIUM
    technical_detail: str = ""
    data_sufficient: bool = True
    disclaimer: str = ""
    question: str = ""

    def __post_init__(self) -> None:
        if not self.answer.strip():
            raise ValueError("AnswerResult.answer 不能为空")
        if len(self.citations) == 0:
            raise ValueError(
                "AnswerResult.citations 不能为空 —— AI 回答必须引用来源"
            )
        if not self.data_sufficient and not self.disclaimer.strip():
            raise ValueError(
                "data_sufficient=False 时必须给出 disclaimer,说明数据不足"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "technical_detail": self.technical_detail,
            "citations": [c.as_dict() for c in self.citations],
            "uncertainty": self.uncertainty.value,
            "data_sufficient": self.data_sufficient,
            "disclaimer": self.disclaimer,
        }


# 策略草案允许的组件键(对应 #79 ResearchStrategySpec 的子部分)。
# AI 只能生成这些组件的草案,不能生成 Python / 模块路径 / 可执行表达式。
ALLOWED_STRATEGY_COMPONENTS: frozenset[str] = frozenset({
    "universe",
    "feature_graph",
    "signal_rules",
    "portfolio_policy",
    "risk_exit_policy",
    "execution_model",
    "validation_plan",
})


@dataclass(frozen=True, slots=True)
class StrategyDraftPayload:
    """策略组件草案(基于 #79 schema 的受限填充)。

    ``components`` 是受白名单约束的结构化片段(dict),持久化前必须经过
    :func:`reject_executable_payload` 校验,采纳时由用户通过 #79 API
    组装成正式 :class:`ResearchStrategySpec`。

    AI 不能直接产出可执行的完整策略 spec —— 草案必须经人工审批后
    才能进入正式研究流水线。

    红线:
    * ``components`` 只允许 :data:`ALLOWED_STRATEGY_COMPONENTS` 中的键;
    * 不含 Python / 模块路径 / 可执行表达式;
    * 必须给出失效场景与数据需求,帮助用户评估。
    """

    component_kind: str
    rationale: str
    components: dict[str, Any]
    failure_scenarios: tuple[str, ...] = ()
    data_requirements: tuple[str, ...] = ()
    risk_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.component_kind not in ALLOWED_STRATEGY_COMPONENTS:
            raise ValueError(
                f"component_kind '{self.component_kind}' 不在允许列表: "
                f"{sorted(ALLOWED_STRATEGY_COMPONENTS)}"
            )
        if not self.rationale.strip():
            raise ValueError("StrategyDraftPayload.rationale 不能为空")
        unknown_keys = set(self.components.keys()) - ALLOWED_STRATEGY_COMPONENTS
        if unknown_keys:
            raise ValueError(
                f"components 包含不允许的键: {sorted(unknown_keys)}"
            )
        # 纵深防御:草案 payload 必须通过可执行载荷拦截。
        reject_executable_payload(self.components)

    def as_dict(self) -> dict[str, Any]:
        return {
            "component_kind": self.component_kind,
            "rationale": self.rationale,
            "components": self.components,
            "failure_scenarios": list(self.failure_scenarios),
            "data_requirements": list(self.data_requirements),
            "risk_notes": list(self.risk_notes),
        }


@dataclass(frozen=True, slots=True)
class StrategyDiffItem:
    """单条结构化策略变更。"""

    path: str
    operation: str  # add | remove | replace
    old_value: Any = None
    new_value: Any = None
    explanation: str = ""

    def __post_init__(self) -> None:
        if self.operation not in ("add", "remove", "replace"):
            raise ValueError(
                f"StrategyDiffItem.operation 必须是 add/remove/replace, "
                f"得到 '{self.operation}'"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "operation": self.operation,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "explanation": self.explanation,
        }


@dataclass(frozen=True, slots=True)
class StrategyDiffPayload:
    """策略修改 diff 草案。

    AI 建议修改现有策略时提供结构化 diff + 理由 + 假设 + 失效场景 +
    数据需求 + 风险提示。用户审批前不得发布策略或启动实验。

    ``changes`` 是 :class:`StrategyDiffItem` 列表(受白名单约束的路径),
    不含可执行代码。
    """

    target_strategy_id: str
    target_version: int
    summary: str
    rationale: str
    changes: tuple[StrategyDiffItem, ...]
    assumptions: tuple[str, ...] = ()
    failure_scenarios: tuple[str, ...] = ()
    data_requirements: tuple[str, ...] = ()
    risk_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.target_strategy_id.strip():
            raise ValueError("StrategyDiffPayload.target_strategy_id 不能为空")
        if self.target_version < 1:
            raise ValueError("target_version 必须 >= 1")
        if not self.summary.strip():
            raise ValueError("StrategyDiffPayload.summary 不能为空")
        if not self.rationale.strip():
            raise ValueError("StrategyDiffPayload.rationale 不能为空")
        if len(self.changes) == 0:
            raise ValueError("StrategyDiffPayload.changes 不能为空")
        # 纵深防御:diff 的值不能携带可执行载荷。
        for item in self.changes:
            reject_executable_payload({"v": item.new_value})
            reject_executable_payload({"v": item.old_value})

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_strategy_id": self.target_strategy_id,
            "target_version": self.target_version,
            "summary": self.summary,
            "rationale": self.rationale,
            "changes": [c.as_dict() for c in self.changes],
            "assumptions": list(self.assumptions),
            "failure_scenarios": list(self.failure_scenarios),
            "data_requirements": list(self.data_requirements),
            "risk_notes": list(self.risk_notes),
        }


@dataclass(frozen=True, slots=True)
class DraftArtifact:
    """持久化的 AI 草案 / 回答(不可变,审批状态可流转)。

    所有 AI 建议必须以草案或假设形式持久化,显示来源、模型/提示词版本、
    不确定性和审批状态,由用户明确审批并通过正式研究流水线验证。

    Attributes:
        draft_id: 草案唯一 ID(``ai-draft-<hex>``)。
        kind: 草案种类(假设 / 策略组件 / 策略 diff / 问答)。
        provenance: 来源元数据(不可篡改)。
        status: 审批状态(可流转)。
        payload: 草案正文(JSON 可序列化)。
        uncertainty: 不确定性等级。
        references: 外部参考文献(可选)。
        approved_by / approved_at: 审批人与时间(审批后填充)。
        rejection_reason: 拒绝原因(拒绝后填充)。
        consumed_ref: 采纳后指向的正式产物 ID(如 spec version / hypothesis id)。
    """

    draft_id: str
    kind: DraftKind
    provenance: Provenance
    status: DraftStatus
    payload: dict[str, Any]
    uncertainty: UncertaintyLevel = UncertaintyLevel.MEDIUM
    references: tuple[Reference, ...] = ()
    created_at: datetime = field(default_factory=_utcnow)
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    consumed_ref: str | None = None

    def with_status(self, new_status: DraftStatus, **kwargs: Any) -> DraftArtifact:
        """返回带新审批状态的不可变副本。

        Raises:
            ValueError: 非法状态转换。
        """
        allowed = _VALID_DRAFT_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise ValueError(
                f"非法草案状态转换: {self.status.value} -> {new_status.value}. "
                f"合法目标: {[s.value for s in allowed] or '(终态)'}"
            )
        return dataclasses.replace(self, status=new_status, **kwargs)

    @property
    def is_terminal(self) -> bool:
        return self.status in (DraftStatus.REJECTED, DraftStatus.CONSUMED)

    @property
    def is_approved(self) -> bool:
        return self.status in (DraftStatus.APPROVED, DraftStatus.CONSUMED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "kind": self.kind.value,
            "provenance": self.provenance.as_dict(),
            "status": self.status.value,
            "payload": self.payload,
            "uncertainty": self.uncertainty.value,
            "references": [
                {
                    "title": r.title,
                    "authors": r.authors,
                    "year": r.year,
                    "url": r.url,
                    "doi": r.doi,
                }
                for r in self.references
            ],
            "created_at": self.created_at.isoformat(),
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "rejection_reason": self.rejection_reason,
            "consumed_ref": self.consumed_ref,
        }


def hypothesis_to_draft_payload(hypothesis: FactorHypothesis) -> dict[str, Any]:
    """把 :class:`FactorHypothesis` 序列化为草案 payload。"""
    return {
        "name": hypothesis.name,
        "economic_mechanism": hypothesis.economic_mechanism,
        "input_fields": list(hypothesis.input_fields),
        "decision_timing": hypothesis.decision_timing,
        "formula": hypothesis.formula,
        "direction": hypothesis.direction,
        "applicable_assets": list(hypothesis.applicable_assets),
        "expected_failure_scenarios": list(hypothesis.expected_failure_scenarios),
        "parameters": [
            {
                "name": p.name,
                "min_value": p.min_value,
                "max_value": p.max_value,
                "grid_size": p.grid_size,
            }
            for p in hypothesis.parameters
        ],
    }


__all__ = [
    "ALLOWED_STRATEGY_COMPONENTS",
    "AnswerResult",
    "Citation",
    "CitationSource",
    "DraftArtifact",
    "DraftKind",
    "DraftStatus",
    "Provenance",
    "StrategyDiffItem",
    "StrategyDiffPayload",
    "StrategyDraftPayload",
    "UncertaintyLevel",
    "hypothesis_to_draft_payload",
]
