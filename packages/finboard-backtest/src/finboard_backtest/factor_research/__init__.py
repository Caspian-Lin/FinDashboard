"""因子研究辅助模块。

Issue #160 起 AI 研究助手(``ResearchAssistant`` / LLM provider / 假设审批
工作流)整体退役,AI 能力全面迁移到 OpenCode 研究运行时;FinBoard 自身不再
有任何 LLM 调用。本包仅保留与 LLM 无关的工具:

* :mod:`finboard_backtest.factor_research.sanitizer` —— 提示词脱敏,
  MCP 入参统一脱敏仍在使用。
"""

from finboard_backtest.factor_research.sanitizer import (
    contains_sensitive_data,
    sanitize_prompt,
)

FACTOR_RESEARCH_VERSION = "v1"

__all__ = [
    "contains_sensitive_data",
    "sanitize_prompt",
]
