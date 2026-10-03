"""Shared REST/MCP contracts for research context, independent of OpenCode."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import ResearchMemoryModel
from finboard_persistence.research_topic_repo import ResearchTopicRepository


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
    return result


async def workspace_read(
    session: AsyncSession,
    *,
    topic_id: str | None = None,
    entries: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    if not 1 <= limit <= 50 or not 0 <= offset <= 100000:
        raise ValueError("分页范围: limit 1-50,offset 0-100000")
    repo = ResearchTopicRepository(session)
    if topic_id and not entries:
        return record_out(await repo.get(topic_id))
    rows = (
        await repo.entries(topic_id, limit + 1, offset)
        if topic_id
        else await repo.list_topics(limit + 1, offset)
    )
    items = [record_out(r) for r in rows[:limit]]
    if not topic_id:
        for item in items:
            item["excerpted"] = True
            for field in ("question", "summary", "next_step"):
                item[field] = item[field][:1200]
            item["open_questions"] = [q[:300] for q in item["open_questions"][:5]]
    return {
        "items": items,
        "has_more": len(rows) > limit,
        "offset": offset,
        "limit": limit,
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
) -> dict[str, Any]:
    repo = ResearchTopicRepository(session)
    row: Any
    if operation == "create":
        row = await repo.create(TopicInput.model_validate(payload).model_dump(mode="json"), actor)
    elif operation == "update" and topic_id and expected_revision is not None:
        row = await repo.update(
            topic_id,
            TopicInput.model_validate(payload).model_dump(mode="json"),
            expected_revision,
            actor,
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
        row = await repo.append(topic_id, idempotency_key, data, actor)
    else:
        raise ValueError(
            "operation 必须 create/update/append,更新需 expected_revision,追加需幂等键"
        )
    await session.commit()
    return record_out(row)


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
