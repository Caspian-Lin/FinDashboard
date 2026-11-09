"""Topic-initiated ResearchRun actions (#515).

This module is the single implementation behind the REST topic-run endpoints and
the ``finboard_topic_run_*`` MCP tools. It owns the one transaction that makes
the topic link, the frozen run, its background job and the research audit event
visible together: :class:`~finboard_app.research_run_submission.ResearchRunSubmissionService`
only flushes, and this service performs the unique ``commit()``.

Invariants:

* the topic row is locked (``SELECT ... FOR UPDATE`` + ``populate_existing``)
  before validation, so two starts for the same topic serialize and the second
  sees the first link (lost-receipt replay);
* a link is immutable -- live run/job state is derived by joining at read time;
* retry always goes through the existing replay implementation (new ``RR-`` with
  ``replay_of_run_id``), never reuses the old run idempotency key;
* every write path records ``topic.run.*`` audit events: success in the caller
  session (same transaction), rejection/failure via a detached session so the
  original error is never masked;
* research domain only -- no live ``orders`` / ``fills`` / ``positions`` /
  ``audit_logs`` writes.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_app.research_audit import (
    ResearchAuditEvent,
    research_audit_recorder,
)
from finboard_app.research_operation import (
    ResearchConflictError,
    ResearchInvalidInputError,
    ResearchNotFoundError,
    ResearchOperationContext,
    ResearchOperationError,
    ResearchPreconditionFailedError,
)
from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_app.research_run_submission import (
    ResearchRunReplayInput,
    ResearchRunSubmissionService,
    ResearchRunSubmitInput,
)
from finboard_backtest.research_run import (
    ResearchRunConflictError,
    ResearchRunCoordinator,
    ResearchRunStatus,
)
from finboard_persistence.background_job_repo import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)
from finboard_persistence.models import ResearchTopicModel
from finboard_persistence.research_run_repo import (
    ResearchRunPersistenceConflictError,
    ResearchRunRepository,
)
from finboard_persistence.research_topic_link_repo import (
    ResearchTopicLinkConflictError,
    ResearchTopicLinkRepository,
    RunTopicJoin,
    TopicRunJoin,
)
from finboard_persistence.research_topic_repo import ResearchTopicRepository

#: ``entity_type`` for a ResearchRun link (the only kind #515 writes; #516 reuses
#: the same table with its own entity kinds).
RESEARCH_RUN_ENTITY = "research_run"

#: Run states that cannot be cancelled from the topic endpoint.
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {
        ResearchRunStatus.COMPLETED.value,
        ResearchRunStatus.FAILED.value,
        ResearchRunStatus.CANCELLED.value,
    }
)

LinkState = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]

_JOB_FAILED = "failed"
_JOB_CANCELLED = "cancelled"
_JOB_SUCCEEDED = "succeeded"
_JOB_INTERRUPTED = "interrupted"
_JOB_RUNNING = frozenset(
    {"running", "cancel_requested", "retry_waiting", "leased"}
)


def _valid_operation_id(operation_id: str | None) -> None:
    """Reject empty/oversized operation ids before they reach the unique index."""

    if operation_id is None:
        return
    if not operation_id.strip() or len(operation_id) > 64:
        raise ResearchInvalidInputError(
            "operation_id 必须为 1-64 个非空字符", code="invalid_operation_id"
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _current_goal_version(topic: ResearchTopicModel) -> str | None:
    payload = topic.payload if isinstance(topic.payload, dict) else {}
    goal = payload.get("goal")
    if not isinstance(goal, dict):
        return None
    version = goal.get("version")
    return version if isinstance(version, str) else None


def _topic_summary(topic: ResearchTopicModel) -> dict[str, Any]:
    payload = topic.payload if isinstance(topic.payload, dict) else {}
    return {
        "topic_id": topic.topic_id,
        "title": payload.get("title"),
        "status": topic.status,
        "revision": topic.revision,
        "goal_version": _current_goal_version(topic),
    }


async def _require_topic(
    session: AsyncSession, topic_id: str, *, lock: bool = False
) -> ResearchTopicModel:
    try:
        return await ResearchTopicRepository(session).get(
            topic_id, lock=lock, populate_existing=lock
        )
    except LookupError as exc:
        raise ResearchNotFoundError(str(exc), code="topic_not_found") from exc


def _require_active_with_goal(topic: ResearchTopicModel, goal_version: str) -> None:
    """Precondition gate shared by start and retry (409 on drift)."""

    if topic.status != "active":
        raise ResearchPreconditionFailedError(
            f"课题当前状态为 {topic.status},只有 active 课题可以发起或重试研究运行",
            code="topic_not_active",
            details={"topic_id": topic.topic_id, "status": topic.status},
        )
    current = _current_goal_version(topic)
    if goal_version != current:
        raise ResearchPreconditionFailedError(
            "目标版本不匹配:课题当前目标已变化,请重新读取后再发起",
            code="goal_version_mismatch",
            details={
                "topic_id": topic.topic_id,
                "current": current,
                "declared": goal_version,
            },
        )


def _require_goal_version(topic: ResearchTopicModel, goal_version: str) -> None:
    """Late linking only needs the exact goal version, not the active state."""

    current = _current_goal_version(topic)
    if goal_version != current:
        raise ResearchPreconditionFailedError(
            "目标版本不匹配:链接必须指向课题当前目标版本",
            code="goal_version_mismatch",
            details={
                "topic_id": topic.topic_id,
                "current": current,
                "declared": goal_version,
            },
        )


def derive_link_state(
    run_status: str | None, job_status: str | None
) -> LinkState:
    """Derive the single display state from run/job facts (no stored state).

    The run is the domain authority when it is terminal. Otherwise the job state
    is used because it carries progress and retry semantics; ``retry_waiting`` is
    shown as ``running`` (the worker is still responsible for the run), while a
    lost lease maps to ``interrupted`` for explicit replay.
    """

    terminal = {
        ResearchRunStatus.COMPLETED.value: "succeeded",
        ResearchRunStatus.FAILED.value: "failed",
        ResearchRunStatus.CANCELLED.value: "cancelled",
        ResearchRunStatus.INTERRUPTED.value: "interrupted",
    }
    if run_status in terminal:
        return terminal[run_status]  # type: ignore[return-value]
    if job_status == _JOB_SUCCEEDED:
        return "succeeded"
    if job_status == _JOB_FAILED:
        return "failed"
    if job_status == _JOB_CANCELLED:
        return "cancelled"
    if job_status == _JOB_INTERRUPTED:
        return "interrupted"
    if job_status in _JOB_RUNNING:
        return "running"
    if run_status == ResearchRunStatus.RUNNING.value:
        return "running"
    return "queued"


def _link_payload(join: TopicRunJoin) -> dict[str, Any]:
    link = join.link
    run = join.run
    job = join.job
    run_status = run.status if run is not None else None
    job_status = job.status if job is not None else None
    run_id = link.entity_id
    return {
        "link_id": link.id,
        "topic_id": link.topic_id,
        "goal_version": link.goal_version,
        "entry_id": link.entry_id,
        "entity_type": link.entity_type,
        "entity_id": link.entity_id,
        "run_id": run_id,
        "job_id": link.job_id or (run.job_id if run is not None else None),
        "operation_id": link.operation_id,
        "created_by": link.created_by,
        "created_at": _iso(link.created_at),
        "state": derive_link_state(run_status, job_status),
        "run_status": run_status,
        "job_status": job_status,
        "progress_done": job.progress_done if job is not None else None,
        "progress_total": job.progress_total if job is not None else None,
        "phase": job.phase if job is not None else None,
        "job_attempt": job.attempt if job is not None else None,
        "job_max_attempts": job.max_attempts if job is not None else None,
        "error_code": (run.error_code if run is not None else None)
        or (job.error_code if job is not None else None),
        "error_summary": (run.error_summary if run is not None else None)
        or (job.error_summary if job is not None else None),
        "result_checksum": run.result_checksum if run is not None else None,
        "manifest_checksum": run.manifest_checksum if run is not None else None,
        "run_created_at": _iso(run.created_at) if run is not None else None,
        "run_completed_at": _iso(run.completed_at) if run is not None else None,
        "deep_link": f"/research/runs?run={run_id}",
    }


def _params_summary(
    *,
    topic_id: str | None,
    goal_version: str | None,
    payload: Any,
    run_id: str | None = None,
    job_id: str | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    summary: dict[str, Any] = {
        "topic_id": topic_id,
        "goal_version": goal_version,
        "strategy_id": data.get("strategy_id"),
        "strategy_version": data.get("strategy_version"),
        "operation_id": operation_id,
    }
    if run_id is not None:
        summary["run_id"] = run_id
    if job_id is not None:
        summary["job_id"] = job_id
    return {key: value for key, value in summary.items() if value is not None}


def _idempotency_key(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("idempotency_key")
    return value if isinstance(value, str) and len(value) <= 128 else None


async def _record_success(
    session: AsyncSession,
    *,
    operation: str,
    ctx: ResearchOperationContext,
    topic_id: str,
    run_id: str,
    job_id: str | None,
    goal_version: str,
    result_code: str,
    params: dict[str, Any],
    idempotency_key: str | None,
    extra_correlation: dict[str, str] | None = None,
) -> None:
    correlation = {
        "topic_id": topic_id,
        "goal_version": goal_version,
        "run_id": run_id,
    }
    if job_id:
        correlation["job_id"] = job_id
    if ctx.operation_id:
        correlation["operation_id"] = ctx.operation_id
    if extra_correlation:
        correlation.update(extra_correlation)
    await research_audit_recorder().record(
        session,
        ResearchAuditEvent(
            operation=operation,
            outcome="succeeded",
            context=ctx,
            topic_id=topic_id,
            entity_type=RESEARCH_RUN_ENTITY,
            entity_id=run_id,
            correlation=correlation,
            result_code=result_code,
            params_summary=params,
            idempotency_key=idempotency_key,
        ),
    )


async def _record_failure(
    *,
    operation: str,
    ctx: ResearchOperationContext,
    topic_id: str | None,
    exc: BaseException,
    params: dict[str, Any],
    idempotency_key: str | None,
    correlation: dict[str, str] | None = None,
) -> None:
    """Detached rejection/failure audit; never masks the original error."""

    rejected = isinstance(
        exc, (ResearchOperationError, ResearchTopicLinkConflictError)
    )
    code = exc.code if isinstance(exc, ResearchOperationError) else type(exc).__name__
    merged = {"topic_id": topic_id} if topic_id else {}
    if correlation:
        merged.update(correlation)
    await research_audit_recorder().record_detached(
        ResearchAuditEvent(
            operation=operation,
            outcome="rejected" if rejected else "failed",
            context=ctx,
            topic_id=topic_id,
            entity_type=RESEARCH_RUN_ENTITY if correlation else None,
            entity_id=correlation.get("run_id") if correlation else None,
            correlation=merged,
            result_code=code,
            params_summary=params,
            error_summary=str(exc),
            idempotency_key=idempotency_key,
        )
    )


def _start_response(
    join: TopicRunJoin | None,
    *,
    topic_id: str,
    goal_version: str,
    run_id: str,
    job_id: str | None,
    operation_id: str | None,
    created: bool,
    status: str | None,
    manifest_checksum: str | None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "topic_id": topic_id,
        "goal_version": goal_version,
        "run_id": run_id,
        "job_id": job_id,
        "operation_id": operation_id,
        "created": created,
        "status": status,
        "manifest_checksum": manifest_checksum,
    }
    if join is not None:
        response["link"] = _link_payload(join)
    return response


async def start_topic_run(
    session: AsyncSession,
    *,
    topic_id: str,
    goal_version: str,
    payload: dict[str, Any],
    ctx: ResearchOperationContext,
    sandbox_enabled: bool = False,
    release_root: Path | None = None,
) -> dict[str, Any]:
    """Start a run from a topic inside one transaction (run+job+link+audit).

    The caller (REST route / MCP handler) must **not** commit before calling;
    this function commits exactly once. On error it re-raises after writing a
    detached rejection audit and leaves rollback to the caller.
    """

    operation = "topic.run.start"
    link_repo = ResearchTopicLinkRepository(session)
    idempotency_key = _idempotency_key(payload)
    params = _params_summary(
        topic_id=topic_id,
        goal_version=goal_version,
        payload=payload,
        operation_id=ctx.operation_id,
    )
    run_id: str | None = None
    job_id: str | None = None
    try:
        _valid_operation_id(ctx.operation_id)
        topic = await _require_topic(session, topic_id, lock=True)
        _require_active_with_goal(topic, goal_version)

        # Lost receipt: the operation id is the idempotency anchor. The topic
        # lock above serializes same-topic concurrent retries, so this read sees
        # the first transaction's committed link.
        if ctx.operation_id:
            bound = await link_repo.get_by_operation_id(ctx.operation_id)
            if bound is not None:
                if (
                    bound.topic_id != topic_id
                    or bound.entity_type != RESEARCH_RUN_ENTITY
                ):
                    raise ResearchConflictError(
                        "operation_id 已绑定其他课题或产物;不能跨课题复用",
                        code="operation_id_bound",
                        details={
                            "operation_id": ctx.operation_id,
                            "topic_id": bound.topic_id,
                            "entity_type": bound.entity_type,
                            "entity_id": bound.entity_id,
                        },
                    )
                run_id = bound.entity_id
                join = await link_repo.get_run_link(topic_id, run_id)
                if join is None:  # pragma: no cover - 防御性(行被并发删除)
                    raise ResearchNotFoundError(
                        "operation_id 对应的链接不存在", code="link_not_found"
                    )
                job_id = join.link.job_id or (
                    join.run.job_id if join.run is not None else None
                )
                await _record_success(
                    session,
                    operation=operation,
                    ctx=ctx,
                    topic_id=topic_id,
                    run_id=run_id,
                    job_id=job_id,
                    goal_version=goal_version,
                    result_code="idempotent_hit",
                    params=params,
                    idempotency_key=idempotency_key,
                )
                await session.commit()
                return _start_response(
                    join,
                    topic_id=topic_id,
                    goal_version=goal_version,
                    run_id=run_id,
                    job_id=job_id,
                    operation_id=ctx.operation_id,
                    created=False,
                    status=join.run.status if join.run is not None else None,
                    manifest_checksum=(
                        join.run.manifest_checksum if join.run is not None else None
                    ),
                )

        try:
            body = ResearchRunSubmitInput.from_json_payload(payload)
        except (ValidationError, ValueError, ArithmeticError) as exc:
            raise ResearchInvalidInputError(
                f"研究运行入队参数非法: {exc}", code="invalid_run_payload"
            ) from exc

        outcome = await ResearchRunSubmissionService(
            session, sandbox_enabled=sandbox_enabled, release_root=release_root
        ).submit(body, ctx)
        run_id = outcome.run_row.run_id
        job_id = outcome.run_row.job_id
        try:
            _link, link_created = await link_repo.create_or_get(
                topic_id=topic_id,
                goal_version=goal_version,
                entity_type=RESEARCH_RUN_ENTITY,
                entity_id=run_id,
                job_id=job_id,
                operation_id=ctx.operation_id,
                created_by=ctx.actor,
            )
        except ResearchTopicLinkConflictError as exc:
            raise ResearchConflictError(str(exc), code="link_conflict") from exc
        params.update(
            _params_summary(
                topic_id=topic_id,
                goal_version=goal_version,
                payload=payload,
                run_id=run_id,
                job_id=job_id,
                operation_id=ctx.operation_id,
            )
        )
        await _record_success(
            session,
            operation=operation,
            ctx=ctx,
            topic_id=topic_id,
            run_id=run_id,
            job_id=job_id,
            goal_version=goal_version,
            result_code=(
                "created" if (outcome.created or link_created) else "idempotent_hit"
            ),
            params=params,
            idempotency_key=idempotency_key,
        )
        join = await link_repo.get_run_link(topic_id, run_id)
        await session.commit()
        return _start_response(
            join,
            topic_id=topic_id,
            goal_version=goal_version,
            run_id=run_id,
            job_id=job_id,
            operation_id=ctx.operation_id,
            created=bool(outcome.created or link_created),
            status=outcome.run_row.status,
            manifest_checksum=outcome.manifest.checksum,
        )
    except Exception as exc:
        correlation: dict[str, str] = {}
        if run_id:
            correlation["run_id"] = run_id
        if job_id:
            correlation["job_id"] = job_id
        await _record_failure(
            operation=operation,
            ctx=ctx,
            topic_id=topic_id,
            exc=exc,
            params=params,
            idempotency_key=idempotency_key,
            correlation=correlation,
        )
        raise


async def list_topic_runs(
    session: AsyncSession,
    topic_id: str,
    *,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    if not 1 <= limit <= 50:
        raise ResearchInvalidInputError("limit 范围: 1-50", code="invalid_pagination")
    cursor_id: int | None = None
    if cursor is not None:
        try:
            cursor_id = int(cursor)
        except ValueError as exc:
            raise ResearchInvalidInputError(
                "cursor 必须是链接 id 的十进制字符串", code="invalid_cursor"
            ) from exc
        if cursor_id <= 0:
            raise ResearchInvalidInputError("cursor 必须为正整数", code="invalid_cursor")
    await _require_topic(session, topic_id)
    rows = await ResearchTopicLinkRepository(session).list_runs_for_topic(
        topic_id, limit=limit, cursor=cursor_id
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "items": [_link_payload(row) for row in page],
        "has_more": has_more,
        "next_cursor": str(page[-1].link.id) if has_more and page else None,
        "limit": limit,
    }


async def get_topic_run(
    session: AsyncSession, topic_id: str, run_id: str
) -> dict[str, Any]:
    await _require_topic(session, topic_id)
    join = await ResearchTopicLinkRepository(session).get_run_link(topic_id, run_id)
    if join is None:
        raise ResearchNotFoundError(
            f"课题 {topic_id} 未关联研究运行 {run_id}", code="topic_run_not_found"
        )
    return _link_payload(join)


async def list_run_topics(session: AsyncSession, run_id: str) -> dict[str, Any]:
    if await ResearchRunRepository(session).get(run_id) is None:
        raise ResearchNotFoundError("研究运行不存在", code="run_not_found")
    rows = await ResearchTopicLinkRepository(session).list_topics_for_run(run_id)
    return {
        "run_id": run_id,
        "items": [_run_topic_payload(row) for row in rows],
        "count": len(rows),
    }


def _run_topic_payload(row: RunTopicJoin) -> dict[str, Any]:
    link = row.link
    topic = row.topic
    payload = topic.payload if isinstance(topic.payload, dict) else {}
    return {
        "link_id": link.id,
        "topic_id": link.topic_id,
        "title": payload.get("title"),
        "topic_status": topic.status,
        "topic_revision": topic.revision,
        "goal_version": link.goal_version,
        "entry_id": link.entry_id,
        "operation_id": link.operation_id,
        "created_by": link.created_by,
        "created_at": _iso(link.created_at),
    }


async def retry_topic_run(
    session: AsyncSession,
    *,
    topic_id: str,
    run_id: str,
    payload: dict[str, Any],
    ctx: ResearchOperationContext,
    sandbox_enabled: bool = False,
    release_root: Path | None = None,
) -> dict[str, Any]:
    """Replay a topic-linked run as a new queued run plus a new link.

    A new ``operation_id`` is mandatory (the old one already names the original
    link); ``goal_version`` is inherited from the original link and must still
    match the topic's current goal.
    """

    operation = "topic.run.retry"
    link_repo = ResearchTopicLinkRepository(session)
    idempotency_key = _idempotency_key(payload)
    params = _params_summary(
        topic_id=topic_id,
        goal_version=None,
        payload=payload,
        operation_id=ctx.operation_id,
    )
    params["replay_of_run_id"] = run_id
    new_run_id: str | None = None
    new_job_id: str | None = None
    try:
        _valid_operation_id(ctx.operation_id)
        topic = await _require_topic(session, topic_id, lock=True)
        original = await link_repo.get(topic_id, RESEARCH_RUN_ENTITY, run_id)
        if original is None:
            raise ResearchNotFoundError(
                f"课题 {topic_id} 未关联研究运行 {run_id}", code="topic_run_not_found"
            )
        _require_active_with_goal(topic, original.goal_version)
        if not ctx.operation_id:
            raise ResearchInvalidInputError(
                "重试必须提供新的 operation_id", code="operation_id_required"
            )
        bound = await link_repo.get_by_operation_id(ctx.operation_id)
        if bound is not None:
            if (
                bound.topic_id != topic_id
                or bound.entity_type != RESEARCH_RUN_ENTITY
            ):
                raise ResearchConflictError(
                    "operation_id 已绑定其他课题或产物;不能跨课题复用",
                    code="operation_id_bound",
                    details={
                        "operation_id": ctx.operation_id,
                        "topic_id": bound.topic_id,
                    },
                )
            existing_run = await ResearchRunRepository(session).get(bound.entity_id)
            if existing_run is None or existing_run.replay_of_run_id != run_id:
                raise ResearchConflictError(
                    "operation_id 已绑定其他研究运行;重试须使用新键",
                    code="operation_id_bound",
                    details={"operation_id": ctx.operation_id, "run_id": bound.entity_id},
                )
            join = await link_repo.get_run_link(topic_id, bound.entity_id)
            if join is None:  # pragma: no cover - 防御性
                raise ResearchNotFoundError(
                    "operation_id 对应的链接不存在", code="link_not_found"
                )
            await _record_success(
                session,
                operation=operation,
                ctx=ctx,
                topic_id=topic_id,
                run_id=bound.entity_id,
                job_id=join.link.job_id,
                goal_version=original.goal_version,
                result_code="idempotent_hit",
                params=params,
                idempotency_key=idempotency_key,
                extra_correlation={"replay_of_run_id": run_id},
            )
            await session.commit()
            return _retry_response(join, created=False, goal_version=original.goal_version)

        try:
            body = ResearchRunReplayInput.model_validate(payload)
        except ValidationError as exc:
            raise ResearchInvalidInputError(
                f"研究运行重放参数非法: {exc}", code="invalid_replay_payload"
            ) from exc

        outcome = await ResearchRunSubmissionService(
            session, sandbox_enabled=sandbox_enabled, release_root=release_root
        ).replay(run_id, body, ctx)
        new_run_id = outcome.run_row.run_id
        new_job_id = outcome.run_row.job_id
        try:
            _link, link_created = await link_repo.create_or_get(
                topic_id=topic_id,
                goal_version=original.goal_version,
                entity_type=RESEARCH_RUN_ENTITY,
                entity_id=new_run_id,
                job_id=new_job_id,
                operation_id=ctx.operation_id,
                created_by=ctx.actor,
            )
        except ResearchTopicLinkConflictError as exc:
            raise ResearchConflictError(str(exc), code="link_conflict") from exc
        params.update(
            _params_summary(
                topic_id=topic_id,
                goal_version=original.goal_version,
                payload=payload,
                run_id=new_run_id,
                job_id=new_job_id,
                operation_id=ctx.operation_id,
            )
        )
        await _record_success(
            session,
            operation=operation,
            ctx=ctx,
            topic_id=topic_id,
            run_id=new_run_id,
            job_id=new_job_id,
            goal_version=original.goal_version,
            result_code=(
                "created" if (outcome.created or link_created) else "idempotent_hit"
            ),
            params=params,
            idempotency_key=idempotency_key,
            extra_correlation={"replay_of_run_id": run_id},
        )
        join = await link_repo.get_run_link(topic_id, new_run_id)
        await session.commit()
        return _retry_response(
            join, created=bool(outcome.created or link_created),
            goal_version=original.goal_version,
        )
    except Exception as exc:
        correlation: dict[str, str] = {"replay_of_run_id": run_id}
        if new_run_id:
            correlation["run_id"] = new_run_id
        if new_job_id:
            correlation["job_id"] = new_job_id
        await _record_failure(
            operation=operation,
            ctx=ctx,
            topic_id=topic_id,
            exc=exc,
            params=params,
            idempotency_key=idempotency_key,
            correlation=correlation,
        )
        raise


def _retry_response(
    join: TopicRunJoin | None, *, created: bool, goal_version: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "topic_id": join.link.topic_id if join is not None else None,
        "goal_version": goal_version,
        "created": created,
    }
    if join is not None:
        payload["link"] = _link_payload(join)
        payload["run_id"] = join.link.entity_id
        payload["job_id"] = join.link.job_id
        payload["status"] = join.run.status if join.run is not None else None
    return payload


async def cancel_topic_run(
    session: AsyncSession,
    *,
    topic_id: str,
    run_id: str,
    ctx: ResearchOperationContext,
) -> dict[str, Any]:
    """Proxy the existing cooperative cancel for a topic-linked run."""

    operation = "topic.run.cancel"
    link_repo = ResearchTopicLinkRepository(session)
    params = {"topic_id": topic_id, "run_id": run_id}
    try:
        await _require_topic(session, topic_id)
        join = await link_repo.get_run_link(topic_id, run_id)
        if join is None or join.run is None:
            raise ResearchNotFoundError(
                f"课题 {topic_id} 未关联研究运行 {run_id}", code="topic_run_not_found"
            )
        if join.run.status in TERMINAL_RUN_STATUSES:
            raise ResearchConflictError(
                f"研究运行已终态({join.run.status}),不能取消",
                code="run_terminal",
                details={"run_id": run_id, "status": join.run.status},
            )
        store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
        try:
            record = await ResearchRunCoordinator(store).cancel(run_id)
            if record.job_id:
                with contextlib.suppress(BackgroundJobPersistenceConflictError):
                    await BackgroundJobRepository(session).request_cancel(
                        record.job_id
                    )
        except (
            ResearchRunConflictError,
            ResearchRunPersistenceConflictError,
        ) as exc:
            raise ResearchConflictError(str(exc), code="run_not_cancellable") from exc
        await _record_success(
            session,
            operation=operation,
            ctx=ctx,
            topic_id=topic_id,
            run_id=run_id,
            job_id=record.job_id,
            goal_version=join.link.goal_version,
            result_code="cancelled",
            params=params,
            idempotency_key=None,
        )
        fresh = await link_repo.get_run_link(topic_id, run_id)
        if fresh is None:  # pragma: no cover - 防御性(行不可被本事务删除)
            raise ResearchNotFoundError("链接不存在", code="link_not_found")
        await session.commit()
        return _link_payload(fresh)
    except Exception as exc:
        await _record_failure(
            operation=operation,
            ctx=ctx,
            topic_id=topic_id,
            exc=exc,
            params=params,
            idempotency_key=None,
            correlation={"run_id": run_id},
        )
        raise


async def link_run_to_topic(
    session: AsyncSession,
    *,
    run_id: str,
    topic_id: str,
    goal_version: str,
    operation_id: str | None,
    ctx: ResearchOperationContext,
) -> dict[str, Any]:
    """Idempotently attach an already-existing (standalone) run to a topic."""

    operation = "topic.run.link"
    link_repo = ResearchTopicLinkRepository(session)
    params = {
        "topic_id": topic_id,
        "run_id": run_id,
        "goal_version": goal_version,
        "operation_id": operation_id,
    }
    try:
        _valid_operation_id(operation_id)
        _valid_operation_id(ctx.operation_id)
        topic = await _require_topic(session, topic_id, lock=True)
        _require_goal_version(topic, goal_version)
        run = await ResearchRunRepository(session).get(run_id)
        if run is None:
            raise ResearchNotFoundError("研究运行不存在", code="run_not_found")
        if operation_id is not None:
            bound = await link_repo.get_by_operation_id(operation_id)
            if bound is not None and (
                bound.topic_id != topic_id
                or bound.entity_type != RESEARCH_RUN_ENTITY
                or bound.entity_id != run_id
            ):
                raise ResearchConflictError(
                    "operation_id 已绑定其他课题或产物;不能跨课题复用",
                    code="operation_id_bound",
                    details={
                        "operation_id": operation_id,
                        "topic_id": bound.topic_id,
                        "entity_id": bound.entity_id,
                    },
                )
        try:
            link, created = await link_repo.create_or_get(
                topic_id=topic_id,
                goal_version=goal_version,
                entity_type=RESEARCH_RUN_ENTITY,
                entity_id=run_id,
                job_id=run.job_id,
                operation_id=operation_id,
                created_by=ctx.actor,
            )
        except ResearchTopicLinkConflictError as exc:
            raise ResearchConflictError(str(exc), code="link_conflict") from exc
        if not created and link.goal_version != goal_version:
            raise ResearchConflictError(
                "同一课题/运行已按其他目标版本建立链接;链接不可变,请读取原链接",
                code="link_goal_version_conflict",
                details={
                    "link_id": link.id,
                    "linked_goal_version": link.goal_version,
                    "declared": goal_version,
                },
            )
        # Keep the model's operation id visible for the response/audit even when
        # an existing link won on (topic, entity) without storing this new key.
        await _record_success(
            session,
            operation=operation,
            ctx=ctx,
            topic_id=topic_id,
            run_id=run_id,
            job_id=run.job_id,
            goal_version=goal_version,
            result_code="created" if created else "idempotent_hit",
            params=params,
            idempotency_key=None,
        )
        join = await link_repo.get_run_link(topic_id, run_id)
        if join is None:  # pragma: no cover - 防御性
            raise ResearchNotFoundError("链接创建后不可见", code="link_not_found")
        await session.commit()
        payload = _link_payload(join)
        payload["created"] = created
        return payload
    except Exception as exc:
        await _record_failure(
            operation=operation,
            ctx=ctx,
            topic_id=topic_id,
            exc=exc,
            params=params,
            idempotency_key=None,
            correlation={"run_id": run_id},
        )
        raise


__all__ = [
    "RESEARCH_RUN_ENTITY",
    "TERMINAL_RUN_STATUSES",
    "cancel_topic_run",
    "derive_link_state",
    "get_topic_run",
    "link_run_to_topic",
    "list_run_topics",
    "list_topic_runs",
    "retry_topic_run",
    "start_topic_run",
]
