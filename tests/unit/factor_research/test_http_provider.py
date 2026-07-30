"""OpenAI 兼容 HTTP provider 测试(issue #84)。

使用 httpx.MockTransport 注入,完全不依赖公网。
覆盖正常解析、超时、限流、5xx、无效 JSON、字段缺失等故障注入。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from finboard_backtest.factor_research import LLMUnavailableError
from finboard_backtest.factor_research.http_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleLLMProvider,
)


def _config(**overrides: Any) -> OpenAICompatibleConfig:
    defaults: dict[str, Any] = {
        "base_url": "http://localhost",
        "api_key": "sk-test",
        "model": "test-model",
        "timeout_seconds": 5.0,
        "max_retries": 2,
    }
    defaults.update(overrides)
    return OpenAICompatibleConfig(**defaults)


def _provider(
    handler: Any,
    *,
    config: OpenAICompatibleConfig | None = None,
) -> OpenAICompatibleLLMProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(base_url="http://localhost", transport=transport)
    return OpenAICompatibleLLMProvider(config or _config(), client=client)


def _chat_response(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
    )


class TestNormalParsing:
    def test_hypothesis_parsed(self) -> None:
        payload = {
            "name": "动量",
            "economic_mechanism": "趋势延续",
            "input_fields": ["close", "volume"],
            "decision_timing": "close",
            "formula": "rank(ts_mean(close, 20))",
            "direction": "long_high",
            "applicable_assets": ["沪深300"],
            "expected_failure_scenarios": ["反转"],
            "parameters": [
                {"name": "window", "min_value": 5, "max_value": 20, "grid_size": 4}
            ],
            "references": [{"title": "Paper", "authors": "X", "year": 2020}],
        }

        def handler(req: httpx.Request) -> httpx.Response:
            return _chat_response(json.dumps(payload))

        provider = _provider(handler)
        try:
            h = provider.generate_hypothesis("生成动量假设")
            assert h.name == "动量"
            assert h.input_fields == ("close", "volume")
            assert h.parameter_budget == 4
        finally:
            provider.close()

    def test_answer_parsed(self) -> None:
        payload = {
            "answer": "夏普比率衡量风险调整后收益",
            "technical_detail": "mean/std",
            "citations": [
                {
                    "source_type": "project_doc",
                    "title": "metrics.py",
                    "locator": "sharpe",
                }
            ],
            "uncertainty": "low",
            "data_sufficient": True,
        }

        def handler(req: httpx.Request) -> httpx.Response:
            return _chat_response(json.dumps(payload))

        provider = _provider(handler)
        try:
            a = provider.answer_question("什么是夏普比率")
            assert "夏普" in a.answer
            assert len(a.citations) == 1
        finally:
            provider.close()

    def test_json_code_block_extracted(self) -> None:
        payload = {"answer": "x", "citations": [{"source_type": "external_reference", "title": "t"}]}

        def handler(req: httpx.Request) -> httpx.Response:
            return _chat_response("```json\n" + json.dumps(payload) + "\n```")

        provider = _provider(handler)
        try:
            a = provider.answer_question("q")
            assert a.answer == "x"
        finally:
            provider.close()

    def test_strategy_draft_parsed(self) -> None:
        payload = {
            "component_kind": "universe",
            "rationale": "蓝筹",
            "components": {"universe": {"markets": ["A"]}},
            "failure_scenarios": ["崩盘"],
        }

        def handler(req: httpx.Request) -> httpx.Response:
            return _chat_response(json.dumps(payload))

        provider = _provider(handler)
        try:
            d = provider.generate_strategy_draft("候选池")
            assert d.component_kind == "universe"
        finally:
            provider.close()

    def test_strategy_diff_parsed(self) -> None:
        payload = {
            "target_strategy_id": "s1",
            "target_version": 1,
            "summary": "调整",
            "rationale": "降低换手",
            "changes": [
                {"path": "universe.limit", "operation": "replace", "old_value": 50, "new_value": 30}
            ],
        }

        def handler(req: httpx.Request) -> httpx.Response:
            return _chat_response(json.dumps(payload))

        provider = _provider(handler)
        try:
            d = provider.generate_strategy_diff("修改")
            assert d.target_strategy_id == "s1"
            assert len(d.changes) == 1
        finally:
            provider.close()


class TestFaultInjection:
    def test_timeout_degrades_without_retry(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.TimeoutException("timeout")

        provider = _provider(handler)
        try:
            with pytest.raises(LLMUnavailableError, match="超时"):
                provider.answer_question("q")
            assert calls["n"] == 1  # 超时不重试
        finally:
            provider.close()

    def test_429_retries_then_degrades(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429, text="rate limited")

        provider = _provider(handler, config=_config(max_retries=2))
        try:
            with pytest.raises(LLMUnavailableError, match="429"):
                provider.answer_question("q")
            # 1 次初始 + 2 次重试 = 3 次
            assert calls["n"] == 3
        finally:
            provider.close()

    def test_500_retries_then_degrades(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(500, text="server error")

        provider = _provider(handler, config=_config(max_retries=1))
        try:
            with pytest.raises(LLMUnavailableError, match="500"):
                provider.answer_question("q")
            assert calls["n"] == 2
        finally:
            provider.close()

    def test_4xx_degrades_immediately(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, text="bad request")

        provider = _provider(handler)
        try:
            with pytest.raises(LLMUnavailableError, match="400"):
                provider.answer_question("q")
            assert calls["n"] == 1
        finally:
            provider.close()

    def test_invalid_json_degrades(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return _chat_response("not json at all {{{")

        provider = _provider(handler)
        try:
            with pytest.raises(LLMUnavailableError):
                provider.answer_question("q")
        finally:
            provider.close()

    def test_missing_choices_degrades(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": "no choices"})

        provider = _provider(handler)
        try:
            with pytest.raises(LLMUnavailableError, match="choices"):
                provider.answer_question("q")
        finally:
            provider.close()

    def test_answer_without_citations_degrades(self) -> None:
        payload = {"answer": "x"}  # 无 citations

        def handler(req: httpx.Request) -> httpx.Response:
            return _chat_response(json.dumps(payload))

        provider = _provider(handler)
        try:
            with pytest.raises(LLMUnavailableError, match="citations"):
                provider.answer_question("q")
        finally:
            provider.close()

    def test_retry_succeeds_after_transient_failure(self) -> None:
        calls = {"n": 0}
        payload = {"answer": "ok", "citations": [{"source_type": "project_doc", "title": "t"}]}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, text="temp")
            return _chat_response(json.dumps(payload))

        provider = _provider(handler, config=_config(max_retries=2))
        try:
            a = provider.answer_question("q")
            assert a.answer == "ok"
            assert calls["n"] == 2
        finally:
            provider.close()


class TestSanitizationInProvider:
    def test_prompt_sanitized_before_call(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            captured["messages"] = body["messages"]
            return _chat_response(
                json.dumps(
                    {"answer": "x", "citations": [{"source_type": "project_doc", "title": "t"}]}
                )
            )

        provider = _provider(handler)
        try:
            provider.answer_question("my api_key=sk-secret-abc1234567890")
            user_msg = captured["messages"][-1]["content"]
            assert "sk-secret" not in user_msg
            assert "REDACTED" in user_msg
        finally:
            provider.close()
