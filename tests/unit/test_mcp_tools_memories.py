"""``finboard.memory.*`` 工具 —— 研究记忆(mock session,无需 DB)。

验证:envelope 映射(ok/error/not_found/invalid_argument)、审计记录、
脱敏标记。业务逻辑由集成测试 ``test_research_memory_repository`` 覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.factor_research import FakeLLMProvider, ResearchAssistant
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import memories
from finboard_persistence.models import ResearchMemoryModel


def _memory_model(
    memory_id: str = "RM-1", status: str = "active"
) -> ResearchMemoryModel:
    return ResearchMemoryModel(
        memory_id=memory_id,
        memory_type="note",
        content="test memory",
        source_refs=[{"kind": "research_run", "ref_id": "RR-1"}],
        status=status,
        tags=["alpha"],
        created_by="agent:mcp",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _mock_sm(
    *,
    get_row: ResearchMemoryModel | None = None,
    list_rows: list[ResearchMemoryModel] | None = None,
) -> async_sessionmaker[AsyncSession]:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    result = MagicMock()
    # repo.list 用 ``list(result.scalars())``,故 scalars() 直接返回可迭代 list
    result.scalars.return_value = list_rows or []
    result.scalar_one_or_none.return_value = get_row
    session.execute = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return cast("async_sessionmaker[AsyncSession]", cm)


def _make_app(session_maker: async_sessionmaker[AsyncSession]) -> McpAppContext:
    provider = FakeLLMProvider()
    return McpAppContext(
        settings=Settings(),
        session_maker=session_maker,
        research_assistant=ResearchAssistant(provider),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=MagicMock(),
        provider=provider,
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


class TestRemember:
    async def test_ok(self) -> None:
        app = _make_app(_mock_sm())
        env = await memories.remember(
            app, memory_type="note", content="hello world"
        )
        assert env.status == "ok"
        assert env.data["memory_id"].startswith("RM-")
        assert env.data["status"] == "active"

    async def test_invalid_type(self) -> None:
        app = _make_app(_mock_sm())
        env = await memories.remember(app, memory_type="bad", content="x")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_records_audit(self) -> None:
        app = _make_app(_mock_sm())
        await memories.remember(app, memory_type="note", content="x")
        assert len(app.audit.records) == 1
        assert app.audit.records[0].tool_name == "finboard.memory.remember"

    async def test_content_is_sensitive(self) -> None:
        """``content`` 应被标记为敏感(审计摘要脱敏)。"""
        app = _make_app(_mock_sm())
        await memories.remember(
            app, memory_type="note", content="secret-api-key-12345"
        )
        summary = app.audit.records[0].arguments_summary
        assert "secret-api-key-12345" not in summary


class TestGet:
    async def test_not_found(self) -> None:
        app = _make_app(_mock_sm(get_row=None))
        env = await memories.get_memory(app, "RM-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"


class TestList:
    async def test_ok(self) -> None:
        app = _make_app(_mock_sm(list_rows=[_memory_model()]))
        env = await memories.list_memories(app)
        assert env.status == "ok"
        assert isinstance(env.data, list)
        assert env.data[0]["memory_id"] == "RM-1"

    async def test_empty(self) -> None:
        app = _make_app(_mock_sm(list_rows=[]))
        env = await memories.list_memories(app)
        assert env.status == "ok"
        assert env.data == []


class TestForget:
    async def test_not_found(self) -> None:
        app = _make_app(_mock_sm(get_row=None))
        env = await memories.forget(app, "RM-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_not_active(self) -> None:
        app = _make_app(_mock_sm(get_row=_memory_model(status="forgotten")))
        env = await memories.forget(app, "RM-1")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
