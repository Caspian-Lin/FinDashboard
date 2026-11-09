"""Research topic lifecycle, continuation context and precise lookups (#514)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_app.research_topic_lifecycle import (
    IdempotencyConflictError,
    IllegalTransitionError,
    TopicRevisionConflictError,
)
from finboard_app.research_workspace import workspace_read, workspace_write
from finboard_persistence.models import ResearchRunModel, ResearchTopicModel
from finboard_persistence.research_topic_repo import ResearchTopicRepository

GOAL: dict[str, Any] = {
    "version": "v1",
    "criteria": "年化15%,最大回撤20%",
    "source": {"kind": "document", "ref_id": "ROADMAP.md"},
}


def topic_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "小盘反转生命周期",
        "question": "收益能否在同源 OOS 下存活?",
        "goal": dict(GOAL),
        **overrides,
    }


def draft_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "先存问题的草稿",
        "question": "哪些证据还没有核验?",
        **overrides,
    }


def round_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "goal_version": "v1",
        "objective": "核查冻结输入",
        "action": "读指定运行",
        "rationale": "避免混源比较",
        "outcome": "completed",
        "confidence": "low",
        "branch": "main",
        "source_refs": [{"kind": "research_run", "ref_id": "RR-514-deleted"}],
        **overrides,
    }


async def draft(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
    return await workspace_write(
        session,
        actor="user:api",
        operation="draft_create",
        payload=draft_payload(**kwargs),
    )


async def activate(
    session: AsyncSession, topic_id: str, revision: int, goal: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await workspace_write(
        session,
        actor="user:api",
        operation="activate",
        topic_id=topic_id,
        expected_revision=revision,
        payload={"goal": goal or dict(GOAL)},
    )


async def transition(
    session: AsyncSession,
    topic_id: str,
    action: str,
    revision: int,
    reason: str = "",
) -> dict[str, Any]:
    return await workspace_write(
        session,
        actor="user:api",
        operation="transition",
        topic_id=topic_id,
        expected_revision=revision,
        payload={"action": action, "reason": reason},
    )


async def lifecycle_entries(session: AsyncSession, topic_id: str) -> list[dict[str, Any]]:
    page = await workspace_read(session, topic_id=topic_id, entries=True, limit=50)
    return [item for item in page["items"] if item.get("entry_type") == "lifecycle"]


async def test_full_lifecycle_chain_appends_entries(db_session: AsyncSession) -> None:
    created = await draft(db_session)
    assert created["status"] == "draft"
    assert created["goal"] is None
    assert created["created"] is True
    topic_id = created["topic_id"]

    active = await activate(db_session, topic_id, created["revision"])
    assert active["status"] == "active"
    assert active["goal"]["version"] == "v1"
    assert active["revision"] == created["revision"] + 1

    paused = await transition(db_session, topic_id, "pause", active["revision"])
    assert paused["status"] == "paused"
    resumed = await transition(db_session, topic_id, "resume", paused["revision"])
    assert resumed["status"] == "active"
    closed = await transition(db_session, topic_id, "close", resumed["revision"], "门槛已达成")
    assert closed["status"] == "closed"
    reopened = await transition(db_session, topic_id, "reopen", closed["revision"], "出现新证据")
    assert reopened["status"] == "active"
    assert reopened["revision"] == closed["revision"] + 1

    entries = await lifecycle_entries(db_session, topic_id)
    # Newest first: reopen, close, resume, pause, activate.
    assert [(e["from"], e["to"]) for e in reversed(entries)] == [
        ("draft", "active"),
        ("active", "paused"),
        ("paused", "active"),
        ("active", "closed"),
        ("closed", "active"),
    ]
    assert all(e["actor"] == "user:api" and e["at"] for e in entries)
    assert next(e for e in entries if e["to"] == "closed")["reason"] == "门槛已达成"

    # The column and the legacy JSON payload stay in sync.
    payload = await db_session.scalar(
        select(ResearchTopicModel.payload).where(ResearchTopicModel.topic_id == topic_id)
    )
    assert payload is not None
    assert payload["status"] == "active"
    detail = await workspace_read(db_session, topic_id=topic_id)
    assert detail["status"] == "active"
    assert detail["updated_at"]


async def test_discard_draft_and_illegal_transitions(db_session: AsyncSession) -> None:
    created = await draft(db_session)
    topic_id = created["topic_id"]
    with pytest.raises(IllegalTransitionError) as draft_pause:
        await transition(db_session, topic_id, "pause", created["revision"])
    assert draft_pause.value.detail["code"] == "illegal_transition"
    assert draft_pause.value.detail["current"] == "draft"
    assert draft_pause.value.detail["allowed"] == ["active", "closed"]

    with pytest.raises(IllegalTransitionError):
        await transition(db_session, topic_id, "resume", created["revision"])

    discarded = await transition(db_session, topic_id, "discard", created["revision"], "放弃草稿")
    assert discarded["status"] == "closed"
    with pytest.raises(IllegalTransitionError) as closed_pause:
        await transition(db_session, topic_id, "pause", discarded["revision"])
    assert closed_pause.value.detail["allowed"] == ["active"]
    # A discarded draft has no goal, so reopening it cannot produce an active topic.
    with pytest.raises(IllegalTransitionError, match="目标"):
        await transition(db_session, topic_id, "reopen", discarded["revision"], "重开")

    # Close/reopen/discard must carry a reviewable reason.
    full = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload()
    )
    with pytest.raises(ValueError, match="原因"):
        await transition(db_session, full["topic_id"], "close", full["revision"])
    paused = await transition(db_session, full["topic_id"], "pause", full["revision"])
    assert paused["status"] == "paused"
    closed = await transition(db_session, full["topic_id"], "close", paused["revision"], "门槛已达成")
    assert closed["status"] == "closed"
    reopened = await transition(db_session, full["topic_id"], "reopen", closed["revision"], "新证据")
    assert reopened["status"] == "active"

    # Activation needs the exact current revision.
    with pytest.raises(TopicRevisionConflictError):
        await activate(db_session, topic_id, created["revision"])


async def test_activation_is_idempotent_with_same_goal(db_session: AsyncSession) -> None:
    created = await draft(db_session)
    first = await activate(db_session, created["topic_id"], created["revision"])
    again = await activate(db_session, created["topic_id"], first["revision"])
    assert again["topic_id"] == first["topic_id"]
    assert again["revision"] == first["revision"]
    changed_goal = {**GOAL, "criteria": "悄悄放宽门槛"}
    with pytest.raises(IllegalTransitionError):
        await activate(db_session, created["topic_id"], first["revision"], changed_goal)


async def test_idempotent_create_retry_returns_original(db_session: AsyncSession) -> None:
    first = await workspace_write(
        db_session,
        actor="user:api",
        operation="draft_create",
        payload=draft_payload(),
        idempotency_key="create-draft-514",
    )
    retry = await workspace_write(
        db_session,
        actor="user:api",
        operation="draft_create",
        payload=draft_payload(),
        idempotency_key="create-draft-514",
    )
    assert retry["topic_id"] == first["topic_id"]
    assert retry["created"] is False
    assert retry["revision"] == first["revision"]

    full = await workspace_write(
        db_session,
        actor="user:api",
        operation="create",
        payload=topic_payload(),
        idempotency_key="create-full-514",
    )
    full_retry = await workspace_write(
        db_session,
        actor="user:api",
        operation="create",
        payload=topic_payload(),
        idempotency_key="create-full-514",
    )
    assert full_retry["topic_id"] == full["topic_id"]
    assert full_retry["created"] is False

    with pytest.raises(IdempotencyConflictError):
        await workspace_write(
            db_session,
            actor="user:api",
            operation="create",
            payload=topic_payload(title="同名键的不同课题"),
            idempotency_key="create-full-514",
        )
    count = (
        await db_session.execute(
            text("SELECT count(*) FROM research_topics WHERE idempotency_key=:key"),
            {"key": "create-full-514"},
        )
    ).scalar_one()
    assert count == 1


async def test_dual_session_stale_revision_is_rejected_without_overwrite(
    db_session: AsyncSession,
) -> None:
    """Real contention: A holds FOR UPDATE, B blocks, then conflicts (no overwrite)."""
    import asyncio

    from finboard_persistence import session_factory

    topic = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload()
    )
    engine = db_session.bind
    assert isinstance(engine, AsyncEngine)
    async with session_factory(engine)() as writer_a, session_factory(engine)() as writer_b:
        # A locks the row before touching it, exactly like the lifecycle service.
        locked = await ResearchTopicRepository(writer_a).get(topic["topic_id"], lock=True)
        assert locked.revision == 1

        blocked = asyncio.create_task(
            workspace_write(
                writer_b,
                actor="agent:mcp",
                operation="update",
                topic_id=topic["topic_id"],
                expected_revision=1,
                payload=topic_payload(summary="过期会话不得覆盖"),
            )
        )
        await asyncio.sleep(0.3)
        # B must be waiting on A's row lock; without FOR UPDATE it would have read
        # revision 1 and could later overwrite A's write without a conflict.
        assert not blocked.done()

        current = await workspace_write(
            writer_a,
            actor="user:api",
            operation="update",
            topic_id=topic["topic_id"],
            expected_revision=1,
            payload=topic_payload(summary="第一位写者的结果"),
        )
        assert current["revision"] == 2
        with pytest.raises(TopicRevisionConflictError) as conflict:
            await blocked
        assert conflict.value.detail["current_revision"] == 2
        assert conflict.value.detail["your_revision"] == 1
        assert conflict.value.detail["changed_fields"]

    stored = await db_session.scalar(
        select(ResearchTopicModel.payload).where(ResearchTopicModel.topic_id == topic["topic_id"])
    )
    assert stored is not None
    assert stored["summary"] == "第一位写者的结果"


async def test_concurrent_same_idempotency_key_creates_one_topic(
    db_session: AsyncSession,
) -> None:
    import asyncio

    from finboard_persistence import session_factory

    engine = db_session.bind
    assert isinstance(engine, AsyncEngine)

    async def create(session: AsyncSession) -> dict[str, Any]:
        return await workspace_write(
            session,
            actor="user:api",
            operation="draft_create",
            payload=draft_payload(),
            idempotency_key="concurrent-create-514",
        )

    async with session_factory(engine)() as first, session_factory(engine)() as second:
        results = await asyncio.gather(create(first), create(second), return_exceptions=True)

    assert not any(isinstance(item, BaseException) for item in results)
    created = [item for item in results if isinstance(item, dict)]
    assert len(created) == 2
    assert created[0]["topic_id"] == created[1]["topic_id"]
    assert sorted(bool(item.get("created")) for item in created) == [False, True]
    rows = (
        await db_session.execute(
            text("SELECT count(*) FROM research_topics WHERE idempotency_key=:key"),
            {"key": "concurrent-create-514"},
        )
    ).scalar_one()
    assert rows == 1


async def test_put_cannot_change_lifecycle_state(db_session: AsyncSession) -> None:
    topic = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload()
    )
    closed = await transition(db_session, topic["topic_id"], "close", topic["revision"], "本轮结束")
    assert closed["status"] == "closed"
    lifecycle_before = len(await lifecycle_entries(db_session, topic["topic_id"]))

    # Old-style full PUT without a status field must not revive the topic.
    unchanged = await workspace_write(
        db_session,
        actor="user:api",
        operation="update",
        topic_id=topic["topic_id"],
        expected_revision=closed["revision"],
        payload=topic_payload(summary="只更新说明"),
    )
    assert unchanged["status"] == "closed"
    assert unchanged["revision"] == closed["revision"] + 1
    assert len(await lifecycle_entries(db_session, topic["topic_id"])) == lifecycle_before

    # Declaring the current status is tolerated (old clients send the full shape).
    same = await workspace_write(
        db_session,
        actor="user:api",
        operation="update",
        topic_id=topic["topic_id"],
        expected_revision=unchanged["revision"],
        payload=topic_payload(status="closed"),
    )
    assert same["status"] == "closed"
    assert len(await lifecycle_entries(db_session, topic["topic_id"])) == lifecycle_before

    # Declaring a different status is a conflict that points at /transitions.
    with pytest.raises(IllegalTransitionError) as mismatch:
        await workspace_write(
            db_session,
            actor="user:api",
            operation="update",
            topic_id=topic["topic_id"],
            expected_revision=same["revision"],
            payload=topic_payload(status="active"),
        )
    assert mismatch.value.detail["code"] == "illegal_transition"
    assert mismatch.value.detail["current"] == "closed"
    assert mismatch.value.detail["allowed"] == ["reopen"]

    # active -> closed via PUT is rejected too (the reported bypass).
    active = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload(title="第二个课题")
    )
    with pytest.raises(IllegalTransitionError):
        await workspace_write(
            db_session,
            actor="user:api",
            operation="update",
            topic_id=active["topic_id"],
            expected_revision=active["revision"],
            payload=topic_payload(title="第二个课题", status="closed"),
        )
    stored = await db_session.scalar(
        select(ResearchTopicModel.payload).where(ResearchTopicModel.topic_id == active["topic_id"])
    )
    assert stored is not None
    assert stored["status"] == "active"


async def test_draft_is_editable_but_never_gets_a_goal_via_put(
    db_session: AsyncSession,
) -> None:
    created = await draft(db_session)
    edited = await workspace_write(
        db_session,
        actor="user:api",
        operation="update",
        topic_id=created["topic_id"],
        expected_revision=created["revision"],
        payload=draft_payload(summary="草稿笔记", open_questions=["先核验来源"]),
    )
    assert edited["status"] == "draft"
    assert edited["revision"] == created["revision"] + 1
    assert edited["summary"] == "草稿笔记"
    assert edited["goal"] is None

    from finboard_app.research_topic_lifecycle import TopicInvalidInputError

    with pytest.raises(TopicInvalidInputError, match="activate"):
        await workspace_write(
            db_session,
            actor="user:api",
            operation="update",
            topic_id=created["topic_id"],
            expected_revision=edited["revision"],
            payload=draft_payload(goal=dict(GOAL)),
        )

    # Non-draft PUT without a goal is a 422 at the API layer (validation error).
    active = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload()
    )
    with pytest.raises(ValueError, match="goal"):
        await workspace_write(
            db_session,
            actor="user:api",
            operation="update",
            topic_id=active["topic_id"],
            expected_revision=active["revision"],
            payload={"title": "缺少目标", "question": "?"},
        )


async def test_create_only_accepts_active_and_cross_mode_keys_conflict(
    db_session: AsyncSession,
) -> None:
    from finboard_app.research_topic_lifecycle import TopicInvalidInputError

    with pytest.raises(TopicInvalidInputError, match="active"):
        await workspace_write(
            db_session,
            actor="user:api",
            operation="create",
            payload=topic_payload(status="paused"),
        )

    full = await workspace_write(
        db_session,
        actor="user:api",
        operation="create",
        payload=topic_payload(),
        idempotency_key="cross-mode-514",
    )
    with pytest.raises(IdempotencyConflictError, match="创建模式"):
        await workspace_write(
            db_session,
            actor="user:api",
            operation="draft_create",
            payload=draft_payload(title=full["title"], question=full["question"]),
            idempotency_key="cross-mode-514",
        )
    with pytest.raises(TopicInvalidInputError, match="幂等键"):
        await workspace_write(
            db_session,
            actor="user:api",
            operation="draft_create",
            payload=draft_payload(),
            idempotency_key="k" * 129,
        )


async def test_entry_lookup_is_topic_scoped(db_session: AsyncSession) -> None:
    first = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload()
    )
    second = await workspace_write(
        db_session,
        actor="user:api",
        operation="create",
        payload=topic_payload(title="另一个课题"),
    )
    entry = await workspace_write(
        db_session,
        actor="user:api",
        operation="append",
        topic_id=first["topic_id"],
        idempotency_key="round-514-1",
        payload=round_payload(),
    )
    found = await workspace_read(
        db_session, topic_id=first["topic_id"], entry_id=entry["entry_id"]
    )
    assert found["checksum"] == entry["checksum"]
    assert [(ref["kind"], ref["ref_id"]) for ref in found["refs"]] == [
        ("research_run", "RR-514-deleted")
    ]
    assert found["entry_id"] == entry["entry_id"]

    with pytest.raises(LookupError):
        await workspace_read(db_session, topic_id=second["topic_id"], entry_id=entry["entry_id"])
    with pytest.raises(LookupError):
        await workspace_read(db_session, topic_id=first["topic_id"], entry_id="RE-missing")


async def test_branch_aggregation(db_session: AsyncSession) -> None:
    topic = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload()
    )
    for index, (branch, outcome) in enumerate(
        [
            ("main", "failed"),
            ("main", "completed"),
            ("alt", "interrupted"),
            ("alt", "failed"),
            ("alt", "completed"),
        ]
    ):
        await workspace_write(
            db_session,
            actor="user:api",
            operation="append",
            topic_id=topic["topic_id"],
            idempotency_key=f"branch-round-{index}",
            payload=round_payload(branch=branch, outcome=outcome),
        )
    page = await workspace_read(db_session, topic_id=topic["topic_id"], view="branches")
    items = {item["branch"]: item for item in page["items"]}
    assert items["main"]["count"] == 2
    assert items["main"]["last_outcome"] == "completed"
    assert items["alt"]["count"] == 3
    assert items["alt"]["last_outcome"] == "completed"
    assert items["alt"]["last_at"]
    assert page["has_more"] is False

    first_page = await workspace_read(
        db_session, topic_id=topic["topic_id"], view="branches", limit=1
    )
    assert first_page["has_more"] is True
    assert len(first_page["items"]) == 1


async def _add_run(session: AsyncSession, run_id: str) -> None:
    session.add(
        ResearchRunModel(
            run_id=run_id,
            idempotency_key=f"key-{run_id}",
            strategy_id="test",
            strategy_kind="multi_factor",
            status="completed",
            schema_version="v1",
            manifest_checksum="a" * 64,
            manifest={"dataset_releases": []},
            requested_by="test",
        )
    )
    await session.commit()


async def test_refs_check_reports_deleted_target(db_session: AsyncSession) -> None:
    run_id = "RR-514-deleted"
    await _add_run(db_session, run_id)
    topic = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload()
    )
    await workspace_write(
        db_session,
        actor="user:api",
        operation="append",
        topic_id=topic["topic_id"],
        idempotency_key="refs-round-1",
        payload=round_payload(),
    )
    matched = await workspace_read(
        db_session,
        topic_id=topic["topic_id"],
        view="refs",
        docs_root=Path("docs/research"),
    )
    assert [item["status"] for item in matched["items"]] == ["matched"]
    assert matched["items"][0]["entry_ids"]

    await db_session.execute(
        text("DELETE FROM research_runs WHERE run_id=:run_id"), {"run_id": run_id}
    )
    await db_session.commit()
    missing = await workspace_read(
        db_session,
        topic_id=topic["topic_id"],
        view="refs",
        docs_root=Path("docs/research"),
    )
    assert missing["items"][0]["status"] == "missing"

    context = await workspace_read(
        db_session,
        topic_id=topic["topic_id"],
        view="context",
        docs_root=Path("docs/research"),
    )
    assert context["dangling_refs"][0]["ref"]["ref_id"] == run_id
    assert context["dangling_refs"][0]["status"] == "missing"
    assert context["active_links"] == []
    assert context["next_actions"]
    assert context["evidence_levels"]["documents"] == "canonical_document"


async def test_context_is_bounded_for_large_topic(db_session: AsyncSession) -> None:
    questions = [f"开放问题{i}:" + "待核验" * 250 for i in range(30)]
    topic = await workspace_write(
        db_session,
        actor="user:api",
        operation="create",
        payload=topic_payload(open_questions=questions),
    )
    for index in range(200):
        await workspace_write(
            db_session,
            actor="agent:mcp",
            operation="append",
            topic_id=topic["topic_id"],
            idempotency_key=f"large-round-{index}",
            payload=round_payload(
                objective=f"第{index}轮",
                conclusion="长结论" * 500,
                next_step="下一步" * 300,
                outcome="failed" if index == 199 else "completed",
            ),
        )
    context = await workspace_read(
        db_session,
        topic_id=topic["topic_id"],
        view="context",
        rounds=20,
        refs=10,
        docs_root=Path("docs/research"),
    )
    raw = json.dumps(context, ensure_ascii=False, default=str).encode()
    assert len(raw) <= 64 * 1024
    assert context["excerpted"] is True
    assert len(context["recent_rounds"]) <= 20
    assert all(len(item["conclusion_excerpt"]) <= 300 for item in context["recent_rounds"])
    assert len(context["open_questions"]) <= 30
    assert context["bounds"]["total_rounds"] == 200
    assert context["recent_rounds"][0]["outcome"] == "failed"
    assert context["topic"]["revision"] == topic["revision"]
    assert context["generated_at"]

    with pytest.raises(ValueError, match="rounds"):
        await workspace_read(db_session, topic_id=topic["topic_id"], view="context", rounds=21)
    with pytest.raises(ValueError, match="refs"):
        await workspace_read(db_session, topic_id=topic["topic_id"], view="context", refs=11)


async def test_list_filter_and_sort(db_session: AsyncSession) -> None:
    first = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload(title="较早课题")
    )
    second = await draft(db_session)
    await workspace_write(
        db_session,
        actor="user:api",
        operation="update",
        topic_id=first["topic_id"],
        expected_revision=first["revision"],
        payload=topic_payload(title="较早课题", summary="刚刚更新"),
    )
    by_updated = await workspace_read(db_session, limit=50)
    assert by_updated["items"][0]["topic_id"] == first["topic_id"]
    by_created = await workspace_read(db_session, limit=50, sort="created")
    assert by_created["items"][0]["topic_id"] == second["topic_id"]
    drafts = await workspace_read(db_session, limit=50, status="draft")
    assert [item["topic_id"] for item in drafts["items"]] == [second["topic_id"]]
    with pytest.raises(ValueError, match="status"):
        await workspace_read(db_session, status="unknown")
    with pytest.raises(ValueError, match="sort"):
        await workspace_read(db_session, sort="name")


async def test_list_excerpted_only_when_actually_truncated(db_session: AsyncSession) -> None:
    short = await workspace_write(
        db_session,
        actor="user:api",
        operation="create",
        payload=topic_payload(title="短课题", question="短问题", summary="短摘要"),
    )
    long = await workspace_write(
        db_session,
        actor="user:api",
        operation="create",
        payload=topic_payload(title="长课题", question="长" * 1500, summary="摘要"),
    )
    page = await workspace_read(db_session, limit=50)
    items = {item["topic_id"]: item for item in page["items"]}
    assert items[short["topic_id"]]["excerpted"] is False
    assert items[long["topic_id"]]["excerpted"] is True
    assert len(items[long["topic_id"]]["question"]) == 1200


async def test_context_marks_objective_and_next_step_excerpts(db_session: AsyncSession) -> None:
    topic = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload()
    )
    await workspace_write(
        db_session,
        actor="user:api",
        operation="append",
        topic_id=topic["topic_id"],
        idempotency_key="excerpt-round-1",
        payload=round_payload(
            objective="长目标" * 200,
            next_step="长下一步" * 200,
            conclusion="短结论",
        ),
    )
    context = await workspace_read(
        db_session, topic_id=topic["topic_id"], view="context", refs=0
    )
    item = context["recent_rounds"][0]
    assert context["excerpted"] is True
    assert len(item["objective"]) == 300
    assert len(item["next_step"]) == 300
    assert item["conclusion_excerpt"] == "短结论"


async def test_context_not_excerpted_when_nothing_is_cut(db_session: AsyncSession) -> None:
    topic = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload()
    )
    await workspace_write(
        db_session,
        actor="user:api",
        operation="append",
        topic_id=topic["topic_id"],
        idempotency_key="short-round-1",
        payload=round_payload(objective="短目标", next_step="短下一步", conclusion="短结论"),
    )
    context = await workspace_read(
        db_session, topic_id=topic["topic_id"], view="context", refs=0
    )
    assert context["excerpted"] is False
    assert context["bounds"]["total_rounds"] == 1


async def test_context_reports_dangling_refs_has_more(db_session: AsyncSession) -> None:
    topic = await workspace_write(
        db_session, actor="user:api", operation="create", payload=topic_payload()
    )
    refs = [
        {"kind": "research_run", "ref_id": f"RR-missing-{index}"} for index in range(12)
    ]
    await workspace_write(
        db_session,
        actor="user:api",
        operation="append",
        topic_id=topic["topic_id"],
        idempotency_key="many-refs-round-1",
        payload=round_payload(source_refs=refs),
    )
    context = await workspace_read(
        db_session, topic_id=topic["topic_id"], view="context", refs=5
    )
    assert context["refs_checked"] == 5
    assert context["dangling_refs_has_more"] is True
    assert len(context["dangling_refs"]) == 5
    assert {item["status"] for item in context["dangling_refs"]} == {"missing"}


async def test_mcp_topic_lifecycle_views_and_readonly(db_session: AsyncSession) -> None:
    from types import SimpleNamespace
    from typing import cast

    from finboard_mcp.audit import AuditRecorder
    from finboard_mcp.context import McpAppContext
    from finboard_mcp.tools.workspace import call

    engine = db_session.bind
    assert isinstance(engine, AsyncEngine)
    from finboard_persistence import session_factory

    app = cast(
        McpAppContext,
        SimpleNamespace(
            write_tools_enabled=True,
            audit=AuditRecorder(),
            session_maker=session_factory(engine),
            settings=SimpleNamespace(research_docs_dir="docs/research"),
        ),
    )
    created = await call(app, "write", {"operation": "draft_create", "payload": draft_payload()})
    assert created.status == "ok"
    assert created.data["status"] == "draft"
    assert created.data["created"] is True
    topic_id = created.data["topic_id"]

    activated = await call(
        app,
        "write",
        {
            "operation": "activate",
            "topic_id": topic_id,
            "expected_revision": created.data["revision"],
            "payload": {"goal": dict(GOAL)},
        },
    )
    assert activated.status == "ok"
    assert activated.data["status"] == "active"

    paused = await call(
        app,
        "write",
        {
            "operation": "transition",
            "topic_id": topic_id,
            "expected_revision": activated.data["revision"],
            "payload": {"action": "pause"},
        },
    )
    assert paused.status == "ok"
    assert paused.data["status"] == "paused"

    illegal = await call(
        app,
        "write",
        {
            "operation": "transition",
            "topic_id": topic_id,
            "expected_revision": paused.data["revision"],
            "payload": {"action": "reopen", "reason": "无依据重开"},
        },
    )
    assert illegal.status == "error"
    assert illegal.error is not None
    assert illegal.error.kind == "conflict"
    # Structured detail survives the MCP layer (no silent string flattening).
    assert '"allowed"' in illegal.error.message
    assert '"current": "paused"' in illegal.error.message

    context = await call(app, "read", {"topic_id": topic_id, "view": "context"})
    assert context.status == "ok"
    assert context.data["topic"]["status"] == "paused"
    assert context.data["next_actions"]
    assert "dangling_refs_has_more" in context.data

    branches = await call(app, "read", {"topic_id": topic_id, "view": "branches"})
    assert branches.status == "ok"
    assert branches.data["items"] == []
    refs = await call(app, "read", {"topic_id": topic_id, "view": "refs"})
    assert refs.status == "ok"
    assert refs.data["total"] == 0

    readonly = cast(
        McpAppContext,
        SimpleNamespace(
            **{**vars(app), "write_tools_enabled": False},
        ),
    )
    denied = await call(
        readonly, "write", {"operation": "draft_create", "payload": draft_payload()}
    )
    assert denied.status == "denied"
    assert denied.error is not None
    assert denied.error.kind == "permission_denied"


async def test_rest_lifecycle_conflict_and_context(db_session: AsyncSession) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from finboard_api.deps import get_db_session
    from finboard_api.routes.research_topics import router

    app = FastAPI()
    app.include_router(router)

    async def session_override() -> Any:
        yield db_session

    app.dependency_overrides[get_db_session] = session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/research/topics", json=draft_payload())
        assert created.status_code == 201
        topic = created.json()
        assert topic["status"] == "draft"

        retry = await client.post(
            "/api/research/topics",
            json={**draft_payload(), "idempotency_key": "rest-draft-514"},
        )
        assert retry.status_code == 201
        again = await client.post(
            "/api/research/topics",
            json={**draft_payload(), "idempotency_key": "rest-draft-514"},
        )
        assert again.status_code == 200
        assert again.json()["topic_id"] == retry.json()["topic_id"]

        activated = await client.post(
            f"/api/research/topics/{topic['topic_id']}/activate",
            json={"goal": dict(GOAL), "expected_revision": topic["revision"]},
        )
        assert activated.status_code == 200
        assert activated.json()["status"] == "active"
        revision = activated.json()["revision"]

        illegal = await client.post(
            f"/api/research/topics/{topic['topic_id']}/transitions",
            json={"action": "reopen", "reason": "无依据重开", "expected_revision": revision},
        )
        assert illegal.status_code == 409
        assert illegal.json()["detail"]["code"] == "illegal_transition"
        assert illegal.json()["detail"]["current"] == "active"
        assert illegal.json()["detail"]["allowed"] == ["closed", "paused"]

        stale = await client.put(
            f"/api/research/topics/{topic['topic_id']}",
            json={**topic_payload(), "expected_revision": topic["revision"]},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "topic_revision_conflict"
        assert stale.json()["detail"]["current_revision"] == revision

        paused = await client.post(
            f"/api/research/topics/{topic['topic_id']}/transitions",
            json={"action": "pause", "expected_revision": revision},
        )
        assert paused.status_code == 200

        context = await client.get(
            f"/api/research/topics/{topic['topic_id']}/context?rounds=5&refs=5"
        )
        assert context.status_code == 200
        assert context.json()["topic"]["status"] == "paused"
        assert context.json()["lifecycle"]["last"]["to"] == "paused"

        branches = await client.get(f"/api/research/topics/{topic['topic_id']}/branches")
        assert branches.status_code == 200
        refs = await client.get(f"/api/research/topics/{topic['topic_id']}/refs/check?rounds=5")
        assert refs.status_code == 200
        assert (await client.get("/api/research/topics?status=draft")).status_code == 200
        assert (await client.get("/api/research/topics?status=bogus")).status_code == 422
        assert (
            await client.get(f"/api/research/topics/{topic['topic_id']}/entries/RE-missing")
        ).status_code == 404

        # Creating paused/closed topics would skip the lifecycle audit.
        rejected_create = await client.post(
            "/api/research/topics", json={**topic_payload(), "status": "paused"}
        )
        assert rejected_create.status_code == 422

        # PUT cannot change lifecycle state: a declared mismatch is a 409.
        put_mismatch = await client.put(
            f"/api/research/topics/{topic['topic_id']}",
            json={
                **topic_payload(),
                "status": "closed",
                "expected_revision": paused.json()["revision"],
            },
        )
        assert put_mismatch.status_code == 409
        assert put_mismatch.json()["detail"]["code"] == "illegal_transition"
        assert put_mismatch.json()["detail"]["allowed"] == ["close", "resume"]

        # A full PUT without status keeps the topic paused and appends no lifecycle.
        entries_before = await client.get(
            f"/api/research/topics/{topic['topic_id']}/entries?limit=50"
        )
        lifecycle_before = len(
            [
                item
                for item in entries_before.json()["items"]
                if item.get("entry_type") == "lifecycle"
            ]
        )
        kept = await client.put(
            f"/api/research/topics/{topic['topic_id']}",
            json={**topic_payload(), "expected_revision": paused.json()["revision"]},
        )
        assert kept.status_code == 200
        assert kept.json()["status"] == "paused"
        entries_after = await client.get(
            f"/api/research/topics/{topic['topic_id']}/entries?limit=50"
        )
        assert (
            len(
                [
                    item
                    for item in entries_after.json()["items"]
                    if item.get("entry_type") == "lifecycle"
                ]
            )
            == lifecycle_before
        )

        # Drafts are editable without a goal, and non-draft PUT without one is 422.
        draft_topic = (await client.post("/api/research/topics", json=draft_payload())).json()
        draft_edit = await client.put(
            f"/api/research/topics/{draft_topic['topic_id']}",
            json={
                "title": "改过的草稿",
                "question": "更具体的问题",
                "summary": "草稿笔记",
                "expected_revision": draft_topic["revision"],
            },
        )
        assert draft_edit.status_code == 200
        assert draft_edit.json()["status"] == "draft"
        assert draft_edit.json()["goal"] is None
        assert draft_edit.json()["revision"] == draft_topic["revision"] + 1

        missing_goal = await client.put(
            f"/api/research/topics/{topic['topic_id']}",
            json={
                "title": "缺少目标",
                "question": "?",
                "expected_revision": kept.json()["revision"],
            },
        )
        assert missing_goal.status_code == 422

        draft_with_goal = await client.put(
            f"/api/research/topics/{draft_topic['topic_id']}",
            json={
                "title": "草稿想带目标",
                "question": "?",
                "goal": dict(GOAL),
                "expected_revision": draft_edit.json()["revision"],
            },
        )
        assert draft_with_goal.status_code == 422

        # Cross-mode idempotency keys are conflicts, not silent reuse.
        await client.post(
            "/api/research/topics",
            json={**topic_payload(), "idempotency_key": "rest-cross-mode-514"},
        )
        cross_mode = await client.post(
            "/api/research/topics",
            json={**draft_payload(), "idempotency_key": "rest-cross-mode-514"},
        )
        assert cross_mode.status_code == 409
        assert cross_mode.json()["detail"]["code"] == "idempotency_conflict"


async def test_topic_lifecycle_migration_roundtrip(db_session: AsyncSession) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    base_spec = importlib.util.spec_from_file_location(
        "research_topic_base_migration", "migrations/versions/62f8b1e7a905_research_topics_500.py"
    )
    lifecycle_spec = importlib.util.spec_from_file_location(
        "research_topic_lifecycle_migration",
        "migrations/versions/a7c41e9b5d32_research_topic_lifecycle_514.py",
    )
    assert base_spec is not None
    assert base_spec.loader is not None
    assert lifecycle_spec is not None
    assert lifecycle_spec.loader is not None
    base = importlib.util.module_from_spec(base_spec)
    base_spec.loader.exec_module(base)
    lifecycle = importlib.util.module_from_spec(lifecycle_spec)
    lifecycle_spec.loader.exec_module(lifecycle)
    assert lifecycle.revision == "a7c41e9b5d32"

    schema = "migration_probe_" + uuid4().hex
    conn = await db_session.connection()
    await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))

    def columns(sync_conn: Any) -> set[str]:
        return {
            row[0]
            for row in sync_conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='research_topics' AND table_schema=current_schema()"
                )
            )
        }

    def probe(sync_conn: Any) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            base.upgrade()
            lifecycle.upgrade()
            assert {"status", "updated_at", "idempotency_key"} <= columns(sync_conn)
            sync_conn.execute(
                text(
                    "INSERT INTO research_topics(topic_id,payload,revision,created_by) "
                    "VALUES ('RT-probe','{\"title\":\"x\"}',1,'test')"
                )
            )
            sync_conn.execute(text("UPDATE research_topics SET status='paused'"))
            lifecycle.downgrade()
            assert not ({"status", "updated_at", "idempotency_key"} & columns(sync_conn))
            # Downgrade is lossless: the column value was folded back into payload.
            assert (
                sync_conn.execute(
                    text("SELECT payload::jsonb->>'status' FROM research_topics")
                ).scalar()
                == "paused"
            )
            lifecycle.upgrade()
            assert (
                sync_conn.execute(text("SELECT status FROM research_topics")).scalar() == "paused"
            )
            index_count = sync_conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes WHERE tablename='research_topics' "
                    "AND schemaname=current_schema() "
                    "AND indexname='uq_research_topics_idempotency_key'"
                )
            ).scalar()
            assert index_count == 1

    await conn.run_sync(probe)
    await db_session.rollback()  # Entire disposable schema and probes disappear.
