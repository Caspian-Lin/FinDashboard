"""LLM Provider 抽象测试。"""

from __future__ import annotations

import pytest

from finboard_backtest.factor_research import (
    FactorHypothesis,
    FakeLLMProvider,
    LLMProvider,
    LLMUnavailableError,
)


def _make_hypothesis(name: str = "test") -> FactorHypothesis:
    return FactorHypothesis(
        name=name,
        economic_mechanism="x",
        input_fields=("close",),
        decision_timing="close",
        formula="rank(close)",
        direction="long_high",
    )


class TestFakeLLMProvider:
    def test_returns_hypotheses_in_order(self) -> None:
        h1 = _make_hypothesis("first")
        h2 = _make_hypothesis("second")
        provider = FakeLLMProvider(hypotheses=[h1, h2])
        assert provider.generate_hypothesis("prompt").name == "first"
        assert provider.generate_hypothesis("prompt").name == "second"

    def test_provider_name(self) -> None:
        provider = FakeLLMProvider()
        assert provider.provider_name() == "fake"

    def test_exhausted_raises(self) -> None:
        provider = FakeLLMProvider(hypotheses=[_make_hypothesis()])
        provider.generate_hypothesis("prompt")
        with pytest.raises(LLMUnavailableError):
            provider.generate_hypothesis("prompt")

    def test_fail_on_call(self) -> None:
        provider = FakeLLMProvider(fail_on_call=True)
        with pytest.raises(LLMUnavailableError, match="不可用"):
            provider.generate_hypothesis("prompt")

    def test_call_count(self) -> None:
        provider = FakeLLMProvider(hypotheses=[_make_hypothesis()])
        assert provider.call_count == 0
        provider.generate_hypothesis("prompt")
        assert provider.call_count == 1

    def test_remaining(self) -> None:
        provider = FakeLLMProvider(hypotheses=[_make_hypothesis(), _make_hypothesis()])
        assert provider.remaining == 2
        provider.generate_hypothesis("prompt")
        assert provider.remaining == 1

    def test_empty_provider_raises(self) -> None:
        provider = FakeLLMProvider()
        with pytest.raises(LLMUnavailableError):
            provider.generate_hypothesis("prompt")


class TestProtocolConformance:
    def test_fake_provider_is_llm_provider(self) -> None:
        provider: FakeLLMProvider = FakeLLMProvider()
        assert isinstance(provider, LLMProvider)
