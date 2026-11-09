"""Topic-initiated ResearchRun links, idempotency and transaction boundaries (#515).

The fixture seeds the #513 submission scenario once (fixed strategy/release IDs),
then each test clears only the business tables it is allowed to touch. The topic
lock and one-commit contract are exercised against the real PostgreSQL test DB;
no live trading table is written anywhere in this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_app.research_audit import (
    ResearchAuditEvent,
    ResearchAuditRecorder,
    configure_research_audit_recorder,
    reset_research_audit_recorder,
)
from finboard_app.research_operation import (
    ResearchConflictError,
    ResearchOperationContext,
    ResearchPreconditionFailedError,
)
from finboard_app.research_run_submission import (
    ResearchRunSubmissionService,
    ResearchRunSubmitInput,
)
from finboard_app.research_topic_actions import (
    cancel_topic_run,
    get_topic_run,
    link_run_to_topic,
    list_run_topics,
    list_topic_runs,
    retry_topic_run,
    start_topic_run,
)
from finboard_app.research_workspace import workspace_read, workspace_write
from finboard_persistence import session_factory
from finboard_persistence.models import (
    BackgroundJobModel,
    ResearchAuditEventModel,
    ResearchRunModel,
    ResearchTopicEntryModel,
    ResearchTopicLinkModel,
    ResearchTopicModel,
)
from finboard_persistence.research_topic_link_repo import (
    ResearchTopicLinkConflictError,
    ResearchTopicLinkRepository,
)
from tests.integration.conftest import clean_tables
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
        "title": "课题发起研究运行",
        "question": "运行、任务与产物能否同事务关联?",
        "goal": dict(GOAL),
        **overrides,
    }


_SEEDED = False
_SEED_LOCK = asyncio.Lock()


async def _ensure_seed(engine: AsyncEngine) -> None:
    global _SEEDED
    async with _SEED_LOCK:
        if _SEEDED:
            return
        async with engine.begin() as conn:
            await clean_tables(conn)
        await _seed(engine)
        _SEEDED = True


@pytest_asyncio.fixture(scope="module")
async def engine(_engine: AsyncEngine) -> AsyncEngine:
    return _engine


async def _clean_business(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for model in (
            ResearchTopicLinkModel,
            ResearchTopicEntryModel,
            ResearchTopicModel,
            ResearchAuditEventModel,
            BackgroundJobModel,
            ResearchRunModel,
        ):
            await conn.execute(delete(model))


@pytest_asyncio.fixture(autouse=True)
async def _isolate(engine: AsyncEngine) -> AsyncIterator[None]:
    await _ensure_seed(engine)
    await _clean_business(engine)
    yield
    await _clean_business(engine)
    reset_research_audit_recorder()


@pytest_asyncio.fixture
async def audit(engine: AsyncEngine) -> ResearchAuditRecorder:
    recorder = ResearchAuditRecorder(session_maker=session_factory(engine))
    configure_research_audit_recorder(recorder)
    return recorder


@pytest_asyncio.fixture
async def client(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> AsyncIterator[httpx.AsyncClient]:
    from finboard_api.deps import get_db_session
    from finboard_api.routes.research_runs import router as research_runs_router
    from finboard_api.routes.research_topic_runs import (
        run_links_router,
        topic_runs_router,
    )
    from finboard_api.routes.research_topics import router as research_topics_router

    smaker = session_factory(engine)
    app = FastAPI()
    app.state.settings = SimpleNamespace(research_sandbox_enabled=False)
    app.include_router(research_topics_router)
    app.include_router(topic_runs_router)
    app.include_router(run_links_router)
    app.include_router(research_runs_router)

    async def _override() -> Any:
        async with smaker() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def _create_topic(
    engine: AsyncEngine, *, title: str = "课题发起研究运行", goal: dict[str, Any] | None = GOAL
) -> dict[str, Any]:
    async with session_factory(engine)() as session:
        payload = _topic_payload(title=title)
        if goal is None:
            payload.pop("goal")
            record = await workspace_write(
                session, actor="user:api", operation="draft_create", payload=payload
            )
        else:
            record = await workspace_write(
                session, actor="user:api", operation="create", payload=payload
            )
    # Topic.create itself is audited (#518); start from a clean audit slate so
    # each test asserts only the topic-run events it produces.
    async with engine.begin() as conn:
        await conn.execute(delete(ResearchAuditEventModel))
    return record


def _web_ctx(operation_id: str | None = None, *, actor: str = "user:api") -> ResearchOperationContext:
    return ResearchOperationContext.for_web(actor=actor, operation_id=operation_id)


async def _count(engine: AsyncEngine, model: Any) -> int:
    async with session_factory(engine)() as session:
        return int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )


async def _counts(engine: AsyncEngine) -> dict[str, int]:
    return {
        "links": await _count(engine, ResearchTopicLinkModel),
        "runs": await _count(engine, ResearchRunModel),
        "jobs": await _count(engine, BackgroundJobModel),
        "audits": await _count(engine, ResearchAuditEventModel),
    }


async def _audit_rows(
    engine: AsyncEngine, operation: str | None = None
) -> list[ResearchAuditEventModel]:
    async with session_factory(engine)() as session:
        stmt = select(ResearchAuditEventModel).order_by(ResearchAuditEventModel.id)
        if operation is not None:
            stmt = stmt.where(ResearchAuditEventModel.operation == operation)
        return list((await session.execute(stmt)).scalars())


async def _set_run_status(engine: AsyncEngine, run_id: str, status: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE research_runs SET status=:status, error_code=:error, "
                "error_summary=:summary, result_checksum=:checksum WHERE run_id=:run_id"
            ),
            {
                "status": status,
                "error": "boom" if status == "failed" else None,
                "summary": "执行失败" if status == "failed" else None,
                "checksum": "c" * 64 if status == "completed" else None,
                "run_id": run_id,
            },
        )


async def _set_job_status(
    engine: AsyncEngine,
    job_id: str,
    status: str,
    *,
    done: int = 0,
    total: int = 0,
    error_summary: str | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE background_jobs SET status=:status, progress_done=:done, "
                "progress_total=:total, error_summary=:error WHERE job_id=:job_id"
            ),
            {
                "status": status,
                "done": done,
                "total": total,
                "error": error_summary,
                "job_id": job_id,
            },
        )


async def _start(
    engine: AsyncEngine,
    topic_id: str,
    *,
    key: str,
    operation_id: str | None,
    goal_version: str = "v1",
    **overrides: Any,
) -> dict[str, Any]:
    async with session_factory(engine)() as session:
        return await start_topic_run(
            session,
            topic_id=topic_id,
            goal_version=goal_version,
            payload=_payload(key, **overrides),
            ctx=_web_ctx(operation_id),
        )


# --------------------------------------------------------------------------- #
# 同事务 / 原子性
# --------------------------------------------------------------------------- #


async def test_start_links_run_job_and_topic_in_one_transaction(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    topic = await _create_topic(engine)
    result = await _start(
        engine, topic["topic_id"], key="topic-run-tx-1", operation_id="OP-topic-1"
    )

    assert result["created"] is True
    run_id = result["run_id"]
    job_id = result["job_id"]
    assert run_id.startswith("RR-")
    assert job_id is not None
    assert job_id.startswith("BJ-")
    assert result["link"]["state"] == "queued"
    assert result["link"]["topic_id"] == topic["topic_id"]
    assert result["link"]["goal_version"] == "v1"
    assert result["link"]["operation_id"] == "OP-topic-1"

    # run / job / link all exist and point at each other.
    async with session_factory(engine)() as session:
        run = await session.scalar(
            select(ResearchRunModel).where(ResearchRunModel.run_id == run_id)
        )
        link = await session.scalar(select(ResearchTopicLinkModel))
        job = await session.scalar(
            select(BackgroundJobModel).where(BackgroundJobModel.job_id == job_id)
        )
    assert run is not None
    assert job is not None
    assert link is not None
    assert run.job_id == job_id
    assert link.entity_id == run_id
    assert link.job_id == job_id
    assert link.operation_id == "OP-topic-1"

    rows = await _audit_rows(engine, "topic.run.start")
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome == "succeeded"
    assert row.topic_id == topic["topic_id"]
    assert row.entity_type == "research_run"
    assert row.entity_id == run_id
    assert row.correlation["topic_id"] == topic["topic_id"]
    assert row.correlation["run_id"] == run_id
    assert row.correlation["job_id"] == job_id
    assert row.correlation["operation_id"] == "OP-topic-1"
    # The #513 run.submit audit is still written in the same transaction.
    assert len(await _audit_rows(engine, "run.submit")) == 1

    listing = await _read(engine, topic["topic_id"])
    assert listing["items"][0]["run_id"] == run_id
    assert listing["items"][0]["state"] == "queued"
    detail = await _detail(engine, topic["topic_id"], run_id)
    assert detail["job_id"] == job_id
    assert detail["deep_link"].endswith(run_id)
    reverse = await _reverse(engine, run_id)
    assert reverse["count"] == 1
    assert reverse["items"][0]["topic_id"] == topic["topic_id"]
    async with session_factory(engine)() as session:
        context = await workspace_read(
            session, topic_id=topic["topic_id"], view="context", refs=0
        )
    assert context["active_links"] == [
        {
            "entity_type": "research_run",
            "entity_id": run_id,
            "job_id": job_id,
            "state": "queued",
            "goal_version": "v1",
        }
    ]


async def _read(engine: AsyncEngine, topic_id: str) -> dict[str, Any]:
    async with session_factory(engine)() as session:
        return await list_topic_runs(session, topic_id)


async def _detail(engine: AsyncEngine, topic_id: str, run_id: str) -> dict[str, Any]:
    async with session_factory(engine)() as session:
        return await get_topic_run(session, topic_id, run_id)


async def _reverse(engine: AsyncEngine, run_id: str) -> dict[str, Any]:
    async with session_factory(engine)() as session:
        return await list_run_topics(session, run_id)


async def test_link_insert_conflict_rolls_back_run_and_job(
    engine: AsyncEngine, audit: ResearchAuditRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic = await _create_topic(engine)

    async def _conflict(*_args: Any, **_kwargs: Any) -> Any:
        raise ResearchTopicLinkConflictError("模拟链接唯一冲突")

    monkeypatch.setattr(ResearchTopicLinkRepository, "create_or_get", _conflict)
    with pytest.raises(ResearchConflictError) as raised:
        await _start(
            engine,
            topic["topic_id"],
            key="topic-run-link-conflict",
            operation_id="OP-link-conflict",
        )
    assert raised.value.code == "link_conflict"
    # The failed transaction left no run, job or link behind.
    assert await _counts(engine) == {"links": 0, "runs": 0, "jobs": 0, "audits": 1}
    rejected = await _audit_rows(engine, "topic.run.start")
    assert len(rejected) == 1
    assert rejected[0].outcome == "rejected"


async def test_job_creation_failure_rolls_back_run_and_link(
    engine: AsyncEngine, audit: ResearchAuditRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic = await _create_topic(engine)

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("后台任务插入失败")

    monkeypatch.setattr(
        ResearchRunSubmissionService, "_enqueue_research_run_job", _boom
    )
    with pytest.raises(RuntimeError, match="后台任务插入失败"):
        await _start(
            engine,
            topic["topic_id"],
            key="topic-run-job-failure",
            operation_id="OP-job-failure",
        )
    counts = await _counts(engine)
    assert counts["links"] == 0
    assert counts["runs"] == 0
    assert counts["jobs"] == 0
    # Both the #513 run.submit and #515 topic.run.start detached audits exist.
    assert counts["audits"] == 2
    assert {row.outcome for row in await _audit_rows(engine)} == {"failed"}


async def test_audit_failure_rolls_back_everything(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic = await _create_topic(engine)

    class _TopicAuditDown(ResearchAuditRecorder):
        async def record(
            self, session: AsyncSession, event: ResearchAuditEvent
        ) -> None:
            if event.operation == "topic.run.start":
                raise RuntimeError("审计写入失败")
            await super().record(session, event)

    configure_research_audit_recorder(
        _TopicAuditDown(session_maker=session_factory(engine))
    )
    with pytest.raises(RuntimeError, match="审计写入失败"):
        await _start(
            engine,
            topic["topic_id"],
            key="topic-run-audit-failure",
            operation_id="OP-audit-failure",
        )
    counts = await _counts(engine)
    assert counts["links"] == 0
    assert counts["runs"] == 0
    assert counts["jobs"] == 0
    rows = await _audit_rows(engine)
    # Only the detached failure record survives; the in-transaction success
    # rows (run.submit) rolled back with the business transaction.
    assert [row.operation for row in rows] == ["topic.run.start"]
    assert rows[0].outcome == "failed"
    assert rows[0].error_summary is not None
    assert "审计写入失败" in rows[0].error_summary


# --------------------------------------------------------------------------- #
# 幂等与并发
# --------------------------------------------------------------------------- #


async def test_same_operation_id_returns_original_run(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    topic = await _create_topic(engine)
    first = await _start(
        engine, topic["topic_id"], key="topic-run-idem-1", operation_id="OP-idem"
    )
    # Same operation id with a brand-new run idempotency key: the link anchor
    # wins and the original RR-/BJ- is returned without a second run.
    second = await _start(
        engine, topic["topic_id"], key="topic-run-idem-other", operation_id="OP-idem"
    )
    assert first["created"] is True
    assert second["created"] is False
    assert second["run_id"] == first["run_id"]
    assert second["job_id"] == first["job_id"]
    counts = await _counts(engine)
    assert counts["links"] == 1
    assert counts["runs"] == 1
    assert counts["jobs"] == 1
    results = [row.result_code for row in await _audit_rows(engine, "topic.run.start")]
    assert results == ["created", "idempotent_hit"]


async def test_same_operation_id_across_topics_conflicts(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    first_topic = await _create_topic(engine, title="课题 A")
    second_topic = await _create_topic(engine, title="课题 B")
    await _start(
        engine, first_topic["topic_id"], key="topic-run-cross-1", operation_id="OP-cross"
    )
    with pytest.raises(ResearchConflictError) as raised:
        await _start(
            engine,
            second_topic["topic_id"],
            key="topic-run-cross-2",
            operation_id="OP-cross",
        )
    assert raised.value.code == "operation_id_bound"
    assert await _count(engine, ResearchTopicLinkModel) == 1
    assert await _count(engine, ResearchRunModel) == 1


async def test_concurrent_different_operation_ids_create_two_links(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    topic = await _create_topic(engine)

    async def _run(key: str, operation_id: str) -> dict[str, Any]:
        async with session_factory(engine)() as session:
            return await start_topic_run(
                session,
                topic_id=topic["topic_id"],
                goal_version="v1",
                payload=_payload(key),
                ctx=_web_ctx(operation_id),
            )

    results = await asyncio.gather(
        _run("topic-run-concurrent-a", "OP-concurrent-a"),
        _run("topic-run-concurrent-b", "OP-concurrent-b"),
    )
    assert all(result["created"] for result in results)
    assert len({result["run_id"] for result in results}) == 2
    counts = await _counts(engine)
    assert counts["links"] == 2
    assert counts["runs"] == 2
    assert counts["jobs"] == 2


async def test_concurrent_same_operation_id_returns_one_link(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    topic = await _create_topic(engine)

    async def _run(key: str) -> dict[str, Any]:
        async with session_factory(engine)() as session:
            return await start_topic_run(
                session,
                topic_id=topic["topic_id"],
                goal_version="v1",
                payload=_payload(key),
                ctx=_web_ctx("OP-concurrent-same"),
            )

    results = await asyncio.gather(
        _run("topic-run-same-a"), _run("topic-run-same-b")
    )
    assert {result["run_id"] for result in results} == {results[0]["run_id"]}
    assert sorted(result["created"] for result in results) == [False, True]
    counts = await _counts(engine)
    assert counts["links"] == 1
    assert counts["runs"] == 1
    assert counts["jobs"] == 1


async def test_concurrent_same_operation_id_across_topics_rolls_back_loser(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    """The partial unique index, not the pre-check, decides a cross-topic race."""

    first_topic = await _create_topic(engine, title="并发课题 A")
    second_topic = await _create_topic(engine, title="并发课题 B")

    async def _run(topic_id: str, key: str) -> dict[str, Any]:
        async with session_factory(engine)() as session:
            return await start_topic_run(
                session,
                topic_id=topic_id,
                goal_version="v1",
                payload=_payload(key),
                ctx=_web_ctx("OP-cross-race"),
            )

    results = await asyncio.gather(
        _run(first_topic["topic_id"], "topic-run-race-a"),
        _run(second_topic["topic_id"], "topic-run-race-b"),
        return_exceptions=True,
    )
    successes = [item for item in results if isinstance(item, dict)]
    failures = [item for item in results if isinstance(item, ResearchConflictError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code in {"operation_id_bound", "link_conflict"}
    counts = await _counts(engine)
    assert counts["links"] == 1
    assert counts["runs"] == 1
    assert counts["jobs"] == 1



# --------------------------------------------------------------------------- #
# 读侧 join 派生状态
# --------------------------------------------------------------------------- #


async def test_read_state_is_derived_and_duplicate_events_do_not_add_links(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    topic = await _create_topic(engine)
    started = await _start(
        engine, topic["topic_id"], key="topic-run-read", operation_id="OP-read"
    )
    run_id, job_id = started["run_id"], started["job_id"]
    assert started["link"]["state"] == "queued"

    await _set_run_status(engine, run_id, "running")
    await _set_job_status(engine, job_id, "running", done=3, total=10)
    running = (await _read(engine, topic["topic_id"]))["items"][0]
    assert running["state"] == "running"
    assert (running["progress_done"], running["progress_total"]) == (3, 10)

    await _set_run_status(engine, run_id, "completed")
    await _set_job_status(engine, job_id, "succeeded", done=10, total=10)
    completed = (await _read(engine, topic["topic_id"]))["items"][0]
    assert completed["state"] == "succeeded"
    assert completed["result_checksum"] == "c" * 64

    # Worker restart / duplicate completion event: replaying the same terminal
    # updates must not touch the link ledger.
    await _set_run_status(engine, run_id, "completed")
    await _set_job_status(engine, job_id, "succeeded", done=10, total=10)
    again = (await _read(engine, topic["topic_id"]))["items"]
    assert len(again) == 1
    assert await _count(engine, ResearchTopicLinkModel) == 1

    await _set_run_status(engine, run_id, "interrupted")
    await _set_job_status(engine, job_id, "interrupted")
    assert (await _read(engine, topic["topic_id"]))["items"][0]["state"] == "interrupted"


# --------------------------------------------------------------------------- #
# 重试 / 取消 / 后来关联 / 多课题
# --------------------------------------------------------------------------- #


async def test_retry_creates_replay_run_and_keeps_original_link(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    topic = await _create_topic(engine)
    original = await _start(
        engine, topic["topic_id"], key="topic-run-retry-original", operation_id="OP-retry-1"
    )
    await _set_run_status(engine, original["run_id"], "interrupted")
    await _set_job_status(engine, original["job_id"], "interrupted")

    async with session_factory(engine)() as session:
        retried = await retry_topic_run(
            session,
            topic_id=topic["topic_id"],
            run_id=original["run_id"],
            payload={
                "idempotency_key": "topic-run-retry-replay",
                "requested_by": "integration-test",
            },
            ctx=_web_ctx("OP-retry-2"),
        )
    assert retried["created"] is True
    assert retried["run_id"] != original["run_id"]
    async with session_factory(engine)() as session:
        new_run = await session.scalar(
            select(ResearchRunModel).where(
                ResearchRunModel.run_id == retried["run_id"]
            )
        )
    assert new_run is not None
    assert new_run.replay_of_run_id == original["run_id"]
    assert await _count(engine, ResearchTopicLinkModel) == 2
    assert await _count(engine, ResearchRunModel) == 2

    # Lost receipt on retry: same operation id returns the same replay run.
    async with session_factory(engine)() as session:
        replay_again = await retry_topic_run(
            session,
            topic_id=topic["topic_id"],
            run_id=original["run_id"],
            payload={
                "idempotency_key": "topic-run-retry-other",
                "requested_by": "integration-test",
            },
            ctx=_web_ctx("OP-retry-2"),
        )
    assert replay_again["created"] is False
    assert replay_again["run_id"] == retried["run_id"]
    assert await _count(engine, ResearchTopicLinkModel) == 2
    rows = await _audit_rows(engine, "topic.run.retry")
    assert len(rows) == 2
    assert all(row.correlation["replay_of_run_id"] == original["run_id"] for row in rows)


async def test_retry_requires_new_operation_id(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    topic = await _create_topic(engine)
    original = await _start(
        engine, topic["topic_id"], key="topic-run-retry-noop", operation_id="OP-retry-original"
    )
    await _set_run_status(engine, original["run_id"], "interrupted")
    async with session_factory(engine)() as session:
        with pytest.raises(ResearchConflictError) as raised:
            await retry_topic_run(
                session,
                topic_id=topic["topic_id"],
                run_id=original["run_id"],
                payload={
                    "idempotency_key": "topic-run-retry-new-key",
                    "requested_by": "integration-test",
                },
                ctx=_web_ctx("OP-retry-original"),
            )
    assert raised.value.code == "operation_id_bound"


async def test_cancel_terminal_run_returns_conflict(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    topic = await _create_topic(engine)
    started = await _start(
        engine, topic["topic_id"], key="topic-run-cancel", operation_id="OP-cancel"
    )
    async with session_factory(engine)() as session:
        cancelled = await cancel_topic_run(
            session,
            topic_id=topic["topic_id"],
            run_id=started["run_id"],
            ctx=_web_ctx(),
        )
    assert cancelled["state"] == "cancelled"
    assert cancelled["run_status"] == "cancelled"
    async with session_factory(engine)() as session:
        with pytest.raises(ResearchConflictError) as raised:
            await cancel_topic_run(
                session,
                topic_id=topic["topic_id"],
                run_id=started["run_id"],
                ctx=_web_ctx(),
            )
    assert raised.value.code == "run_terminal"
    cancel_audits = await _audit_rows(engine, "topic.run.cancel")
    assert [row.outcome for row in cancel_audits] == ["succeeded", "rejected"]


async def test_late_link_is_idempotent_and_run_can_have_two_topics(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    # Standalone run through the legacy #513 entry: no topic link is created.
    async with session_factory(engine)() as session:
        outcome = await ResearchRunSubmissionService(session).submit(
            ResearchRunSubmitInput.from_json_payload(_payload("topic-run-late")),
            _web_ctx("OP-late-run"),
        )
        await session.commit()
    run_id = outcome.run_row.run_id
    assert await _count(engine, ResearchTopicLinkModel) == 0

    first_topic = await _create_topic(engine, title="后来关联 A")
    second_topic = await _create_topic(engine, title="后来关联 B")
    async with session_factory(engine)() as session:
        first = await link_run_to_topic(
            session,
            run_id=run_id,
            topic_id=first_topic["topic_id"],
            goal_version="v1",
            operation_id="OP-late-link",
            ctx=_web_ctx("OP-late-link"),
        )
    assert first["created"] is True
    async with session_factory(engine)() as session:
        again = await link_run_to_topic(
            session,
            run_id=run_id,
            topic_id=first_topic["topic_id"],
            goal_version="v1",
            operation_id="OP-late-link",
            ctx=_web_ctx("OP-late-link"),
        )
    assert again["created"] is False
    assert again["link_id"] == first["link_id"]
    assert await _count(engine, ResearchTopicLinkModel) == 1

    async with session_factory(engine)() as session:
        second = await link_run_to_topic(
            session,
            run_id=run_id,
            topic_id=second_topic["topic_id"],
            goal_version="v1",
            operation_id="OP-late-link-2",
            ctx=_web_ctx("OP-late-link-2"),
        )
    assert second["created"] is True
    reverse = await _reverse(engine, run_id)
    assert reverse["count"] == 2
    assert {item["topic_id"] for item in reverse["items"]} == {
        first_topic["topic_id"],
        second_topic["topic_id"],
    }
    # The standalone run itself is untouched by linking.
    assert await _count(engine, ResearchRunModel) == 1


# --------------------------------------------------------------------------- #
# 拒绝路径 / 目标版本 / 旧入口
# --------------------------------------------------------------------------- #


async def test_inactive_topic_and_goal_mismatch_are_rejected_and_audited(
    engine: AsyncEngine, audit: ResearchAuditRecorder
) -> None:
    draft = await _create_topic(engine, title="草稿课题", goal=None)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE research_topics SET status='paused' WHERE topic_id=:id"),
            {"id": draft["topic_id"]},
        )

    with pytest.raises(ResearchPreconditionFailedError) as raised:
        await _start(
            engine, draft["topic_id"], key="topic-run-paused", operation_id="OP-paused"
        )
    assert raised.value.code == "topic_not_active"
    assert await _count(engine, ResearchTopicLinkModel) == 0
    rejected = await _audit_rows(engine, "topic.run.start")
    assert len(rejected) == 1
    assert rejected[0].outcome == "rejected"
    assert rejected[0].result_code == "topic_not_active"

    active = await _create_topic(engine, title="目标版本课题")
    with pytest.raises(ResearchPreconditionFailedError) as mismatch:
        await _start(
            engine,
            active["topic_id"],
            key="topic-run-goal-mismatch",
            operation_id="OP-goal",
            goal_version="v2",
        )
    assert mismatch.value.code == "goal_version_mismatch"
    assert mismatch.value.details["current"] == "v1"
    assert mismatch.value.details["declared"] == "v2"


async def test_legacy_standalone_rest_submission_is_unchanged(
    engine: AsyncEngine, client: httpx.AsyncClient, audit: ResearchAuditRecorder
) -> None:
    response = await client.post(
        "/api/research/runs", json=_payload("legacy-standalone-515")
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["run_id"].startswith("RR-")
    assert await _count(engine, ResearchTopicLinkModel) == 0
    # A standalone run is visible through the existing run endpoint and only
    # appears under a topic after an explicit late link.
    assert (
        await client.get(f"/api/research/runs/{body['run_id']}")
    ).status_code == 200


# --------------------------------------------------------------------------- #
# REST 契约
# --------------------------------------------------------------------------- #


async def test_rest_topic_run_lifecycle(
    engine: AsyncEngine, client: httpx.AsyncClient, audit: ResearchAuditRecorder
) -> None:
    topic = await _create_topic(engine)
    body = {**_payload("rest-topic-run-1"), "goal_version": "v1", "operation_id": "OP-rest-1"}
    started = await client.post(f"/api/research/topics/{topic['topic_id']}/runs", json=body)
    assert started.status_code == 201, started.text
    started_body = started.json()
    run_id = started_body["run_id"]
    job_id = started_body["job_id"]
    assert started_body["created"] is True

    replay = await client.post(
        f"/api/research/topics/{topic['topic_id']}/runs",
        json={**body, "idempotency_key": "rest-topic-run-2"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["run_id"] == run_id

    listing = await client.get(f"/api/research/topics/{topic['topic_id']}/runs")
    assert listing.status_code == 200, listing.text
    assert listing.json()["items"][0]["run_id"] == run_id
    detail = await client.get(
        f"/api/research/topics/{topic['topic_id']}/runs/{run_id}"
    )
    assert detail.status_code == 200
    assert detail.json()["job_id"] == job_id

    # Retry needs a replayable source (interrupted is the incident channel).
    await _set_run_status(engine, run_id, "interrupted")
    await _set_job_status(engine, job_id, "interrupted")
    retried = await client.post(
        f"/api/research/topics/{topic['topic_id']}/runs/{run_id}/retry",
        json={
            "idempotency_key": "rest-topic-run-retry",
            "requested_by": "integration-test",
            "operation_id": "OP-rest-retry",
        },
    )
    assert retried.status_code == 201, retried.text
    replay_run_id = retried.json()["run_id"]
    assert replay_run_id != run_id

    cancelled = await client.post(
        f"/api/research/topics/{topic['topic_id']}/runs/{replay_run_id}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    terminal = await client.post(
        f"/api/research/topics/{topic['topic_id']}/runs/{replay_run_id}/cancel"
    )
    assert terminal.status_code == 409
    assert terminal.json()["detail"]["code"] == "run_terminal"

    # Late link a standalone run and reverse-look it up.
    standalone = await client.post(
        "/api/research/runs", json=_payload("rest-standalone-late")
    )
    standalone_id = standalone.json()["run_id"]
    linked = await client.post(
        f"/api/research/runs/{standalone_id}/link",
        json={
            "topic_id": topic["topic_id"],
            "goal_version": "v1",
            "operation_id": "OP-rest-late",
        },
    )
    assert linked.status_code == 201, linked.text
    again = await client.post(
        f"/api/research/runs/{standalone_id}/link",
        json={
            "topic_id": topic["topic_id"],
            "goal_version": "v1",
            "operation_id": "OP-rest-late",
        },
    )
    assert again.status_code == 200
    reverse = await client.get(f"/api/research/runs/{standalone_id}/topics")
    assert reverse.status_code == 200
    assert reverse.json()["count"] == 1
