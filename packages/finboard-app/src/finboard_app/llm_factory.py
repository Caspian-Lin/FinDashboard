"""LLM Provider 工厂(issue #84)。

根据 :class:`Settings` 构建 LLM provider。默认 ``fake`` —— 不调用公网,
适合 CI / 离线研究。``openai_compatible`` 使用真实 HTTP provider。

安全约束:
* api_key 不进入日志 / 审计 / :class:`Provenance`;
* factory 不暴露 Broker / 账户 / 实盘配置给 provider。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from finboard_backtest.factor_research.http_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleLLMProvider,
)
from finboard_backtest.factor_research.provider import (
    FakeLLMProvider,
    LLMProvider,
)

if TYPE_CHECKING:
    from finboard_app.config import Settings


def build_llm_provider(settings: Settings) -> LLMProvider:
    """根据配置构建 LLM provider。

    * ``fake`` —— 返回 :class:`FakeLLMProvider`(不调用公网,默认);
    * ``openai_compatible`` —— 返回 :class:`OpenAICompatibleLLMProvider`
      (需要配置 ``llm_base_url`` 与 ``llm_api_key``)。
    """
    if settings.llm_provider == "openai_compatible":
        if not settings.llm_base_url or not settings.llm_api_key:
            raise ValueError(
                "openai_compatible provider 需要 FINBOARD_LLM_BASE_URL "
                "与 FINBOARD_LLM_API_KEY"
            )
        config = OpenAICompatibleConfig(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            total_timeout_seconds=settings.llm_total_timeout_seconds,
            token_idle_timeout_seconds=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            thinking_enabled=settings.llm_thinking_enabled,
            reasoning_effort=settings.llm_reasoning_effort,
        )
        return OpenAICompatibleLLMProvider(config)
    return FakeLLMProvider()


__all__ = ["build_llm_provider"]
