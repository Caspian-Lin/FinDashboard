"""LLM Provider 抽象(Issue #65)。

定义 LLM provider 的最小接口。
测试使用 :class:`FakeLLMProvider`,不调用公网。

安全约束:
- Provider 的输出必须是 :class:`FactorHypothesis`,不接受可执行代码
- 提示词在传入 provider 前必须经过 :func:`sanitize_prompt` 脱敏
- 系统不提供任何连接 Broker / OrderManager / 实盘配置的路径
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from finboard_backtest.factor_research.hypothesis import FactorHypothesis
from finboard_backtest.factor_research.sanitizer import sanitize_prompt


@runtime_checkable
class LLMProvider(Protocol):
    """LLM provider 最小接口。

    所有实现(包括真实 provider)必须:
    1. 只返回 :class:`FactorHypothesis`
    2. 不返回可执行代码
    3. 不连接 Broker / OrderManager / 实盘配置
    """

    def generate_hypothesis(self, prompt: str) -> FactorHypothesis:
        """从提示词生成结构化因子假设。"""
        ...

    def provider_name(self) -> str:
        """返回 provider 标识(用于审计)。"""
        ...


class LLMUnavailableError(RuntimeError):
    """LLM provider 不可用(模型故障 / 网络 / 配额耗尽)。"""


@dataclass
class FakeLLMProvider:
    """不调用公网的测试 provider。

    按队列返回预设的 :class:`FactorHypothesis`。
    可配置 ``fail_on_call`` 模拟模型不可用。
    """

    hypotheses: list[FactorHypothesis] = field(default_factory=list)
    fail_on_call: bool = False
    _index: int = 0
    _call_count: int = 0

    def generate_hypothesis(self, prompt: str) -> FactorHypothesis:
        _ = sanitize_prompt(prompt)
        self._call_count += 1
        if self.fail_on_call:
            raise LLMUnavailableError("FakeLLMProvider: 模拟不可用")
        if self._index >= len(self.hypotheses):
            raise LLMUnavailableError(
                "FakeLLMProvider: 已耗尽预设假设"
            )
        hypothesis = self.hypotheses[self._index]
        self._index += 1
        return hypothesis

    def provider_name(self) -> str:
        return "fake"

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def remaining(self) -> int:
        return len(self.hypotheses) - self._index
