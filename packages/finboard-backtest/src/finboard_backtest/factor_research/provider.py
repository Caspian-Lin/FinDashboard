"""LLM Provider 抽象(Issue #65 / #84)。

定义 LLM provider 的统一接口。所有实现(包括真实 HTTP provider)必须:

1. 只返回受白名单 / schema 约束的结构化对象,不接受可执行代码
2. 提示词在传入 provider 前必须经过 :func:`sanitize_prompt` 脱敏
3. 不连接 Broker / OrderManager / 实盘配置
4. 超时 / 限流 / 不可用时降级为 :class:`LLMUnavailableError`

测试使用 :class:`FakeLLMProvider`,不调用公网。
真实 HTTP provider 见 :mod:`finboard_backtest.factor_research.http_provider`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from finboard_backtest.factor_research.ai_contracts import (
    AnswerResult,
    StrategyDiffPayload,
    StrategyDraftPayload,
)
from finboard_backtest.factor_research.hypothesis import FactorHypothesis
from finboard_backtest.factor_research.sanitizer import sanitize_prompt

# 提示词模板版本(随系统提示词变更递增,用于审计与可复现)。
PROMPT_VERSION = "ai-assistant-v1"


@runtime_checkable
class LLMProvider(Protocol):
    """LLM provider 统一接口(Issue #84)。

    所有实现必须:
    1. 只返回受白名单 / schema 约束的结构化对象
    2. 不返回可执行代码 / 模块路径 / 任意表达式
    3. 不连接 Broker / OrderManager / 实盘配置
    4. 报告 model / prompt 版本(用于 :class:`Provenance`)

    AI 永远不位于订单执行链路中。
    """

    def generate_hypothesis(self, prompt: str) -> FactorHypothesis:
        """从提示词生成结构化因子假设(受白名单约束)。"""
        ...

    def generate_strategy_draft(self, prompt: str) -> StrategyDraftPayload:
        """生成策略组件草案(受 #79 schema 与白名单约束)。"""
        ...

    def generate_strategy_diff(self, prompt: str) -> StrategyDiffPayload:
        """生成策略修改 diff 草案。"""
        ...

    def answer_question(self, prompt: str) -> AnswerResult:
        """回答金融研究问题(通俗解释 + 引用 + 不确定性)。"""
        ...

    def provider_name(self) -> str:
        """返回 provider 标识(用于审计)。"""
        ...

    def model_version(self) -> str:
        """返回底层模型版本标识(用于 :class:`Provenance`)。"""
        ...

    def prompt_version(self) -> str:
        """返回提示词模板版本(用于 :class:`Provenance`)。"""
        ...


class LLMUnavailableError(RuntimeError):
    """LLM provider 不可用(模型故障 / 网络 / 配额耗尽 / 限流耗尽重试)。

    调用方应降级处理,不得自动重试下单类操作或错误晋级研究状态。
    """


@dataclass
class FakeLLMProvider:
    """不调用公网的测试 provider。

    按队列返回预设的结构化输出。可配置 ``fail_on_call`` 模拟模型不可用,
    或用 ``fail_on`` 针对特定方法注入故障(用于故障注入测试)。
    """

    hypotheses: list[FactorHypothesis] = field(default_factory=list)
    fail_on_call: bool = False
    strategy_drafts: list[StrategyDraftPayload] = field(default_factory=list)
    strategy_diffs: list[StrategyDiffPayload] = field(default_factory=list)
    answers: list[AnswerResult] = field(default_factory=list)
    fail_on: tuple[str, ...] = ()
    _model_version: str = "fake-model-v0"
    _index_h: int = 0
    _index_d: int = 0
    _index_diff: int = 0
    _index_a: int = 0
    _call_log: list[str] = field(default_factory=list)

    def _check_fail(self, method: str) -> None:
        self._call_log.append(method)
        if self.fail_on_call or method in self.fail_on:
            raise LLMUnavailableError(
                f"FakeLLMProvider: {method} 模拟不可用"
            )

    def generate_hypothesis(self, prompt: str) -> FactorHypothesis:
        _ = sanitize_prompt(prompt)
        self._check_fail("generate_hypothesis")
        if self._index_h >= len(self.hypotheses):
            raise LLMUnavailableError("FakeLLMProvider: 已耗尽预设假设")
        hypothesis = self.hypotheses[self._index_h]
        self._index_h += 1
        return hypothesis

    def generate_strategy_draft(self, prompt: str) -> StrategyDraftPayload:
        _ = sanitize_prompt(prompt)
        self._check_fail("generate_strategy_draft")
        if self._index_d >= len(self.strategy_drafts):
            raise LLMUnavailableError("FakeLLMProvider: 已耗尽预设策略草案")
        draft = self.strategy_drafts[self._index_d]
        self._index_d += 1
        return draft

    def generate_strategy_diff(self, prompt: str) -> StrategyDiffPayload:
        _ = sanitize_prompt(prompt)
        self._check_fail("generate_strategy_diff")
        if self._index_diff >= len(self.strategy_diffs):
            raise LLMUnavailableError("FakeLLMProvider: 已耗尽预设策略 diff")
        diff = self.strategy_diffs[self._index_diff]
        self._index_diff += 1
        return diff

    def answer_question(self, prompt: str) -> AnswerResult:
        _ = sanitize_prompt(prompt)
        self._check_fail("answer_question")
        if self._index_a >= len(self.answers):
            raise LLMUnavailableError("FakeLLMProvider: 已耗尽预设回答")
        answer = self.answers[self._index_a]
        self._index_a += 1
        return answer

    def provider_name(self) -> str:
        return "fake"

    def model_version(self) -> str:
        return self._model_version

    def prompt_version(self) -> str:
        return PROMPT_VERSION

    @property
    def call_count(self) -> int:
        return len(self._call_log)

    @property
    def call_log(self) -> list[str]:
        return list(self._call_log)

    @property
    def remaining(self) -> int:
        return len(self.hypotheses) - self._index_h


__all__ = [
    "PROMPT_VERSION",
    "FakeLLMProvider",
    "LLMProvider",
    "LLMUnavailableError",
]
