"""AI 研究助手门面与权限矩阵(Issue #84)。

:class:`ResearchAssistant` 是 AI 助手的唯一入口,负责:

* **权限边界**:只允许研究/教育类请求,拒绝任何试图调用实盘账户 / 订单 /
  撤单 / 持仓校准 / Kill Switch / 启动回测或模拟会话的越权请求
* **脱敏**:提示词进入 provider 前强制 :func:`sanitize_prompt`
* **来源组装**:为每次 AI 输出生成不可篡改的 :class:`Provenance`
* **降级**:provider 不可用时抛 :class:`LLMUnavailableError`,由调用方记录审计

权限矩阵(红线):

================  =======  =======  =======  ========
能力              AI 可读  AI 可写  审批门    说明
================  =======  =======  =======  ========
研究上下文        是       否       -        只读
因子假设          -       草案      人工     需审批后登记
策略规格          -       草案/diff 人工     需审批后通过 #79 API 正式化
金融问答          -       解释      -        引用来源,不编造
实盘账户          否       否       -        完全禁止
订单 / 撤单       否       否       -        完全禁止
持仓校准          否       否       -        完全禁止
Kill Switch       否       否       -        完全禁止
启动回测/模拟     否       否       -        AI 不能直接启动运行
================  =======  =======  =======  ========

AI 永远不位于订单执行链路中。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

from finboard_backtest.factor_research.ai_contracts import (
    AnswerResult,
    Provenance,
    StrategyDiffPayload,
    StrategyDraftPayload,
)
from finboard_backtest.factor_research.hypothesis import FactorHypothesis
from finboard_backtest.factor_research.provider import LLMProvider
from finboard_backtest.factor_research.sanitizer import sanitize_prompt


class PermissionDeniedError(RuntimeError):
    """请求越权 —— 试图让 AI 调用实盘能力或绕过研究边界。"""


class AIDegradedError(RuntimeError):
    """AI 助手降级(provider 不可用 / 输出非法)。"""


# 权限边界:出现这些意图的请求一律拒绝(纵深防御,与白名单互补)。
# 注意:中文关键词不加 ``\b``(中文字符属于 ``\w``,``\b`` 在中文间不生效)。
_BOUNDARY_VIOLATIONS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:下单|发单|买入|卖出|市价单|限价单|委托下单)"),
    re.compile(r"(?:撤单|撤销订单|撤销委托|撤销|撤掉)"),
    re.compile(r"(?:修改持仓|校准持仓|调整仓位|调整持仓|手动改仓|改仓|改持仓|满仓|清仓|空仓)"),
    re.compile(r"kill[\s_]*switch", re.IGNORECASE),
    re.compile(r"(?:熔断|紧急停止|紧急刹车|一键停止交易)"),
    re.compile(r"(?:实盘账户|真实账户|券商账户|交易账户资金)"),
    re.compile(r"(?:启动回测|启动模拟|运行回测|开始模拟交易|启动研究运行|运行模拟交易)"),
    re.compile(r"(?:api[_\-\s]?key|密钥|(?:账户|登录|交易)?密码|token|credential)", re.IGNORECASE),
    re.compile(r"(?:连接\s*broker|调用\s*qmt|调用\s*ctp)", re.IGNORECASE),
    re.compile(r"(?:写入\s*订单|写入\s*成交|写入\s*持仓|插入订单)"),
    re.compile(r"(?:自动审批|跳过审批|绕过审批|免审批)"),
)

# 提示词注入检测:试图覆盖系统提示或重置角色。
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior)\s+", re.IGNORECASE),
    re.compile(r"forget\s+(?:all\s+)?(?:previous|prior|above)\s+", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a|an)\s+", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"忘记(?:之前|上面|先前)的(?:指令|提示|规则)"),
    re.compile(r"忽略(?:之前|上面|先前)的(?:指令|提示|规则)"),
    re.compile(r"你现在是"),
)


def assert_research_only_request(prompt: str) -> None:
    """校验请求是否在研究/教育边界内。

    Raises:
        PermissionDeniedError: 请求包含越权意图或提示词注入。
    """
    for pattern in _BOUNDARY_VIOLATIONS:
        match = pattern.search(prompt)
        if match:
            raise PermissionDeniedError(
                f"请求越权,包含实盘/下单/凭证等禁止意图: '{match.group().strip()}'"
            )
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(prompt)
        if match:
            raise PermissionDeniedError(
                f"检测到提示词注入企图: '{match.group().strip()}'"
            )


def _request_checksum(prompt: str) -> str:
    sanitized = sanitize_prompt(prompt)
    return hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class AIResponse[T]:
    """AI 助手响应(领域对象 + 来源元数据)。"""

    result: T
    provenance: Provenance

    def as_tuple(self) -> tuple[T, Provenance]:
        return self.result, self.provenance


class ResearchAssistant:
    """AI 研究助手门面(权限边界 + 脱敏 + 来源组装 + 降级)。

    用法::

        assistant = ResearchAssistant(provider)
        response = assistant.propose_hypothesis("提出动量因子假设")
        hypothesis, provenance = response.as_tuple()
        # 持久化为 DraftArtifact(provenance 不可篡改)

    所有方法在调用前都会:
    1. :func:`assert_research_only_request` 拒绝越权 / 注入
    2. :func:`sanitize_prompt` 脱敏
    3. 计时并组装 :class:`Provenance`

    provider 不可用时抛 :class:`AIDegradedError`(调用方记录审计并降级)。
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    def propose_hypothesis(self, prompt: str) -> AIResponse[FactorHypothesis]:
        return self._call(
            prompt, self._provider.generate_hypothesis, "generate_hypothesis"
        )

    def propose_strategy_draft(
        self, prompt: str
    ) -> AIResponse[StrategyDraftPayload]:
        return self._call(
            prompt, self._provider.generate_strategy_draft, "generate_strategy_draft"
        )

    def propose_strategy_diff(
        self, prompt: str
    ) -> AIResponse[StrategyDiffPayload]:
        return self._call(
            prompt, self._provider.generate_strategy_diff, "generate_strategy_diff"
        )

    def ask(self, prompt: str) -> AIResponse[AnswerResult]:
        return self._call(
            prompt, self._provider.answer_question, "answer_question"
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _call(
        self,
        prompt: str,
        handler: Any,
        method_name: str,
    ) -> AIResponse[Any]:
        assert_research_only_request(prompt)
        _ = sanitize_prompt(prompt)  # 防御性二次脱敏(provider 内部也会做)
        started = time.monotonic()
        try:
            result = handler(prompt)
        except Exception as exc:
            raise AIDegradedError(
                f"AI 助手 {method_name} 不可用: {exc}"
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        provenance = Provenance(
            provider=self._provider.provider_name(),
            model_version=self._provider.model_version(),
            prompt_version=self._provider.prompt_version(),
            latency_ms=latency_ms,
            request_checksum=_request_checksum(prompt),
        )
        return AIResponse(result=result, provenance=provenance)


__all__ = [
    "AIDegradedError",
    "AIResponse",
    "PermissionDeniedError",
    "ResearchAssistant",
    "assert_research_only_request",
]
