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

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import runs
from finboard_persistence.models import ResearchRunArtifactModel, ResearchRunModel


async def _async_return(value: object) -> object:
    return value


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
    return McpAppContext(
        settings=Settings(),
        session_maker=session_maker,
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
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
        # issue #183:单快照(未设置 rebalance_frequency)标注 single_shot。
        assert env.data["execution_mode"] == "single_shot"

    async def test_returns_detail_multi_period(self) -> None:
        row = _run_model()
        row.manifest = {"parameters": {"rebalance_frequency": "monthly"}}
        app = _make_app(_session_maker(get_row=row))
        env = await runs.get_run(app, "RR-1")
        assert env.status == "ok"
        assert env.data["execution_mode"] == "multi_period"

    async def test_not_found(self) -> None:
        app = _make_app(_session_maker(get_row=None))
        env = await runs.get_run(app, "RR-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"


class TestListArtifacts:
    async def test_returns_artifacts(self) -> None:
        app = _make_app(_session_maker(get_row=_run_model(), artifact_rows=[_artifact_model()]))
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


# ---------------------------------------------------------------------------
# 写工具(issue #127):queue / cancel / replay / lineage
# ---------------------------------------------------------------------------


def _write_disabled_app() -> McpAppContext:
    session = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return McpAppContext(
        settings=Settings(),
        session_maker=cast("async_sessionmaker[AsyncSession]", cm),
        audit=AuditRecorder(),
        write_tools_enabled=False,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


class TestQueueRun:
    async def test_write_disabled(self) -> None:
        app = _write_disabled_app()
        env = await runs.queue_run(app, payload={"idempotency_key": "x"})
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_invalid_payload(self) -> None:
        app = _make_app(_session_maker())
        # ResearchRunQueueIn 要求 idempotency_key 8-128 / initial_capital 范围等
        env = await runs.queue_run(app, payload={"idempotency_key": "x"})
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_invalid_rebalance_frequency_rejected(self) -> None:
        """issue #183:非法 rebalance_frequency 在入队时提前拒绝。"""
        app = _make_app(_session_maker())
        env = await runs.queue_run(
            app,
            payload={
                "idempotency_key": "queue-freq-invalid",
                "strategy_id": "s",
                "strategy_version": 1,
                "dataset_release_ids": ["r1"],
                "parameters": {"rebalance_frequency": "weekly"},
                "code_version": "abcdef0123456789",
                "requested_by": "tester",
            },
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "rebalance_frequency" in (env.error.message or "")


class TestCancelRun:
    async def test_write_disabled(self) -> None:
        app = _write_disabled_app()
        env = await runs.cancel_run(app, "RR-1")
        assert env.status == "denied"

    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_backtest.research_run import (
            ResearchRunConflictError,
            ResearchRunCoordinator,
        )

        app = _make_app(_session_maker(get_row=_run_model()))

        # coordinator.cancel 抛 conflict -> 映射 conflict
        async def _boom(self, run_id):
            raise ResearchRunConflictError("illegal transition")

        monkeypatch.setattr(ResearchRunCoordinator, "cancel", _boom)
        env = await runs.cancel_run(app, "RR-1")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"


class TestReplayRun:
    async def test_write_disabled(self) -> None:
        app = _write_disabled_app()
        env = await runs.replay_run(app, "RR-1", idempotency_key="newkey12345", requested_by="u")
        assert env.status == "denied"


class TestLineageRun:
    async def test_empty_artifacts_returns_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_backtest.research_run import ResearchRunCoordinator

        app = _make_app(_session_maker())
        monkeypatch.setattr(
            ResearchRunCoordinator,
            "lineage",
            lambda self, run_id, trace_id: _async_return([]),
        )
        env = await runs.lineage_run(app, "RR-1", "T-missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"
