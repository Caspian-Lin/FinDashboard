"""研究操作审计(issue #518):同事务语义、拒绝补记、脱敏、读 API 与作用域。

依赖真实 PostgreSQL(``FINBOARD_TEST_DB_URL``)。只读写研究域表
(``research_audit_events`` / ``research_runs`` / ``background_jobs`` /
``research_topics``),不触实盘 ``orders`` / ``fills`` / ``positions`` /
``audit_logs``,不执行回测。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.routes.jobs import router as jobs_router
from finboard_api.routes.research_audit import router as research_audit_router
from finboard_app.research_audit import (
    ResearchAuditEvent,
    ResearchAuditRecorder,
    ResearchAuditWorkerSink,
    configure_research_audit_recorder,
    reset_research_audit_recorder,
)
from finboard_app.research_operation import ResearchOperationContext
from finboard_app.research_run_submission import (
    ResearchRunSubmissionService,
    ResearchRunSubmitInput,
)
from finboard_app.research_topic_lifecycle import TopicRevisionConflictError
from finboard_app.research_workspace import workspace_write
from finboard_mcp.context import McpAppContext
from finboard_persistence import McpClientRepository, session_factory
from finboard_persistence.models import (
    BackgroundJobModel,
    ResearchAuditEventModel,
    ResearchRunModel,
    ResearchTopicModel,
)
from finboard_persistence.research_audit_repo import ResearchAuditRepository
from tests.integration.test_research_run_submission_equivalence import (
    _payload,
    _seed,
)

pytestmark = pytest.mark.asyncio

GOAL: dict[str, Any] = {
    "version": "v1",
    "criteria": "年化15%,最大回撤20%",
    "source": {"kind": "document", "ref_id": "ROADMAP.md"},
}


def _topic_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "审计同事务课题",
        "question": "审计失败是否回滚业务写入?",
        "goal": dict(GOAL),
        **overrides,
    }


async def _audit_rows(
    engine: AsyncEngine, *conditions: Any
) -> list[ResearchAuditEventModel]:
    async with session_factory(engine)() as session:
        stmt = select(ResearchAuditEventModel).order_by(
            ResearchAuditEventModel.id.asc()
        )
        for condition in conditions:
            stmt = stmt.where(condition)
        return list((await session.execute(stmt)).scalars())


async def _count(engine: AsyncEngine, model: Any) -> int:
    async with session_factory(engine)() as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


async def _delete_audit(engine: AsyncEngine) -> None:
    from sqlalchemy import delete

    async with session_factory(engine)() as session:
        await session.execute(delete(ResearchAuditEventModel))
        await session.commit()


_SEED_LOCK = asyncio.Lock()
_SEEDED = False


async def _ensure_seed(engine: AsyncEngine) -> None:
    """#513 的 seed 使用固定 ID:模块内只做一次,先清表保证确定性。"""

    global _SEEDED
    async with _SEED_LOCK:
        if _SEEDED:
            return
        from sqlalchemy import delete

        from finboard_persistence.models import (
            DatasetManifestModel,
            InstrumentModel,
            ResearchDatasetReleaseModel,
        )
        from tests.integration.conftest import clean_tables

        async with engine.begin() as conn:
            await clean_tables(conn)
            # 共享库兜底模式(未配置 FINBOARD_TEST_DB_URL)会保留这几张表;
            # 本模块的 seed 使用固定 ID,必须显式清干净。
            await conn.execute(delete(InstrumentModel))
            await conn.execute(delete(ResearchDatasetReleaseModel))
            await conn.execute(delete(DatasetManifestModel))
        await _seed(engine)
        _SEEDED = True


@pytest_asyncio.fixture(scope="module")
async def engine(_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    yield _engine


@pytest_asyncio.fixture(autouse=True)
async def _isolate(engine: AsyncEngine) -> AsyncIterator[None]:
    await _delete_audit(engine)
    yield
    await _delete_audit(engine)
    reset_research_audit_recorder()


@pytest_asyncio.fixture
async def audit(engine: AsyncEngine) -> AsyncIterator[ResearchAuditRecorder]:
    recorder = ResearchAuditRecorder(session_maker=session_factory(engine))
    configure_research_audit_recorder(recorder)
    yield recorder
    reset_research_audit_recorder()


@pytest_asyncio.fixture
async def audit_client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    sm = session_factory(engine)
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        research_audit_client_events=False,
        research_audit_retention_days=365,
    )
    app.include_router(research_audit_router)

    async def _dependency() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    app.dependency_overrides[get_db_session] = _dependency
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


