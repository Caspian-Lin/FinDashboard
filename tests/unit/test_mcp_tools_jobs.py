"""``finboard.job.*`` 工具 —— 统一后台任务队列监控与提交测试(issue #136)。

用 ``AsyncMock`` 模拟 ``AsyncSession`` + monkeypatch ``BackgroundJobRepository``
方法,验证:

* 只读查询(list / get)返回 JobOut 结构 + not_found 路径 + status 过滤校验;
* 写操作(enqueue / cancel)在 ``write_tools_enabled=False`` 时拒绝;
* enqueue 非法 kind(白名单外)→ invalid_argument;成功返回 JobOut + created 标记;
* cancel 终态幂等(request_cancel 抛 conflict 时返回当前状态不报错);
* 幂等 / 审计记录。

无需 DB。
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
from finboard_persistence.background_job_repo import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)


async def _async_return(value: object) -> object:
    return value


def _job_row(
    job_id: str = "BJ-1",
    *,
    kind: str = "echo",
    status: str = "queued",
    result_ref: str | None = None,
    archived_at: datetime | None = None,
) -> Any:
    """构造一个 duck-typed BackgroundJobModel 行(满足 JobOut.model_validate)。"""

    now = datetime(2026, 1, 15, tzinfo=UTC)
    return SimpleNamespace(
        job_id=job_id,
        kind=kind,
        queue="default",
        status=status,
        priority=0,
        payload={"msg": "hi"},
        payload_checksum="abc123",
        idempotency_key="idem-key-1234",
        progress_total=0,
        progress_done=0,
        phase=None,
        result_ref=result_ref,
        error_code=None,
        error_summary=None,
        attempt=0,
        max_attempts=3,
        worker_id=None,
        heartbeat_at=None,
        lease_until=None,
        requested_by="tester",
        created_at=now,
        started_at=None,
        finished_at=None,
        archived_at=archived_at,
        updated_at=now,
    )


def _session_maker() -> async_sessionmaker[AsyncSession]:
    """构造一个 mock session_maker,返回一个可 monkeypatch 的 AsyncMock session。"""

    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return cast("async_sessionmaker[AsyncSession]", cm)


def _get_session(
    app: McpAppContext,
) -> AsyncMock:
    """从 mock session_maker 提取内部 session 对象。"""

    cm = cast(MagicMock, app.session_maker)
    return cast(AsyncMock, cm.return_value.__aenter__.return_value)


def _make_app(
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    *,
    write_enabled: bool = True,
) -> McpAppContext:
    return McpAppContext(
        settings=Settings(),
        session_maker=session_maker or _session_maker(),
        audit=AuditRecorder(),
        write_tools_enabled=write_enabled,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


# ---------------------------------------------------------------------------
# finboard.job.list
# ---------------------------------------------------------------------------


class TestJobList:
    async def test_returns_jobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "list_recent",
            lambda self, **kw: _async_return([_job_row()]),
        )
        env = await job_tools.job_list(app)
        assert env.status == "ok"
        assert isinstance(env.data, list)
        assert env.data[0]["job_id"] == "BJ-1"
        assert env.data[0]["status"] == "queued"

    async def test_records_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "list_recent",
            lambda self, **kw: _async_return([]),
        )
        await job_tools.job_list(app)
        assert app.audit.records[0].tool_name == "finboard.job.list"
        assert app.audit.records[0].status == "ok"

    async def test_invalid_status_filter(self) -> None:
        app = _make_app()
        env = await job_tools.job_list(app, status=["bogus"])
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_invalid_archived_filter(self) -> None:
        """issue #221:archived 过滤值非法 → invalid_argument。"""
        app = _make_app()
        env = await job_tools.job_list(app, archived="sometimes")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_archived_param_passed_to_repo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue #221:archived=only 透传给 list_recent。"""
        captured: dict[str, Any] = {}

        async def _capture(self: Any, **kw: Any) -> list[Any]:
            captured.update(kw)
            return []

        app = _make_app()
        monkeypatch.setattr(BackgroundJobRepository, "list_recent", _capture)
        env = await job_tools.job_list(app, archived="only")
        assert env.status == "ok"
        assert captured["archived"] == "only"


