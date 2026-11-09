"""Shared REST/MCP contracts for research context, independent of OpenCode."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import ResearchMemoryModel
from finboard_persistence.research_topic_repo import (
    TOPIC_STATUSES,
    ResearchTopicRepository,
)


class WorkspaceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceRef(WorkspaceInput):
    kind: Literal[
        "research_run",
        "strategy",
        "dataset",
        "experiment",
        "memory",
        "document",
        "factor_series",
        "simulation",
        "backtest",
    ]
    ref_id: str = Field(min_length=1, max_length=256)
    version: str | None = Field(default=None, max_length=64)
    checksum: str | None = Field(default=None, max_length=64)

    @field_validator("ref_id")
    @classmethod
    def valid_id(cls, v: str) -> str:
        if not v.strip() or v.lower() == "unknown":
            raise ValueError("引用必须提供可核验的精确 ID")
        return v


class Goal(WorkspaceInput):
    version: str = Field(min_length=1, max_length=64)
    criteria: str = Field(min_length=1, max_length=2000)
    source: EvidenceRef


class TopicInput(WorkspaceInput):
    title: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=4000)
    goal: Goal
    status: Literal["active", "paused", "closed"] = "active"
    conclusion: Literal["unknown", "supported", "not_supported", "insufficient_evidence"] = (
        "unknown"
    )
    summary: str = Field(default="", max_length=4000)
    open_questions: list[str] = Field(default_factory=list, max_length=30)
    next_step: str = Field(default="", max_length=2000)

    @field_validator("open_questions")
    @classmethod
    def bounded_questions(cls, v: list[str]) -> list[str]:
        if any(not x.strip() or len(x) > 1000 for x in v):
            raise ValueError("开放问题须非空且每项最多1000字")
        return v


class RoundInput(WorkspaceInput):
    entry_type: Literal["round", "evidence"] = "round"
    goal_version: str = Field(min_length=1, max_length=64)
    objective: str = Field(min_length=1, max_length=2000)
    action: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=2000)
    outcome: Literal["completed", "failed", "interrupted", "rejected", "paused"] = "completed"
    conclusion: str = Field(default="", max_length=4000)
    confidence: Literal["unknown", "low", "medium", "high"] = "unknown"
    next_step: str = Field(default="", max_length=2000)
    source_refs: list[EvidenceRef] = Field(default_factory=list, max_length=30)
    supersedes_id: str | None = Field(default=None, max_length=32)
    branch: str = Field(default="main", max_length=100)


def record_out(row: Any) -> dict[str, Any]:
    result = {"created_by": row.created_by, "created_at": row.created_at.isoformat(), **row.payload}
    if hasattr(row, "topic_id"):
        result["topic_id"] = row.topic_id
    if hasattr(row, "entry_id"):
        result.update(entry_id=row.entry_id, checksum=row.checksum)
        result["evidence_level"] = (
            "agent_interpretation" if row.created_by.startswith("agent:") else "human_record"
        )
    else:
        result.update(revision=row.revision, evidence_level="working_summary")
        # The column is authoritative; payload.status is kept in sync for old JSON
        # consumers only.
        status = getattr(row, "status", None)
        if status:
            result["status"] = status
        updated_at = getattr(row, "updated_at", None)
        if updated_at is not None:
            result["updated_at"] = updated_at.isoformat()
    return result


async def topic_entry(session: AsyncSession, topic_id: str, entry_id: str) -> dict[str, Any]:
    """Exact entry lookup scoped to one topic; a cross-topic id is a 404."""
    repo = ResearchTopicRepository(session)
    await repo.get(topic_id)
    row = await repo.entry(topic_id, entry_id)
    if row is None:
        raise LookupError(f"轮次不存在或不属于该课题: {entry_id}")
    result = record_out(row)
    result["refs"] = row.payload.get("source_refs", [])
    result["topic_revision"] = row.payload.get("topic_revision")
    return result


async def topic_branches(
    session: AsyncSession, topic_id: str, *, limit: int = 20, offset: int = 0
) -> dict[str, Any]:
    if not 1 <= limit <= 50 or not 0 <= offset <= 100000:
        raise ValueError("分页范围: limit 1-50,offset 0-100000")
    repo = ResearchTopicRepository(session)
    await repo.get(topic_id)
    rows = await repo.branches(topic_id, limit, offset)
    items = [
        {
            **row,
            "count": int(row["count"]),
            "last_at": row["last_at"].isoformat() if row["last_at"] is not None else None,
        }
        for row in rows[:limit]
    ]
    return {
        "items": items,
        "has_more": len(rows) > limit,
        "offset": offset,
        "limit": limit,
    }


async def topic_refs_check(
    session: AsyncSession,
    topic_id: str,
    *,
    rounds: int = 10,
    limit: int = 20,
    offset: int = 0,
    docs_root: Path | None = None,
) -> dict[str, Any]:
    """Probe the refs of the most recent rounds; at most 20 refs per call."""
    if not 1 <= rounds <= 20:
        raise ValueError("rounds 范围: 1-20")
    if not 1 <= limit <= 20 or not 0 <= offset <= 100000:
        raise ValueError("引用检查分页范围: limit 1-20,offset 0-100000")
    repo = ResearchTopicRepository(session)
    await repo.get(topic_id)
    rows = await repo.recent_rounds(topic_id, rounds)
    ordered: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        for ref in payload.get("source_refs") or []:
            if not isinstance(ref, dict):
                continue
            key = (ref.get("kind"), ref.get("ref_id"), ref.get("version"), ref.get("checksum"))
            item = seen.get(key)
            if item is None:
                item = {"ref": ref, "entry_ids": []}
                seen[key] = item
                ordered.append(item)
            item["entry_ids"].append(row["entry_id"])
    page = ordered[offset : offset + limit]
    items: list[dict[str, Any]] = []
    root = docs_root or Path("docs/research")
    for item in page:
        try:
            fact = await source_fact(session, item["ref"], root)
            items.append({**item, "status": fact["status"], "facts": fact.get("facts")})
        except ValueError as exc:
            items.append({**item, "status": "invalid", "error": str(exc)})
    total = len(ordered)
    return {
        "items": items,
        "total": total,
        "has_more": offset + limit < total,
        "offset": offset,
        "limit": limit,
        "rounds": rounds,
    }


CONTEXT_MAX_BYTES = 64 * 1024
CONTEXT_MAX_ROUNDS = 20
CONTEXT_MAX_REFS = 10
CONTEXT_EXCERPT_CHARS = 300


def _context_size(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode())


def _excerpt(value: Any, limit: int) -> tuple[str, bool]:
    text_value = value if isinstance(value, str) else ""
    if len(text_value) <= limit:
        return text_value, False
    return text_value[:limit], True


def _next_actions(
    *,
    status: str,
    recent_rounds: list[dict[str, Any]],
    dangling_refs: list[dict[str, Any]],
    open_questions: list[str],
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if status == "draft":
        actions.append(
            {"action": "activate", "why": "草稿缺少目标版本与来源,补齐后才能发起研究"}
        )
    if dangling_refs:
        actions.append(
            {
                "action": "check_refs",
                "why": f"{len(dangling_refs)}个引用断链、版本或checksum不符,需重新核验",
            }
        )
    if status == "paused":
        actions.append({"action": "resume", "why": "课题已暂停;续接前先确认是否恢复"})
    if status == "closed":
        actions.append({"action": "reopen", "why": "课题已收束;需要新证据时先重开"})
    if status == "active" and not recent_rounds:
        actions.append({"action": "write_round", "why": "尚无轮次记录,下一步动作未留痕"})
    if status == "active" and recent_rounds:
        last = recent_rounds[0]
        if last.get("outcome") in {"failed", "interrupted", "paused"}:
            actions.append(
                {"action": "write_round", "why": "最近一轮失败或中断,需记录新尝试或原因"}
            )
    if status == "active" and open_questions:
        actions.append(
            {"action": "review_open_questions", "why": "仍有未解决的开放问题,先核对证据"}
        )
    if not actions:
        actions.append(
            {"action": "review", "why": "暂无自动发现的缺口;读轮次与正式文档后决定下一步"}
        )
    return actions[:6]


async def topic_context(
    session: AsyncSession,
    topic_id: str,
    *,
    rounds: int = 10,
    refs: int = 10,
    docs_root: Path | None = None,
) -> dict[str, Any]:
    """Bounded cross-client continuation payload (plan #514 §5.4)."""
    if not 1 <= rounds <= CONTEXT_MAX_ROUNDS:
        raise ValueError(f"rounds 范围: 1-{CONTEXT_MAX_ROUNDS}")
    if not 0 <= refs <= CONTEXT_MAX_REFS:
        raise ValueError(f"refs 范围: 0-{CONTEXT_MAX_REFS}")
    repo = ResearchTopicRepository(session)
    topic = await repo.get(topic_id)
    payload: Mapping[str, Any] = topic.payload if isinstance(topic.payload, dict) else {}
    total_rounds = await repo.count_rounds(topic_id)
    lifecycle = await repo.latest_lifecycle(topic_id)
    round_rows = await repo.recent_rounds(topic_id, rounds)

    excerpted = total_rounds > len(round_rows)
    recent_rounds: list[dict[str, Any]] = []
    for row in round_rows:
        data = row["payload"] if isinstance(row["payload"], dict) else {}
        conclusion, cut_conclusion = _excerpt(data.get("conclusion"), CONTEXT_EXCERPT_CHARS)
        objective, cut_objective = _excerpt(data.get("objective"), CONTEXT_EXCERPT_CHARS)
        next_step, cut_next_step = _excerpt(data.get("next_step"), CONTEXT_EXCERPT_CHARS)
        # Never truncate silently: every shortened field flips the payload flag.
        excerpted = excerpted or cut_conclusion or cut_objective or cut_next_step
        recent_rounds.append(
            {
                "entry_id": row["entry_id"],
                "branch": data.get("branch") or "main",
                "outcome": data.get("outcome"),
                "objective": objective,
                "conclusion_excerpt": conclusion,
                "next_step": next_step,
                "created_at": row["created_at"].isoformat(),
                "created_by": row["created_by"],
                "evidence_level": "agent_interpretation"
                if str(row["created_by"]).startswith("agent:")
                else "human_record",
            }
        )

    questions: list[str] = []
    for question in payload.get("open_questions") or []:
        if not isinstance(question, str) or not question.strip():
            continue
        text_question, cut = _excerpt(question, 1000)
        excerpted = excerpted or cut
        questions.append(text_question)
        if len(questions) >= 30:
            excerpted = True
            break

    dangling_refs: list[dict[str, Any]] = []
    dangling_has_more = False
    refs_checked = 0
    if refs:
        probe = await topic_refs_check(
            session, topic_id, rounds=rounds, limit=refs, docs_root=docs_root
        )
        refs_checked = len(probe["items"])
        dangling_has_more = bool(probe["has_more"])
        dangling_refs = [
            {"ref": item["ref"], "status": item["status"], "entry_ids": item["entry_ids"]}
            for item in probe["items"]
            if item["status"] != "matched"
        ]

    last_lifecycle = None
    since = topic.created_at.isoformat()
    if lifecycle is not None:
        last_payload = lifecycle["payload"] if isinstance(lifecycle["payload"], dict) else {}
        since = lifecycle["created_at"].isoformat()
        last_lifecycle = {
            "from": last_payload.get("from"),
            "to": last_payload.get("to"),
            "reason": last_payload.get("reason"),
            "actor": last_payload.get("actor"),
            "action": last_payload.get("action"),
            "at": last_payload.get("at") or since,
            "entry_id": lifecycle["entry_id"],
        }

    goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else None
    context: dict[str, Any] = {
        "topic": {
            "topic_id": topic.topic_id,
            "revision": topic.revision,
            "title": payload.get("title"),
            "question": payload.get("question"),
            "status": topic.status,
            "conclusion": payload.get("conclusion") or "unknown",
            "summary": payload.get("summary") or "",
            "updated_at": topic.updated_at.isoformat() if topic.updated_at else None,
        },
        "goal": goal,
        "lifecycle": {"since": since, "last": last_lifecycle},
        "open_questions": questions,
        "next_step": payload.get("next_step") or "",
        "recent_rounds": recent_rounds,
        "active_links": [],  # Populated by #515 once topic <-> run links exist.
        "dangling_refs": dangling_refs,
        "dangling_refs_has_more": dangling_has_more,
        "refs_checked": refs_checked,
        "evidence_levels": {
            "topic": "working_summary",
            "rounds": "agent_interpretation",
            "documents": "canonical_document",
            "links": "automatic_fact",
        },
        "next_actions": _next_actions(
            status=topic.status,
            recent_rounds=recent_rounds,
            dangling_refs=dangling_refs,
            open_questions=questions,
        ),
        "generated_at": datetime.now(UTC).isoformat(),
        "revision": topic.revision,
        "excerpted": excerpted,
        "bounds": {
            "rounds": rounds,
            "refs": refs,
            "total_rounds": total_rounds,
            "max_rounds": CONTEXT_MAX_ROUNDS,
            "max_refs": CONTEXT_MAX_REFS,
            "excerpt_chars": CONTEXT_EXCERPT_CHARS,
            "max_bytes": CONTEXT_MAX_BYTES,
        },
    }

    truncated_sections: list[str] = []
    if _context_size(context) > CONTEXT_MAX_BYTES:
        for item in recent_rounds:
            item["conclusion_excerpt"] = item["conclusion_excerpt"][:120]
            item["objective"] = item["objective"][:120]
            item["next_step"] = item["next_step"][:120]
        questions = [q[:200] for q in questions]
        context["open_questions"] = questions
        context["dangling_refs"] = [
            {k: v for k, v in item.items() if k != "facts"} for item in dangling_refs
        ]
        truncated_sections.append("excerpts")
        excerpted = True
    if _context_size(context) > CONTEXT_MAX_BYTES:
        context["recent_rounds"] = recent_rounds[:5]
        context["open_questions"] = questions[:10]
        context["dangling_refs"] = context["dangling_refs"][:5]
        context["topic"]["summary"] = str(context["topic"]["summary"])[:500]
        truncated_sections.append("rounds_and_questions")
        excerpted = True
    if _context_size(context) > CONTEXT_MAX_BYTES:
        context["recent_rounds"] = []
        context["open_questions"] = []
        context["dangling_refs"] = []
        truncated_sections.append("sections_dropped")
        excerpted = True
    context["excerpted"] = excerpted
    if truncated_sections:
        context["truncated_sections"] = truncated_sections
    return context


async def workspace_read(
    session: AsyncSession,
    *,
    topic_id: str | None = None,
    entries: bool = False,
    limit: int = 20,
    offset: int = 0,
    view: str | None = None,
    entry_id: str | None = None,
    status: str | None = None,
    sort: str = "updated",
    rounds: int = 10,
    refs: int = 10,
    docs_root: Path | None = None,
) -> dict[str, Any]:
    if not 1 <= limit <= 50 or not 0 <= offset <= 100000:
        raise ValueError("分页范围: limit 1-50,offset 0-100000")
    if entry_id:
        if not topic_id:
            raise ValueError("精确条目查询须提供 topic_id")
        return await topic_entry(session, topic_id, entry_id)
    if view == "context":
        if not topic_id:
            raise ValueError("续接上下文须提供 topic_id")
        return await topic_context(session, topic_id, rounds=rounds, refs=refs, docs_root=docs_root)
    if view == "branches":
        if not topic_id:
            raise ValueError("分支聚合须提供 topic_id")
        return await topic_branches(session, topic_id, limit=limit, offset=offset)
    if view == "refs":
        if not topic_id:
            raise ValueError("断链检查须提供 topic_id")
        return await topic_refs_check(
            session,
            topic_id,
            rounds=rounds,
            limit=min(limit, 20),
            offset=offset,
            docs_root=docs_root,
        )
    if view not in (None, "list", "detail"):
        raise ValueError("view 必须为 context/branches/refs")
    if status is not None and status not in TOPIC_STATUSES:
        raise ValueError(f"status 必须为 {'/'.join(TOPIC_STATUSES)}")
    if sort not in {"updated", "created"}:
        raise ValueError("sort 必须为 updated/created")
    repo = ResearchTopicRepository(session)
    if topic_id and not entries:
        return record_out(await repo.get(topic_id))
    rows = (
        await repo.entries(topic_id, limit + 1, offset)
        if topic_id
        else await repo.list_topics(limit + 1, offset, status=status, sort=sort)
    )
    items = [record_out(r) for r in rows[:limit]]
    if not topic_id:
        for item in items:
            # List mode is an excerpt view, but the flag must mean "something was
            # actually shortened", not "this endpoint always shortens".
            truncated = False
            for field in ("question", "summary", "next_step"):
                value = item.get(field) or ""
                if len(value) > 1200:
                    truncated = True
                item[field] = value[:1200]
            questions = item.get("open_questions") or []
            if len(questions) > 5 or any(len(question) > 300 for question in questions):
                truncated = True
            item["open_questions"] = [question[:300] for question in questions[:5]]
            item["excerpted"] = truncated
    return {
        "items": items,
        "has_more": len(rows) > limit,
        "offset": offset,
        "limit": limit,
        "sort": sort,
        "status": status,
    }


async def workspace_write(
    session: AsyncSession,
    *,
    actor: str,
    operation: str,
    payload: dict[str, Any],
    topic_id: str | None = None,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
    status_declared: bool | None = None,
) -> dict[str, Any]:
    from finboard_app.research_topic_lifecycle import (
        activate_topic,
        create_topic,
        transition_topic,
        update_topic,
    )

    repo = ResearchTopicRepository(session)
    record: dict[str, Any]
    if operation in {"create", "draft_create"}:
        created_record, created = await create_topic(
            session,
            actor=actor,
            payload=payload,
            draft=operation == "draft_create",
            idempotency_key=idempotency_key,
        )
        await session.commit()
        return {**created_record, "created": created}
    if operation == "update" and topic_id and expected_revision is not None:
        declared = status_declared if status_declared is not None else ("status" in payload)
        record = await update_topic(
            session,
            actor=actor,
            topic_id=topic_id,
            payload=payload,
            expected_revision=expected_revision,
            status_declared=declared,
        )
    elif operation == "activate" and topic_id:
        record = await activate_topic(
            session,
            actor=actor,
            topic_id=topic_id,
            payload=payload,
            expected_revision=expected_revision,
        )
    elif operation == "transition" and topic_id:
        record = await transition_topic(
            session,
            actor=actor,
            topic_id=topic_id,
            payload=payload,
            expected_revision=expected_revision,
        )
    elif operation == "append" and topic_id and idempotency_key and len(idempotency_key) <= 128:
        data = RoundInput.model_validate(payload).model_dump(mode="json")
        if data["supersedes_id"]:
            from finboard_persistence.models import ResearchTopicEntryModel

            old = await session.scalar(
                select(ResearchTopicEntryModel).where(
                    ResearchTopicEntryModel.entry_id == data["supersedes_id"],
                    ResearchTopicEntryModel.topic_id == topic_id,
                )
            )
            if old is None:
                raise ValueError("纠正链只能引用同课题已有轮次")
        record = record_out(await repo.append(topic_id, idempotency_key, data, actor))
    else:
        raise ValueError(
            "operation 必须 create/draft_create/update/activate/transition/append;"
            "更新/激活/转移需 expected_revision,追加需幂等键"
        )
    await session.commit()
    return record


async def memory_page(session: AsyncSession, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    if not 1 <= limit <= 50 or not 0 <= offset <= 100000:
        raise ValueError("分页范围非法")
    m = ResearchMemoryModel
    # Project excerpts before loading; old memories may contain very long text.
    stmt = (
        select(
            m.memory_id,
            m.memory_type,
            func.left(m.content, 1200).label("excerpt"),
            func.length(m.content).label("content_length"),
            m.status,
            literal_column(
                "CASE WHEN octet_length(source_refs::text)<=16384 THEN source_refs::jsonb ELSE '[]'::jsonb END"
            ).label("source_refs"),
            literal_column("octet_length(source_refs::text)>16384").label("refs_truncated"),
            m.created_by,
            m.confirmed_by,
            m.supersedes_id,
            m.created_at,
        )
        .order_by(m.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )
    rows = (await session.execute(stmt)).mappings().all()
    return {
        "items": [
            {
                **dict(r),
                "created_at": r["created_at"].isoformat(),
                "evidence_level": "agent_interpretation"
                if r["created_by"].startswith("agent:")
                else "human_record",
                "unverifiable_refs": r["refs_truncated"]
                or not r["source_refs"]
                or any(not x.get("ref_id") or x.get("kind") == "unknown" for x in r["source_refs"]),
            }
            for r in rows[:limit]
        ],
        "has_more": len(rows) > limit,
        "offset": offset,
        "limit": limit,
    }


async def source_fact(
    session: AsyncSession, reference: dict[str, Any], docs_root: Path
) -> dict[str, Any]:
    """Explicit provenance probe, bounded columns; status is never an opinion."""
    ref = EvidenceRef.model_validate(reference)
    if ref.kind == "document":

        def document_fact() -> dict[str, Any]:
            path = (docs_root / ref.ref_id).resolve()
            if (
                path.suffix != ".md"
                or not path.is_relative_to(docs_root.resolve())
                or not path.is_file()
            ):
                return {"status": "missing"}
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return {
                "status": "checksum_mismatch"
                if ref.checksum and ref.checksum != digest
                else "matched",
                "facts": {
                    "path": ref.ref_id,
                    "checksum": digest,
                    "evidence_level": "canonical_document",
                    "notice": "正文以仓库经PR维护的文件为准,引用存在不代表自动接受工作结论",
                },
            }

        return await asyncio.to_thread(document_fact)
    if ref.kind == "strategy" and not ref.version:
        return {"status": "version_required"}
    queries = {
        "research_run": "SELECT status, manifest_checksum AS checksum, manifest::jsonb->>'strategy_version' AS version, (result::jsonb->'metrics') - 'equity_curve' AS metrics FROM research_runs WHERE run_id=:ref_id",
        "strategy": "SELECT status, checksum, version FROM research_strategy_specs WHERE strategy_id=:ref_id AND version::text=:version",
        "dataset": "SELECT release_checksum AS checksum, version, quality_status, start_date, end_date FROM research_dataset_releases WHERE release_id=:ref_id",
        "experiment": "SELECT status, version_checksum AS checksum, final_test_unsealed FROM research_experiments WHERE experiment_id=:ref_id",
        "memory": "SELECT status, created_by, confirmed_by, supersedes_id FROM research_memories WHERE memory_id=:ref_id",
        "factor_series": "SELECT content_checksum AS checksum, code_commit AS version, code_artifact FROM research_factor_series WHERE series_id=:ref_id",
        "simulation": "SELECT status, strategy_version AS version FROM simulation_sessions WHERE simulation_session_id=:ref_id",
        "backtest": 'SELECT strategy, start, "end" FROM backtest_runs WHERE id::text=:ref_id',
    }
    row = (
        (
            await session.execute(
                text(queries[ref.kind]), {"ref_id": ref.ref_id, "version": ref.version}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return {"status": "missing"}
    facts = {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in row.items()}
    mismatched = bool(ref.checksum and ref.checksum != facts.get("checksum"))
    if ref.version and ref.version != str(facts.get("version")):
        return {"status": "version_mismatch", "facts": facts}
    if ref.kind == "experiment":
        # Delegate the domain's derived outcome instead of guessing from status.
        from finboard_backtest.validation import derive_oos_outcome
        from finboard_persistence.validation_repo import (
            ResearchExperimentRepository,
            ResearchTrialRepository,
        )

        size = (
            (
                await session.execute(
                    text(
                        "SELECT count(*) AS count, coalesce(sum(octet_length(row_to_json(t)::text)),0) AS bytes FROM research_trials t WHERE experiment_id=:ref_id"
                    ),
                    {"ref_id": ref.ref_id},
                )
            )
            .mappings()
            .one()
        )
        if size["count"] > 500 or size["bytes"] > 1048576:
            return {
                "status": "checksum_mismatch" if mismatched else "matched",
                "facts": {
                    **facts,
                    "oos_outcome": "unknown",
                    "evidence_level": "automatic_fact",
                    "notice": "实验trial摘要超限,未加载全部试验,不推断OOS结论",
                },
            }
        exp = await ResearchExperimentRepository(session).get(ref.ref_id)
        if exp:
            trials = await ResearchTrialRepository(session).list_by_experiment(ref.ref_id)
            facts["oos_outcome"] = derive_oos_outcome(exp, trials).value
    return {
        "status": "checksum_mismatch" if mismatched else "matched",
        "facts": {
            **facts,
            "evidence_level": "automatic_fact",
            "notice": "发布/完成不是OOS支持,历史工作解释须另行复核",
        },
    }