# --------------------------------------------------------------------------- #
# 同事务语义
# --------------------------------------------------------------------------- #


async def test_success_audit_shares_business_transaction(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    """成功审计与业务写入同一事务:未 commit 前可见,rollback 后一起消失。"""

    await _ensure_seed(engine)
    sm = session_factory(engine)
    async with sm() as session:
        service = ResearchRunSubmissionService(session)
        outcome = await service.submit(
            ResearchRunSubmitInput.from_json_payload(_payload("audit-tx-rollback")),
            ResearchOperationContext.for_web(actor="user:api"),
        )
        # 唯一一次 commit 之前:业务行与审计行都在同一未提交事务里。
        rows = list((await session.execute(select(ResearchAuditEventModel))).scalars())
        assert {row.operation for row in rows} == {"run.submit", "job.enqueue"}
        run_submit = next(row for row in rows if row.operation == "run.submit")
        assert run_submit.outcome == "succeeded"
        assert run_submit.source == "web"
        assert run_submit.actor == "user:api"
        assert run_submit.actor_type == "human"
        assert run_submit.entity_type == "research_run"
        assert run_submit.entity_id == outcome.run_row.run_id
        assert run_submit.correlation["run_id"] == outcome.run_row.run_id
        assert run_submit.idempotency_key == "audit-tx-rollback"
        await session.rollback()

    async with sm() as session:
        run_count = await session.execute(select(func.count()).select_from(ResearchRunModel))
        audit_count = await session.execute(
            select(func.count()).select_from(ResearchAuditEventModel)
        )
        assert int(run_count.scalar_one()) == 0
        assert int(audit_count.scalar_one()) == 0


async def test_commit_persists_business_and_audit(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    await _ensure_seed(engine)
    sm = session_factory(engine)
    async with sm() as session:
        outcome = await ResearchRunSubmissionService(session).submit(
            ResearchRunSubmitInput.from_json_payload(_payload("audit-tx-commit")),
            ResearchOperationContext.for_web(actor="user:api"),
        )
        await session.commit()

    rows = await _audit_rows(engine)
    run_submit = next(row for row in rows if row.operation == "run.submit")
    job_enqueue = next(row for row in rows if row.operation == "job.enqueue")
    assert run_submit.correlation["job_id"] == outcome.run_row.job_id
    assert job_enqueue.entity_id == outcome.run_row.job_id
    assert job_enqueue.correlation["run_id"] == outcome.run_row.run_id
    assert len(run_submit.params_digest) == 64
    assert job_enqueue.result_code == "created"


class _ExplodingRecorder(ResearchAuditRecorder):
    """模拟审计持久化失败:record 抛错,detached 静默(测试用)。"""

    async def record(self, session: AsyncSession, event: ResearchAuditEvent) -> None:
        raise RuntimeError("audit storage down")

    async def record_detached(self, event: ResearchAuditEvent) -> None:
        return None


async def test_audit_failure_rolls_back_business_write(engine: AsyncEngine) -> None:
    configure_research_audit_recorder(_ExplodingRecorder())
    sm = session_factory(engine)
    async with sm() as session:
        with pytest.raises(RuntimeError, match="audit storage down"):
            await workspace_write(
                session,
                actor="user:api",
                operation="create",
                payload=_topic_payload(title="审计失败课题"),
            )
        await session.rollback()
    assert await _count(engine, ResearchTopicModel) == 0
    assert await _count(engine, ResearchAuditEventModel) == 0


# --------------------------------------------------------------------------- #
# 拒绝路径(独立会话补记,不掩盖原始错误)
# --------------------------------------------------------------------------- #


async def test_rejection_writes_detached_row_without_masking(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    sm = session_factory(engine)
    async with sm() as session:
        topic = await workspace_write(
            session, actor="user:api", operation="create", payload=_topic_payload()
        )
    async with sm() as session:
        with pytest.raises(TopicRevisionConflictError):
            await workspace_write(
                session,
                actor="user:api",
                operation="update",
                topic_id=topic["topic_id"],
                expected_revision=999,
                payload=_topic_payload(title="版本冲突"),
            )
        await session.rollback()

    rows = [
        row
        for row in await _audit_rows(engine)
        if row.operation == "topic.update" and row.outcome == "rejected"
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row.result_code == "topic_revision_conflict"
    assert row.topic_id == topic["topic_id"]
    assert row.error_summary
    assert row.actor == "user:api"
    assert row.source == "web"
    # 业务表仍然是原版本(拒绝没有写入)。
    async with sm() as session:
        stored = await session.scalar(
            select(ResearchTopicModel).where(
                ResearchTopicModel.topic_id == topic["topic_id"]
            )
        )
    assert stored is not None
    assert stored.revision == topic["revision"]


# --------------------------------------------------------------------------- #
# 脱敏 / 截断 / digest
# --------------------------------------------------------------------------- #


async def test_params_are_redacted_and_bounded(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    long_text = "x" * 500
    summary = {
        "api_key": "sk-" + "a" * 30,
        "note": "Bearer abc.def-1234567890",
        "password": "hunter2",
        "long": long_text,
        "nested": {"token": "t0ken-value"},
        **{f"k{index}": "v" * 100 for index in range(80)},
    }
    async with session_factory(engine)() as session:
        await audit.record(
            session,
            ResearchAuditEvent(
                operation="run.submit",
                outcome="failed",
                context=ResearchOperationContext.for_web(actor="user:api"),
                params_summary=summary,
                error_summary="token=abc123",
            ),
        )
        await session.commit()

    row = (await _audit_rows(engine))[0]
    stored = row.params_summary
    assert stored["api_key"] == "***REDACTED***"
    assert stored["password"] == "***REDACTED***"
    assert "Bearer abc" not in str(stored["note"])
    assert len(str(stored["long"])) <= 120
    assert "t0ken-value" not in str(stored)
    assert stored.get("_truncated") is True
    assert len(str(stored).encode("utf-8")) <= 4200
    assert len(row.params_digest) == 64
    assert "abc123" not in (row.error_summary or "")


# --------------------------------------------------------------------------- #
# 读 API:筛选 / 游标 / 覆盖 / 单条
# --------------------------------------------------------------------------- #


async def _insert_events(
    engine: AsyncEngine, count: int = 5, *, start_offset: int = 0
) -> list[str]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    ids: list[str] = []
    async with session_factory(engine)() as session:
        for index in range(count):
            event_id = f"RA-{uuid4().hex[:16]}"
            ids.append(event_id)
            await ResearchAuditRepository(session).append(
                audit_id=event_id,
                occurred_at=base + timedelta(minutes=index + start_offset),
                actor="agent:MC-filter" if index % 2 == 0 else "user:api",
                actor_type="agent" if index % 2 == 0 else "human",
                source="mcp" if index % 2 == 0 else "web",
                operation="topic.append" if index < 3 else "run.submit",
                outcome="succeeded" if index != 4 else "rejected",
                topic_id="RT-filter" if index < 3 else None,
                entity_type="research_topic" if index < 3 else "research_run",
                entity_id=f"E-{index}",
                params_digest="0" * 64,
                params_summary={"index": index},
            )
        await session.commit()
    return ids


async def test_filters_and_cursor(engine: AsyncEngine) -> None:
    await _insert_events(engine, 5)
    async with session_factory(engine)() as session:
        repo = ResearchAuditRepository(session)
        filtered = await repo.list_events(actor="agent:MC-filter")
        assert len(filtered) == 3
        assert all(row.actor == "agent:MC-filter" for row in filtered)
        by_topic = await repo.list_events(topic_id="RT-filter")
        assert len(by_topic) == 3
        by_outcome = await repo.list_events(outcome="rejected")
        assert len(by_outcome) == 1
        window = await repo.list_events(
            since=datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
            until=datetime(2026, 1, 1, 0, 3, tzinfo=UTC),
        )
        assert len(window) == 2

        first = await repo.list_events(limit=2)
        assert len(first) == 3  # limit + 1(has_more)
        cursor = (first[1].occurred_at, first[1].id)
        second = await repo.list_events(limit=2, cursor=cursor)
        assert {row.id for row in first[:2]}.isdisjoint({row.id for row in second})
        assert second[0].occurred_at < first[1].occurred_at


async def test_read_api_filters_cursor_coverage_and_single(
    engine: AsyncEngine, audit_client: AsyncClient
) -> None:
    ids = await _insert_events(engine, 5)

    page = await audit_client.get("/api/research/audit", params={"limit": 2})
    assert page.status_code == 200
    body = page.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert body["next_cursor"]
    assert body["items"][0]["occurred_at"] >= body["items"][1]["occurred_at"]

    page2 = await audit_client.get(
        "/api/research/audit", params={"limit": 2, "cursor": body["next_cursor"]}
    )
    assert page2.status_code == 200
    second = page2.json()
    assert {i["id"] for i in second["items"]}.isdisjoint(
        {i["id"] for i in body["items"]}
    )

    filtered = await audit_client.get(
        "/api/research/audit", params={"source": "mcp", "outcome": "succeeded"}
    )
    assert filtered.status_code == 200
    assert all(item["source"] == "mcp" for item in filtered.json()["items"])
    assert filtered.json()["items"]

    single = await audit_client.get(f"/api/research/audit/{ids[0]}")
    assert single.status_code == 200
    assert single.json()["audit_id"] == ids[0]
    missing = await audit_client.get("/api/research/audit/RA-doesnotexist")
    assert missing.status_code == 404
    oversized = await audit_client.get(
        "/api/research/audit", params={"limit": 201}
    )
    assert oversized.status_code == 422

    coverage = await audit_client.get("/api/research/audit/coverage")
    assert coverage.status_code == 200
    cov = coverage.json()
    assert cov["from"] is not None
    assert cov["total"] == 5
    assert cov["retention_days"] == 365
    assert cov["auto_purge"] is False
    assert "research_writes_before_518" in cov["gaps"]


# --------------------------------------------------------------------------- #
# 客户端声明事件
# --------------------------------------------------------------------------- #


async def _client_credential(
    engine: AsyncEngine, *, scopes: list[str]
) -> tuple[str, str]:
    from finboard_mcp.identity import generate_client_id, generate_client_token

    client_id = generate_client_id()
    token = generate_client_token(client_id)
    async with session_factory(engine)() as session:
        await McpClientRepository(session).append(
            client_id=client_id,
            name=f"audit-test-{uuid4().hex[:8]}",
            token_hash=token.token_hash,
            token_prefix=token.token_prefix,
            scopes=scopes,
            created_by="integration-test",
        )
        await session.commit()
    return client_id, token.token


async def test_client_events_disabled_by_default(
    engine: AsyncEngine, audit_client: AsyncClient, audit: ResearchAuditRecorder
) -> None:
    _client_id, token = await _client_credential(engine, scopes=["research.write"])
    response = await audit_client.post(
        "/api/research/audit/client-events",
        headers={"Authorization": f"Bearer {token}"},
        json={"event_type": "tool_call", "client_event_id": "ce-1", "outcome": "ok"},
    )
    assert response.status_code == 404
    assert await _count(engine, ResearchAuditEventModel) == 0


async def test_client_events_dedupe_and_trust(
    engine: AsyncEngine, audit_client: AsyncClient, audit: ResearchAuditRecorder
) -> None:
    client_id, token = await _client_credential(engine, scopes=["research.write"])
    app = audit_client._transport.app  # type: ignore[attr-defined]
    app.state.settings.research_audit_client_events = True
    payload = {
        "event_type": "tool_call",
        "client_event_id": "ce-dedupe-1",
        "tool_name": "finboard_topic_write",
        "outcome": "ok",
        "summary": "created topic",
        "correlation": {"topic_id": "RT-x"},
    }
    first = await audit_client.post(
        "/api/research/audit/client-events",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True
    assert first.json()["trust"] == "client_declared"

    second = await audit_client.post(
        "/api/research/audit/client-events",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert second.status_code == 201
    assert second.json()["created"] is False
    assert second.json()["deduplicated"] is True
    assert second.json()["audit_id"] == first.json()["audit_id"]

    rows = await _audit_rows(engine)
    assert len(rows) == 1
    assert rows[0].trust == "client_declared"
    assert rows[0].client_id == client_id
    assert rows[0].idempotency_key == "ce-dedupe-1"
    assert rows[0].operation == "client.tool_call"
    assert rows[0].outcome == "succeeded"

    # 缺少 scope 的凭据被拒绝。
    _other_id, read_token = await _client_credential(engine, scopes=["research.read"])
    denied = await audit_client.post(
        "/api/research/audit/client-events",
        headers={"Authorization": f"Bearer {read_token}"},
        json={**payload, "client_event_id": "ce-denied"},
    )
    assert denied.status_code == 403
    # 未带凭据 → 401。
    unauthenticated = await audit_client.post(
        "/api/research/audit/client-events", json=payload
    )
    assert unauthenticated.status_code == 401


# --------------------------------------------------------------------------- #
# MCP 作用域:audit.read
# --------------------------------------------------------------------------- #


async def test_mcp_audit_read_scope_enforced() -> None:
    from finboard_mcp.audit import AuditRecorder
    from finboard_mcp.execution import run_tool
    from finboard_mcp.identity import McpClientIdentity
    from finboard_mcp.scopes import arm_scope_enforcement, reset_scope_policy

    async def handler() -> dict[str, Any]:
        return {"ok": True}

    readonly = McpClientIdentity(
        client_id="MC-readonly", name="readonly", scopes=frozenset({"research.read"})
    )
    auditor = McpClientIdentity(
        client_id="MC-auditor", name="auditor", scopes=frozenset({"audit.read"})
    )
    try:
        arm_scope_enforcement(readonly_only=False, default_identity=readonly)
        denied = await run_tool(
            audit=AuditRecorder(),
            tool_name="finboard.audit.query",
            arguments={},
            handler=handler,
        )
        assert denied.status == "denied"
        assert denied.error is not None
        assert denied.error.kind == "permission_denied"

        arm_scope_enforcement(readonly_only=False, default_identity=auditor)
        allowed = await run_tool(
            audit=AuditRecorder(),
            tool_name="finboard.audit.query",
            arguments={},
            handler=handler,
        )
        assert allowed.status == "ok", allowed.error
        assert allowed.data["ok"] is True
    finally:
        reset_scope_policy()


# --------------------------------------------------------------------------- #
# e2e:一次课题创建 + run 提交,产生可串联的审计
# --------------------------------------------------------------------------- #


async def test_topic_create_and_run_submit_are_correlated(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    await _ensure_seed(engine)
    sm = session_factory(engine)
    context = ResearchOperationContext.for_web(actor="user:api", operation_id="OP-e2e")
    async with sm() as session:
        topic = await workspace_write(
            session,
            actor="user:api",
            operation="create",
            payload=_topic_payload(title="端到端课题"),
            context=context,
        )
        outcome = await ResearchRunSubmissionService(session).submit(
            ResearchRunSubmitInput.from_json_payload(_payload("audit-e2e-run")),
            context,
        )
        await session.commit()

    rows = await _audit_rows(engine)
    by_operation = {row.operation: row for row in rows}
    topic_row = by_operation["topic.create"]
    run_row = by_operation["run.submit"]
    job_row = by_operation["job.enqueue"]

    assert topic_row.topic_id == topic["topic_id"]
    assert topic_row.entity_id == topic["topic_id"]
    assert topic_row.actor == "user:api"
    assert topic_row.source == "web"
    assert topic_row.actor_type == "human"
    assert topic_row.correlation["topic_id"] == topic["topic_id"]

    assert run_row.entity_id == outcome.run_row.run_id
    assert run_row.actor == topic_row.actor
    assert run_row.source == topic_row.source
    assert run_row.correlation["run_id"] == outcome.run_row.run_id
    assert run_row.correlation["job_id"] == outcome.run_row.job_id
    assert run_row.correlation["operation_id"] == "OP-e2e"
    assert job_row.correlation["run_id"] == run_row.correlation["run_id"]
    assert job_row.correlation["job_id"] == run_row.correlation["job_id"]

    # 业务事实与审计一致(run 与 job 都真实存在)。
    async with sm() as session:
        run_exists = await session.scalar(
            select(ResearchRunModel.run_id).where(
                ResearchRunModel.run_id == outcome.run_row.run_id
            )
        )
        job_exists = await session.scalar(
            select(BackgroundJobModel.job_id).where(
                BackgroundJobModel.job_id == outcome.run_row.job_id
            )
        )
    assert run_exists is not None
    assert job_exists is not None


async def test_same_occurred_at_cursor_is_stable(engine: AsyncEngine) -> None:
    """同一 ``occurred_at`` 的多行:游标按 (occurred_at, id) 去重且倒序稳定。"""

    stamp = datetime(2026, 2, 1, tzinfo=UTC)
    async with session_factory(engine)() as session:
        repo = ResearchAuditRepository(session)
        for _ in range(4):
            await repo.append(
                audit_id=f"RA-{uuid4().hex[:16]}",
                occurred_at=stamp,
                actor="user:api",
                actor_type="human",
                source="web",
                operation="run.submit",
                outcome="succeeded",
                params_digest="0" * 64,
            )
        await session.commit()

    async with session_factory(engine)() as session:
        repo = ResearchAuditRepository(session)
        first = await repo.list_events(limit=2)
        cursor = (first[1].occurred_at, first[1].id)
        second = await repo.list_events(limit=2, cursor=cursor)
        assert [row.id for row in first[:2]] == sorted(
            [row.id for row in first[:2]], reverse=True
        )
        assert {row.id for row in first[:2]}.isdisjoint({row.id for row in second})


async def test_purge_dry_run_and_delete(engine: AsyncEngine) -> None:
    await _insert_events(engine, 3)
    sm = session_factory(engine)
    async with sm() as session:
        repo = ResearchAuditRepository(session)
        before = datetime(2026, 1, 2, tzinfo=UTC)
        counted = await repo.purge_before(before, dry_run=True)
        assert counted == 3
        assert await _count(engine, ResearchAuditEventModel) == 3
        deleted = await repo.purge_before(before, dry_run=False)
        await session.commit()
    assert deleted == 3
    assert await _count(engine, ResearchAuditEventModel) == 0


# --------------------------------------------------------------------------- #
# 评审回归:终止态取消、worker 身份、脱敏边界、去重竞态、CLI/工具 smoke
# --------------------------------------------------------------------------- #


async def _insert_job(
    engine: AsyncEngine,
    *,
    status: str,
    kind: str = "research_run",
    payload: dict[str, Any] | None = None,
) -> str:
    job_id = f"BJ-{uuid4().hex[:20]}"
    async with session_factory(engine)() as session:
        session.add(
            BackgroundJobModel(
                job_id=job_id,
                kind=kind,
                queue="research",
                status=status,
                payload=payload or {"run_id": "RR-cancel-audit"},
                payload_checksum="0" * 64,
                idempotency_key=f"k-{uuid4().hex[:20]}",
                requested_by="user:api",
            )
        )
        await session.commit()
    return job_id


@pytest_asyncio.fixture
async def jobs_client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    sm = session_factory(engine)
    app = FastAPI()
    app.include_router(jobs_router)

    async def _dependency() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    app.dependency_overrides[get_db_session] = _dependency
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_cancel_terminal_job_rest_is_noop_with_audit(
    engine: AsyncEngine, jobs_client: AsyncClient, audit: ResearchAuditRecorder
) -> None:
    """B1:终态 research_run 任务再取消 → 200 当前状态 + not_running 审计(不 500)。"""

    job_id = await _insert_job(engine, status="succeeded")
    response = await jobs_client.post(
        f"/api/jobs/{job_id}/cancel", json={"reason": "already done"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"

    rows = [
        row
        for row in await _audit_rows(engine)
        if row.operation == "job.cancel" and row.entity_id == job_id
    ]
    assert len(rows) == 1
    assert rows[0].outcome == "rejected"
    assert rows[0].result_code == "not_running"
    assert rows[0].correlation["run_id"] == "RR-cancel-audit"
    assert rows[0].params_summary["kind"] == "research_run"


async def test_cancel_terminal_job_mcp_is_noop_with_audit(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    """B1(MCP 孪生):rollback 后不再读过期 ORM,返回 ok + not_running 审计。"""

    from finboard_mcp.audit import AuditRecorder
    from finboard_mcp.tools import jobs as job_tools

    job_id = await _insert_job(engine, status="succeeded")
    app = SimpleNamespace(
        write_tools_enabled=True,
        session_maker=session_factory(engine),
        audit=AuditRecorder(),
    )
    envelope = await job_tools.job_cancel(cast(McpAppContext, app), job_id)
    assert envelope.status == "ok", envelope.error
    assert envelope.data["status"] == "succeeded"

    rows = [
        row
        for row in await _audit_rows(engine)
        if row.operation == "job.cancel" and row.entity_id == job_id
    ]
    assert len(rows) == 1
    assert rows[0].outcome == "rejected"
    assert rows[0].result_code == "not_running"


async def test_worker_terminal_audit_identity(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    """worker 终态:actor=worker:<executor kind>、actor_type=worker、带 checksum。"""

    from finboard_backtest.background_jobs.executors.research_run import (
        ResearchRunExecutor,
    )

    sink = ResearchAuditWorkerSink(audit, worker_id="worker-process-1")
    executor = ResearchRunExecutor(
        session_maker=session_factory(engine),
        store_factory=lambda _session: None,  # type: ignore[arg-type,return-value]
        adapter_factory=lambda _manifest: None,  # type: ignore[arg-type,return-value]
        audit=sink,
    )
    await executor._record_terminal(
        run_id="RR-worker-audit",
        job_id="BJ-worker-audit",
        status="succeeded",
        result_code="succeeded",
        result_checksum="checksum-worker",
    )

    row = next(
        row
        for row in await _audit_rows(engine)
        if row.operation == "run.worker.finished"
    )
    assert row.actor == "worker:research_run"
    assert row.actor_type == "worker"
    assert row.source == "worker"
    assert row.correlation["run_id"] == "RR-worker-audit"
    assert row.correlation["job_id"] == "BJ-worker-audit"
    assert row.correlation["result_checksum"] == "checksum-worker"
    assert row.correlation["worker_process_id"] == "worker-process-1"


async def test_worker_unseal_audit_identity(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    """worker 揭盲成功:actor=worker:validation_experiment,带 experiment/job。"""

    from finboard_backtest.background_jobs.executors.validation_experiment import (
        ValidationExperimentExecutor,
    )

    async def _never_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("runner factory must not be called")

    sink = ResearchAuditWorkerSink(audit, worker_id="worker-process-1")
    executor = ValidationExperimentExecutor(
        session_maker=session_factory(engine),
        runner_factory=_never_called,
        audit=sink,
    )
    await executor._record_unseal(
        experiment_id="EXP-worker-audit",
        job_id="BJ-worker-audit",
        outcome="succeeded",
        result_code="unsealed",
    )
    row = next(
        row
        for row in await _audit_rows(engine)
        if row.operation == "experiment.unseal"
    )
    assert row.actor == "worker:validation_experiment"
    assert row.actor_type == "worker"
    assert row.correlation["experiment_id"] == "EXP-worker-audit"
    assert row.correlation["job_id"] == "BJ-worker-audit"
    assert row.result_code == "unsealed"


async def test_correlation_is_redacted(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    async with session_factory(engine)() as session:
        await audit.record(
            session,
            ResearchAuditEvent(
                operation="run.submit",
                outcome="succeeded",
                context=ResearchOperationContext.for_web(actor="user:api"),
                correlation={
                    "run_id": "RR-x",
                    "note": "Bearer abc.def-1234567890",
                    "token": "token=plain-secret",
                },
            ),
        )
        await session.commit()
    row = (await _audit_rows(engine))[0]
    assert row.correlation["run_id"] == "RR-x"
    assert "abc.def" not in str(row.correlation["note"])
    assert "plain-secret" not in str(row.correlation["token"])


async def test_body_and_code_never_stored(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    async with session_factory(engine)() as session:
        await audit.record(
            session,
            ResearchAuditEvent(
                operation="research_code.submit",
                outcome="succeeded",
                context=ResearchOperationContext.for_mcp(client_id="MC-x"),
                params_summary={
                    "code": "def alpha():\n    return 1",
                    "content": "研究记忆正文不可入库",
                    "files": {"factor.py": "import os\nos.system('x')"},
                    "note": "import pandas as pd\nprint('code')",
                    "title": "正常短标题",
                },
            ),
        )
        await session.commit()
    stored = (await _audit_rows(engine))[0].params_summary
    for key in ("code", "content", "files", "note"):
        assert stored[key] == "***OMITTED***"
    assert stored["title"] == "正常短标题"
    assert "os.system" not in str(stored)
    assert "研究记忆正文" not in str(stored)


async def test_summary_budget_includes_metadata(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    import json

    summary = {f"k{index:03d}": "v" * 110 for index in range(120)}
    async with session_factory(engine)() as session:
        await audit.record(
            session,
            ResearchAuditEvent(
                operation="run.submit",
                outcome="succeeded",
                context=ResearchOperationContext.for_web(actor="user:api"),
                params_summary=summary,
            ),
        )
        await session.commit()
    stored = (await _audit_rows(engine))[0].params_summary
    encoded = json.dumps(stored, ensure_ascii=True, sort_keys=True)
    assert len(encoded.encode("utf-8")) <= 4096
    assert stored["_truncated"] is True
    assert int(cast(int, stored["_omitted_keys"])) > 0


async def test_client_event_dedupe_survives_race(
    engine: AsyncEngine, audit: ResearchAuditRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发重复:先查后插竞态撞唯一索引 → 回滚后读既有行,不 500。"""

    from finboard_persistence.research_audit_repo import ResearchAuditRepository as Repo

    context = ResearchOperationContext.for_mcp(client_id="MC-race")
    sm = session_factory(engine)
    async with sm() as session:
        first, created = await audit.record_client_declared(
            session,
            context=context,
            client_event_id="ce-race-1",
            operation="client.tool_call",
            outcome="succeeded",
            params_summary={"tool_name": "finboard_topic_write"},
        )
        await session.commit()
    assert created is True

    original = Repo.find_client_event
    calls = {"n": 0}

    async def _race(self: Repo, *, client_id: str, client_event_id: str) -> Any:
        # 模拟竞态:第一次查重扑空(另一请求尚未提交),插入时才撞唯一索引;
        # 冲突后的第二次查重走真实实现并返回既有行。
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await original(self, client_id=client_id, client_event_id=client_event_id)

    monkeypatch.setattr(Repo, "find_client_event", _race)
    async with sm() as session:
        second, created_again = await audit.record_client_declared(
            session,
            context=context,
            client_event_id="ce-race-1",
            operation="client.tool_call",
            outcome="succeeded",
            params_summary={"tool_name": "finboard_topic_write"},
        )
        await session.commit()
    monkeypatch.setattr(Repo, "find_client_event", original)
    assert created_again is False
    assert first is not None
    assert second is not None
    assert second.audit_id == first.audit_id
    assert await _count(engine, ResearchAuditEventModel) == 1


async def test_cli_retention_flags_and_defaults() -> None:
    """CLI flags smoke:用 Click 参数对象断言选项/默认值(不依赖 rich 文本渲染)。"""

    from click import Group
    from typer.main import get_command
    from typer.testing import CliRunner

    from finboard_app.cli import app as cli_app
    from finboard_app.config import Settings

    # CI 的 rich 渲染会按终端宽度换行/加 ANSI,不能从 --help 文本里找字面量;
    # 直接从 Click 命令对象读取参数。
    cli_group = cast(Group, get_command(cli_app))
    research_group = cast(Group, cli_group.commands["research"])
    audit_group = cast(Group, research_group.commands["audit"])
    purge = audit_group.commands["purge"]
    opts = {
        opt
        for param in purge.params
        for opt in (*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ()))
    }
    assert "--before" in opts
    assert "--dry-run" in opts
    assert "--no-dry-run" in opts
    dry_run = next(param for param in purge.params if param.name == "dry_run")
    assert dry_run.default is True

    # 帮助入口只做可执行 smoke(不解析渲染文本)。
    result = CliRunner().invoke(
        cli_app,
        ["research", "audit", "purge", "--help"],
        env={"COLUMNS": "200", "TERM": "dumb"},
    )
    assert result.exit_code == 0, result.output

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.research_audit_retention_days == 365
    assert settings.research_audit_client_events is False


async def test_research_code_submit_audit_smoke(
    engine: AsyncEngine, audit: ResearchAuditRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP research_code.submit smoke:成功写与审计同事务,正文不入库。"""

    from finboard_mcp.audit import AuditRecorder
    from finboard_mcp.tools import research_code

    class _FakeService:
        def submit(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "commit": "c" * 40,
                "path": "factors/alpha",
                "checksum": "d" * 64,
            }

    class _FakeRepo:
        def __init__(self, _session: Any) -> None:
            pass

        async def register(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                artifact_id="RC-smoke", status="draft", promotion_status="pending"
            )

    monkeypatch.setattr(research_code, "_service", lambda _app: _FakeService())
    monkeypatch.setattr(research_code, "ResearchCodeArtifactRepository", _FakeRepo)
    app = SimpleNamespace(
        write_tools_enabled=True,
        session_maker=session_factory(engine),
        audit=AuditRecorder(),
        settings=SimpleNamespace(
            research_code_repo_path="unused",
            research_code_max_files=32,
            research_code_max_file_bytes=262144,
        ),
    )
    envelope = await research_code.submit(
        cast(McpAppContext, app),
        kind="factor",
        name="alpha",
        files={"factor.py": "def alpha(): pass"},
    )
    assert envelope.status == "ok", envelope.error
    row = next(
        row
        for row in await _audit_rows(engine)
        if row.operation == "research_code.submit"
    )
    assert row.outcome == "succeeded"
    assert row.entity_type == "research_code_artifact"
    assert row.entity_id == "RC-smoke"
    assert row.source == "mcp"
    assert "def alpha" not in str(row.params_summary)
