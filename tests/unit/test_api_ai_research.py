"""AI 研究助手流式 API 单元测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from finboard_api.deps import get_db_session, get_research_assistant
from finboard_api.routes import ai_research_router
from finboard_backtest.factor_research import (
    AnswerResult,
    Citation,
    CitationSource,
    FakeLLMProvider,
    LLMStreamEvent,
    ResearchAssistant,
)


class _StreamingFakeProvider(FakeLLMProvider):
    async def stream_answer(self, prompt: str) -> AsyncIterator[LLMStreamEvent]:
        yield LLMStreamEvent(kind="reasoning_delta", text="先检查研究边界。")
        async for event in super().stream_answer(prompt):
            yield event


def test_ask_stream_forwards_reasoning_and_persists_validated_answer() -> None:
    app = FastAPI()
    app.include_router(ai_research_router)

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)

    answer = AnswerResult(
        question="什么是夏普比率",
        answer="夏普比率衡量单位风险对应的超额收益。",
        citations=(
            Citation(
                source_type=CitationSource.PROJECT_DOC,
                title="研究指标说明",
            ),
        ),
    )
    assistant = ResearchAssistant(_StreamingFakeProvider(answers=[answer]))
    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_research_assistant] = lambda: assistant

    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/research/ai/ask/stream",
                json={"question": "什么是夏普比率"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: reasoning.delta" in response.text
    assert "先检查研究边界。" in response.text
    assert "event: content.delta" in response.text
    assert 'event: completed' in response.text
    assert "夏普比率衡量单位风险对应的超额收益。" in response.text
    session.commit.assert_awaited_once()