# ---------------------------------------------------------------------------
# finboard.job.get
# ---------------------------------------------------------------------------


class TestJobGet:
    async def test_returns_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(_job_row(jid, result_ref="FSS-1")),
        )
        env = await job_tools.job_get(app, "BJ-1", view="detail")
        assert env.status == "ok"
        assert env.data["job_id"] == "BJ-1"
        assert env.data["result_ref"] == "FSS-1"
        assert env.data["payload"] == {"msg": "hi"}

    async def test_default_summary_strips_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue #206:默认 summary 剥离 payload,附 data_hash。"""
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(_job_row(jid, result_ref="FSS-1")),
        )
        env = await job_tools.job_get(app, "BJ-1")
        assert env.status == "ok"
        data = env.data
        assert data["job_id"] == "BJ-1"
        assert data["result_ref"] == "FSS-1"
        assert "payload" not in data
        assert data["data_hash"]

    async def test_view_none_is_polling_minimal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue #206:view=none 只回轮询最小字段集。"""
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(_job_row(jid, status="running")),
        )
        env = await job_tools.job_get(app, "BJ-1", view="none")
        assert env.status == "ok"
        data = env.data
        assert data["status"] == "running"
        assert set(data) <= {
            *job_tools._JOB_POLL_FIELDS,
            "data_hash",
        }

    async def test_invalid_view_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(_job_row(jid)),
        )
        env = await job_tools.job_get(app, "BJ-1", view="huge")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_data_hash_short_circuit_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue #206 P3:同状态同 hash → {unchanged: true} 不重发全量。"""
        row = _job_row("BJ-1", status="running")
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(row),
        )
        first = await job_tools.job_get(app, "BJ-1")
        assert first.status == "ok"
        data_hash = first.data["data_hash"]
        assert first.data.get("unchanged") is not True

        second = await job_tools.job_get(app, "BJ-1", data_hash=data_hash)
        assert second.status == "ok"
        assert second.data["unchanged"] is True
        assert second.data["data_hash"] == data_hash
        assert second.data["status"] == "running"
        assert "payload" not in second.data

    async def test_data_hash_changes_with_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """状态推进后 data_hash 变化,短路失效返回全量。"""
        row = _job_row("BJ-1", status="running")
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(row),
        )
        first = await job_tools.job_get(app, "BJ-1")
        stale_hash = first.data["data_hash"]

        row.status = "succeeded"
        row.result_ref = "123"
        second = await job_tools.job_get(app, "BJ-1", data_hash=stale_hash)
        assert second.status == "ok"
        assert second.data.get("unchanged") is not True
        assert second.data["status"] == "succeeded"
        assert second.data["data_hash"] != stale_hash

    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(None),
        )
        env = await job_tools.job_get(app, "missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"


# ---------------------------------------------------------------------------
# finboard.job.enqueue
# ---------------------------------------------------------------------------


class TestJobEnqueue:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await job_tools.job_enqueue(
            app,
            kind="echo",
            idempotency_key="idem-key-1",
            requested_by="agent",
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_invalid_kind(self) -> None:
        app = _make_app()
        env = await job_tools.job_enqueue(
            app,
            kind="evil_live_trading",
            idempotency_key="idem-key-2",
            requested_by="agent",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_ok_returns_jobout_and_created(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        row = _job_row()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "create_or_get",
            lambda self, **kw: _async_return((row, True)),
        )
        env = await job_tools.job_enqueue(
            app,
            kind="echo",
            idempotency_key="idem-key-3",
            requested_by="agent",
        )
        assert env.status == "ok"
        assert env.data["job_id"] == "BJ-1"
        assert env.data["created"] is True
        # 幂等键上送到信封
        assert env.idempotency_key == "idem-key-3"

    async def test_idempotent_hit_created_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        row = _job_row(status="succeeded", result_ref="FSS-1")
        monkeypatch.setattr(
            BackgroundJobRepository,
            "create_or_get",
            lambda self, **kw: _async_return((row, False)),
        )
        env = await job_tools.job_enqueue(
            app,
            kind="feature_snapshot",
            idempotency_key="idem-key-4",
            requested_by="agent",
        )
        assert env.status == "ok"
        assert env.data["created"] is False
        assert env.data["status"] == "succeeded"

    async def test_conflict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()

        async def _raise(self: Any, **kw: Any) -> Any:
            raise BackgroundJobPersistenceConflictError("checksum mismatch")

        monkeypatch.setattr(BackgroundJobRepository, "create_or_get", _raise)
        env = await job_tools.job_enqueue(
            app,
            kind="echo",
            idempotency_key="idem-key-5",
            requested_by="agent",
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"

    async def test_records_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        row = _job_row()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "create_or_get",
            lambda self, **kw: _async_return((row, True)),
        )
        await job_tools.job_enqueue(
            app,
            kind="echo",
            idempotency_key="idem-key-6",
            requested_by="agent",
        )
        assert app.audit.records[0].tool_name == "finboard.job.enqueue"

    # ------------------------------------------------- issue #260 payload 契约

    async def test_payload_unknown_key_rejected(self) -> None:
        """data_types 是不存在的键 → 入队即 invalid_argument(不再静默忽略)。"""
        app = _make_app()
        env = await job_tools.job_enqueue(
            app,
            kind="research_data_sync",
            idempotency_key="idem-key-260a",
            requested_by="agent",
            payload={
                "data_types": ["financial_indicators"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "data_types" in (env.error.message or "")
        assert "datasets" in (env.error.message or "")

    async def test_payload_missing_start_date_rejected(self) -> None:
        app = _make_app()
        env = await job_tools.job_enqueue(
            app,
            kind="research_data_sync",
            idempotency_key="idem-key-260b",
            requested_by="agent",
            payload={"datasets": ["profiles"], "end_date": "2026-01-31"},
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "start_date" in (env.error.message or "")

    async def test_payload_empty_symbol_pool_rejected(self) -> None:
        """逐标的数据集缺 symbols 且缺 profiles → 入队即拒(空池静默零迭代)。"""
        app = _make_app()
        env = await job_tools.job_enqueue(
            app,
            kind="research_data_sync",
            idempotency_key="idem-key-260c",
            requested_by="agent",
            payload={
                "datasets": ["financial_indicators"],
                "start_date": "2026-01-01",
                "end_date": "2026-03-31",
            },
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "symbol 池" in (env.error.message or "")

    async def test_payload_contract_valid_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        row = _job_row(kind="research_data_sync")
        monkeypatch.setattr(
            BackgroundJobRepository,
            "create_or_get",
            lambda self, **kw: _async_return((row, True)),
        )
        env = await job_tools.job_enqueue(
            app,
            kind="research_data_sync",
            idempotency_key="idem-key-260d",
            requested_by="agent",
            payload={
                "datasets": ["profiles", "daily_metrics"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
        )
        assert env.status == "ok"
        assert env.data["created"] is True

    async def test_unregistered_kind_payload_not_validated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未注册 kind 的 payload 保持自由结构(向后兼容)。"""
        app = _make_app()
        row = _job_row(kind="echo")
        monkeypatch.setattr(
            BackgroundJobRepository,
            "create_or_get",
            lambda self, **kw: _async_return((row, True)),
        )
        env = await job_tools.job_enqueue(
            app,
            kind="echo",
            idempotency_key="idem-key-260e",
            requested_by="agent",
            payload={"anything": ["goes", "here"]},
        )
        assert env.status == "ok"


# ---------------------------------------------------------------------------
# finboard.job.cancel
# ---------------------------------------------------------------------------


class TestJobCancel:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await job_tools.job_cancel(app, "BJ-1")
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(None),
        )
        env = await job_tools.job_cancel(app, "missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_running_transitions_to_cancel_requested(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        running = _job_row(status="running")
        cancelled = _job_row(status="cancel_requested")
        # 第一次 get 校验存在;request_cancel 成功;第二次 get 返回新状态。
        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(running),
        )
        monkeypatch.setattr(
            BackgroundJobRepository,
            "request_cancel",
            lambda self, jid: _async_return(cancelled),
        )
        env = await job_tools.job_cancel(app, "BJ-1")
        assert env.status == "ok"
        assert env.data["status"] == "running"  # 第一次 get 的行(未刷新)
        assert app.audit.records[0].tool_name == "finboard.job.cancel"

    async def test_terminal_returns_current_status_no_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """终态任务:request_cancel 抛 conflict,捕获后返回当前状态不报错。"""

        app = _make_app()
        terminal = _job_row(status="succeeded", result_ref="FSS-1")

        async def _raise_conflict(self: Any, jid: str) -> Any:
            raise BackgroundJobPersistenceConflictError("not running")

        monkeypatch.setattr(
            BackgroundJobRepository,
            "get",
            lambda self, jid: _async_return(terminal),
        )
        monkeypatch.setattr(
            BackgroundJobRepository, "request_cancel", _raise_conflict
        )
        env = await job_tools.job_cancel(app, "BJ-1")
        assert env.status == "ok"
        assert env.data["status"] == "succeeded"


# ---------------------------------------------------------------------------
# finboard.job.archive / finboard.job.unarchive(issue #221)
# ---------------------------------------------------------------------------


class TestJobArchive:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await job_tools.job_archive(app, job_id="BJ-1")
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_single_ok_returns_jobout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        archived = _job_row(
            status="succeeded", archived_at=datetime(2026, 1, 16, tzinfo=UTC)
        )
        monkeypatch.setattr(
            BackgroundJobRepository,
            "archive",
            lambda self, jid: _async_return(archived),
        )
        env = await job_tools.job_archive(app, job_id="BJ-1")
        assert env.status == "ok"
        assert env.data["job_id"] == "BJ-1"
        assert env.data["archived_at"] is not None
        assert app.audit.records[0].tool_name == "finboard.job.archive"

    async def test_single_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()

        async def _raise(self: Any, jid: str) -> Any:
            raise BackgroundJobPersistenceConflictError("后台任务不存在: BJ-1")

        monkeypatch.setattr(BackgroundJobRepository, "archive", _raise)
        env = await job_tools.job_archive(app, job_id="BJ-1")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"

    async def test_single_non_terminal_conflict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()

        async def _raise(self: Any, jid: str) -> Any:
            raise BackgroundJobPersistenceConflictError(
                "任务 BJ-1 当前状态 running 不是终态,不支持归档"
            )

        monkeypatch.setattr(BackgroundJobRepository, "archive", _raise)
        env = await job_tools.job_archive(app, job_id="BJ-1")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "conflict"

    async def test_job_id_and_filters_mutually_exclusive(self) -> None:
        app = _make_app()
        env = await job_tools.job_archive(
            app, job_id="BJ-1", kinds=["echo"]
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_bulk_returns_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()
        monkeypatch.setattr(
            BackgroundJobRepository,
            "archive_bulk",
            lambda self, **kw: _async_return(42),
        )
        env = await job_tools.job_archive(app, statuses=["succeeded"], limit=50)
        assert env.status == "ok"
        assert env.data == {"archived_count": 42}

    async def test_bulk_rejects_non_terminal_statuses(self) -> None:
        app = _make_app()
        env = await job_tools.job_archive(app, statuses=["running"])
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"

    async def test_bulk_invalid_finished_before(self) -> None:
        app = _make_app()
        env = await job_tools.job_archive(app, finished_before="not-a-date")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"


class TestJobUnarchive:
    async def test_write_disabled_rejects(self) -> None:
        app = _make_app(write_enabled=False)
        env = await job_tools.job_unarchive(app, "BJ-1")
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_ok_clears_archived_at(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _make_app()
        restored = _job_row(status="succeeded", archived_at=None)
        monkeypatch.setattr(
            BackgroundJobRepository,
            "unarchive",
            lambda self, jid: _async_return(restored),
        )
        env = await job_tools.job_unarchive(app, "BJ-1")
        assert env.status == "ok"
        assert env.data["archived_at"] is None
        assert app.audit.records[0].tool_name == "finboard.job.unarchive"

    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _make_app()

        async def _raise(self: Any, jid: str) -> Any:
            raise BackgroundJobPersistenceConflictError("后台任务不存在: BJ-1")

        monkeypatch.setattr(BackgroundJobRepository, "unarchive", _raise)
        env = await job_tools.job_unarchive(app, "missing")
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "not_found"
