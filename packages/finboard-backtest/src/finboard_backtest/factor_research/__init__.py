"""LLM 辅助因子假设登记与离线审阅流程(Issue #65 / #84)。

受控、可审计的 LLM 辅助研究循环:

1. LLM 只输出受白名单 / #79 schema 约束的结构化对象(不含可执行代码)
2. 公式必须映射到白名单字段与算子;策略草案受组件白名单约束
3. 人工批准是进入实验的显式 gate
4. 候选状态: proposed -> approved -> in_sample -> validated_oos / rejected
5. AI 建议以草案 / 假设形式持久化,显示来源、模型/提示词版本、不确定性和审批状态
6. validated_oos 只能由 #57 持久化的机器验证实验决定(不能由 LLM/人工判断)
7. 系统不连接 Broker / OrderManager / 实盘策略配置 / 动态代码执行
8. AI 提供上下文式金融问答,引用项目来源,数据不足时明确说明

详见 :mod:`finboard_backtest.factor_research.hypothesis` 与
:mod:`finboard_backtest.factor_research.ai_contracts`。
"""

from finboard_backtest.factor_research.ai_contracts import (
    ALLOWED_STRATEGY_COMPONENTS,
    AnswerResult,
    Citation,
    CitationSource,
    DraftArtifact,
    DraftKind,
    DraftStatus,
    Provenance,
    StrategyDiffItem,
    StrategyDiffPayload,
    StrategyDraftPayload,
    UncertaintyLevel,
    hypothesis_to_draft_payload,
)
from finboard_backtest.factor_research.assistant import (
    AIDegradedError,
    AIResponse,
    PermissionDeniedError,
    ResearchAssistant,
    assert_research_only_request,
)
from finboard_backtest.factor_research.audit import (
    AuditEntry,
    AuditEventType,
    AuditTrail,
)
from finboard_backtest.factor_research.http_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleLLMProvider,
)
from finboard_backtest.factor_research.hypothesis import (
    FactorHypothesis,
    HypothesisStatus,
    ParameterSpec,
    Reference,
)
from finboard_backtest.factor_research.provider import (
    PROMPT_VERSION,
    FakeLLMProvider,
    LLMProvider,
    LLMUnavailableError,
)
from finboard_backtest.factor_research.sanitizer import (
    contains_sensitive_data,
    sanitize_prompt,
)
from finboard_backtest.factor_research.whitelist import (
    ALLOWED_FIELDS,
    ALLOWED_OPERATORS,
    HypothesisValidationResult,
    HypothesisValidator,
)
from finboard_backtest.factor_research.workflow import (
    ExperimentRegistration,
    MachineValidationOutcome,
    ResearchWorkflow,
    WorkflowError,
)

FACTOR_RESEARCH_VERSION = "v1"

__all__ = [
    "ALLOWED_FIELDS",
    "ALLOWED_OPERATORS",
    "ALLOWED_STRATEGY_COMPONENTS",
    "PROMPT_VERSION",
    "AIDegradedError",
    "AIResponse",
    "AnswerResult",
    "AuditEntry",
    "AuditEventType",
    "AuditTrail",
    "Citation",
    "CitationSource",
    "DraftArtifact",
    "DraftKind",
    "DraftStatus",
    "ExperimentRegistration",
    "FactorHypothesis",
    "FakeLLMProvider",
    "HypothesisStatus",
    "HypothesisValidationResult",
    "HypothesisValidator",
    "LLMProvider",
    "LLMUnavailableError",
    "MachineValidationOutcome",
    "OpenAICompatibleConfig",
    "OpenAICompatibleLLMProvider",
    "ParameterSpec",
    "PermissionDeniedError",
    "Provenance",
    "Reference",
    "ResearchAssistant",
    "ResearchWorkflow",
    "StrategyDiffItem",
    "StrategyDiffPayload",
    "StrategyDraftPayload",
    "UncertaintyLevel",
    "WorkflowError",
    "assert_research_only_request",
    "contains_sensitive_data",
    "hypothesis_to_draft_payload",
    "sanitize_prompt",
]
