"""issue #306:job 视图透传 ``run_status`` —— MCP ``finboard_job_get`` 与 REST schema。

锁定:

* kind=research_run 的 job 单查附 ``run_status``(关联 research_runs.status,
  查不到为 None);非 research_run kind 恒 None;
* view=none / unchanged 短路返回同样携带 run_status;
* run_status 参与 data_hash —— run 侧状态翻转不再误报 unchanged;
* REST ``JobOut`` schema 带 run_status 字段(默认 None,列表不 join)。

用 ``AsyncMock`` 模拟 session + monkeypatch repository 方法,无需 DB(真实
DB 链路由 ``tests/integration/test_issue_306_run_guard.py`` 覆盖)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import jobs as job_tools
from finboard_persistence.background_job_repo import BackgroundJobRepository
from finboard_persistence.research_run_repo import ResearchRunRepository


async def _async_return(value: object) -> object:
    return value


def _job_row(
    job_id: str = "BJ-306",
    *,
    kind: str = "research_run",
    status: str = "running",
) -> Any:
    now = datetime(2026, 9, 4, tzinfo=UTC)
    return SimpleNamespace(
        job_id=job_id,
        kind=kind,
        queue="research",
        status=status,
        priority=0,
        payload={"run_id": "RR-x"},
        payload_checksum="abc123",
        idempotency_key="idem-key-306",
        progress_total=13,
        progress_done=0,
        phase="research_run:decision_load",
        result_ref=None,
        error_code=None,
        error_summary=None,
        attempt=1,
        max_attempts=3,
        worker_id="w-1",
        heartbeat_at=now,
        lease_until=now,
        requested_by="tester",
        created_at=now,
        started_at=now,
        finished_at=None,
        archived_at=None,
        updated_at=now,
    )


def _session_maker() -> async_sessionmaker[AsyncSession]:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return cast("async_sessionmaker[AsyncSession]", cm)


def _make_app() -> McpAppContext:
    return McpAppContext(
        settings=Settings(),
        session_maker=_session_maker(),
        audit=AuditRecorder(),
        write_tools_enabled=True,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


@pytest.fixture
def research_run_links(monkeypatch: pytest.MonkeyPatch):
    """monkeypatch job 查询与 run_status 投影;返回可变的 run_status 槽。"""

    state: dict[str, str | None] = {"run_status": "running"}
    monkeypatch.setattr(
        BackgroundJobRepository,
        "get",
        lambda self, jid, for_update=False: _async_return(_job_row(jid)),
    )
    monkeypatch.setattr(
        ResearchRunRepository,
        "get_status_by_job_id",
        lambda self, job_id: _async_return(state["run_status"]),
    )
    return state


class TestJobGetRunStatus:
    async def test_research_run_job_carries_run_status(
        self, research_run_links: dict[str, str | None]
    ) -> None:
        app = _make_app()
        env = await job_tools.job_get(app, "BJ-306", view="summary")
        assert env.status == "ok"
        assert env.data["run_status"] == "running"

    async def test_run_status_visible_when_run_interrupted_but_job_running(
        self, research_run_links: dict[str, str | None]
    ) -> None:
        """不一致一眼可见:run interrupted / job running。"""

        research_run_links["run_status"] = "interrupted"
        app = _make_app()
        env = await job_tools.job_get(app, "BJ-306", view="summary")
        assert env.data["run_status"] == "interrupted"
        assert env.data["status"] == "running"

    async def test_missing_run_yields_null_run_status(
        self, research_run_links: dict[str, str | None]
    ) -> None:
        research_run_links["run_status"] = None
        app = _make_app()
        env = await job_tools.job_get(app, "BJ-306", view="summary")
        assert env.data["run_status"] is None

    async def test_non_research_kind_has_null_run_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid, for_update=False: _async_return(
                _job_row(jid, kind="echo", status="queued")
            ),
        )
        called: list[str] = []

        async def _fail(self: Any, job_id: str) -> str:
            called.append(job_id)
            return "running"

        monkeypatch.setattr(ResearchRunRepository, "get_status_by_job_id", _fail)
        app = _make_app()
        env = await job_tools.job_get(app, "BJ-306", view="summary")
        assert env.data["run_status"] is None
        assert called == []  # 非 research_run 不发起 run 查询

    async def test_view_none_includes_run_status(
        self, research_run_links: dict[str, str | None]
    ) -> None:
        app = _make_app()
        env = await job_tools.job_get(app, "BJ-306", view="none")
        assert env.data["run_status"] == "running"
        assert "payload" not in env.data

    async def test_unchanged_short_circuit_carries_run_status(
        self, research_run_links: dict[str, str | None]
    ) -> None:
        app = _make_app()
        first = await job_tools.job_get(app, "BJ-306", view="summary")
        data_hash = first.data["data_hash"]
        second = await job_tools.job_get(
            app, "BJ-306", view="summary", data_hash=data_hash
        )
        assert second.data["unchanged"] is True
        assert second.data["run_status"] == "running"

    async def test_run_status_flip_invalidates_data_hash(
        self, research_run_links: dict[str, str | None]
    ) -> None:
        """run 侧状态翻转 → data_hash 变化 → 短路失效,返回全量。"""

        app = _make_app()
        first = await job_tools.job_get(app, "BJ-306", view="summary")
        stale_hash = first.data["data_hash"]
        research_run_links["run_status"] = "interrupted"
        second = await job_tools.job_get(
            app, "BJ-306", view="summary", data_hash=stale_hash
        )
        # 短路失效:返回全量(无 unchanged 键),run_status 为新值。
        assert "unchanged" not in second.data
        assert second.data["run_status"] == "interrupted"
        assert second.data["data_hash"] != stale_hash


class TestRestJobOutSchema:
    def test_run_status_defaults_to_none(self) -> None:
        from finboard_api.job_schemas import JobOut

        row = _job_row("BJ-306")
        out = JobOut.model_validate(row)
        # 列表 / 写端点不 join:run_status 恒 None(单查由路由填充)。
        assert out.run_status is None

    def test_run_status_accepts_value(self) -> None:
        from finboard_api.job_schemas import JobOut

        row = _job_row("BJ-306")
        out = JobOut.model_validate(row)
        out.run_status = "interrupted"
        assert out.run_status == "interrupted"
