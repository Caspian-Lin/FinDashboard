"""``finboard.run.*`` 工具 —— ResearchRun 只读查询(mock session,无需 DB)。

用 ``AsyncMock`` 模拟 ``AsyncSession``,验证:

* ``list_runs`` 返回摘要列表(覆盖 ``_run_summary``);
* ``get_run`` 返回详情(覆盖 ``_run_detail``);
* 不存在的 run 映射为 ``not_found``;
* ``list_artifacts`` 覆盖 artifact 摘要与 not_found 路径;
* 审计被记录。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.factor_research import FakeLLMProvider, ResearchAssistant
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import runs
from finboard_persistence.models import ResearchRunArtifactModel, ResearchRunModel


def _run_model(run_id: str = "RR-1") -> ResearchRunModel:
    return ResearchRunModel(
        run_id=run_id,
        idempotency_key="K1",
        replay_of_run_id=None,
        strategy_id="momentum",
        strategy_kind="etf",
        status="completed",
        schema_version="1",
        manifest_checksum="mc",
        manifest={"kind": "etf"},
        result={"nav": [1.0]},
        result_checksum="rc",
        error_code=None,
        error_summary=None,
        requested_by="tester",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        started_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 2, tzinfo=UTC),
    )


def _artifact_model() -> ResearchRunArtifactModel:
    return ResearchRunArtifactModel(
        run_id="RR-1",
        artifact_id="A-1",
        decision_id=None,
        sequence=1,
        stage="features",
        trace_id="T-1",
        parent_trace_ids=[],
        payload={"rows": 10},
        checksum="ac",
    )


def _session_maker(
    *,
    list_rows: list[ResearchRunModel] | None = None,
    get_row: ResearchRunModel | None = None,
    artifact_rows: list[ResearchRunArtifactModel] | None = None,
) -> async_sessionmaker[AsyncSession]:
    """构造一个 mock session_maker,按需返回 list/get/artifact 结果。

    list_recent 与 list_artifacts 都走 ``scalars().all()``,共享同一结果;
    需要区分时在不同测试中分别构造。
    """
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list_rows or []
    result.scalar_one_or_none.return_value = get_row
    session.execute = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)

    artifacts = artifact_rows if artifact_rows is not None else []
    # list_artifacts 单独配置(第二次 execute 调用)
    artifact_result = MagicMock()
    artifact_result.scalars.return_value.all.return_value = artifacts
    session.execute = AsyncMock(side_effect=[result, artifact_result])

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
    )


class TestListRuns:
    async def test_returns_summaries(self) -> None:
        app = _make_app(_session_maker(list_rows=[_run_model()]))
        env = await runs.list_runs(app, limit=10)
        assert env.status == "ok"
        assert isinstance(env.data, list)
        assert env.data[0]["run_id"] == "RR-1"
        assert env.data[0]["strategy_kind"] == "etf"
        assert "manifest" not in env.data[0]  # 摘要不含大 manifest

    async def test_records_audit(self) -> None:
        app = _make_app(_session_maker(list_rows=[]))
        await runs.list_runs(app)
        assert len(app.audit.records) == 1
        assert app.audit.records[0].tool_name == "finboard.run.list"


class TestGetRun:
    async def test_returns_detail(self) -> None:
        app = _make_app(_session_maker(get_row=_run_model()))
        env = await runs.get_run(app, "RR-1")
        assert env.status == "ok"
        assert env.data["run_id"] == "RR-1"
        assert env.data["manifest"] == {"kind": "etf"}

    async def test_not_found(self) -> None:
        app = _make_app(_session_maker(get_row=None))
        env = await runs.get_run(app, "RR-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"


class TestListArtifacts:
    async def test_returns_artifacts(self) -> None:
        app = _make_app(
            _session_maker(get_row=_run_model(), artifact_rows=[_artifact_model()])
        )
        env = await runs.list_artifacts(app, "RR-1")
        assert env.status == "ok"
        assert env.data[0]["artifact_id"] == "A-1"
        assert env.data[0]["stage"] == "features"

    async def test_not_found(self) -> None:
        app = _make_app(_session_maker(get_row=None))
        env = await runs.list_artifacts(app, "RR-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"
