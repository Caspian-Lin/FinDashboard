"""LLM 辅助因子假设登记与离线审阅流程(Issue #65)。

受控、可审计的 LLM 辅助研究循环:
1. LLM 只输出结构化 :class:`FactorHypothesis`(不含可执行代码)
2. 公式必须映射到白名单字段与算子
3. 人工批准是进入实验的显式 gate
4. 候选状态: proposed -> approved -> in_sample -> validated_oos / rejected
5. 系统不连接 Broker / OrderManager / 实盘策略配置

详见 :mod:`finboard_backtest.factor_research.hypothesis`。
"""

from finboard_backtest.factor_research.audit import (
    AuditEntry,
    AuditEventType,
    AuditTrail,
)
from finboard_backtest.factor_research.hypothesis import (
    FactorHypothesis,
    HypothesisStatus,
    ParameterSpec,
    Reference,
)
from finboard_backtest.factor_research.provider import (
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
    ResearchWorkflow,
    WorkflowError,
)

FACTOR_RESEARCH_VERSION = "v1"

__all__ = [
    "ALLOWED_FIELDS",
    "ALLOWED_OPERATORS",
    "FACTOR_RESEARCH_VERSION",
    "AuditEntry",
    "AuditEventType",
    "AuditTrail",
    "ExperimentRegistration",
    "FactorHypothesis",
    "FakeLLMProvider",
    "HypothesisStatus",
    "HypothesisValidationResult",
    "HypothesisValidator",
    "LLMProvider",
    "LLMUnavailableError",
    "ParameterSpec",
    "Reference",
    "ResearchWorkflow",
    "WorkflowError",
    "contains_sensitive_data",
    "sanitize_prompt",
]
