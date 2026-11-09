"""Topic-scoped ResearchRun actions and run -> topic reverse links (#515).

REST is a thin adapter: parse the body, resolve the server-side
``ResearchOperationContext``, call :mod:`finboard_app.research_topic_actions`,
map unified ``ResearchOperationError`` to HTTP and own the single rollback on
failure. The application service owns the single commit.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_app.research_operation import (
    ResearchOperationContext,
    ResearchOperationError,
)
from finboard_app.research_run_submission import ResearchRunSubmitInput
from finboard_app.research_topic_actions import (
    cancel_topic_run,
    get_topic_run,
    link_run_to_topic,
    list_run_topics,
    list_topic_runs,
    retry_topic_run,
    start_topic_run,
)
from finboard_backtest.research_run import ResearchActorType

topic_runs_router = APIRouter(
    prefix="/api/research/topics", tags=["research-topic-runs"]
)
run_links_router = APIRouter(
    prefix="/api/research/runs", tags=["research-run-links"]
)


class TopicRunStartIn(ResearchRunSubmitInput):
    """Run submit input plus the topic goal version and optional operation id."""

    goal_version: str = Field(min_length=1, max_length=64)
    operation_id: str | None = Field(default=None, max_length=64)

    def run_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="python", exclude={"goal_version", "operation_id"}
        )


class TopicRunRetryIn(BaseModel):
    """Retry always creates a new replay run, so it needs a new operation id."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=128)
    requested_by: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=64)

    def replay_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude={"operation_id"})


class RunLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_id: str = Field(min_length=1, max_length=32)
    goal_version: str = Field(min_length=1, max_length=64)
    operation_id: str | None = Field(default=None, max_length=64)


def _sandbox_enabled(request: Request) -> bool:
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return bool(settings.research_sandbox_enabled)
    from finboard_app.config import load_settings

    return load_settings().research_sandbox_enabled


def _web_context(
    request: Request,
    *,
    operation_id: str | None,
    actor_type: ResearchActorType = ResearchActorType.HUMAN,
) -> ResearchOperationContext:
    return ResearchOperationContext.for_web(
        request_id=request.headers.get("x-request-id"),
        actor_type=actor_type,
        operation_id=operation_id,
    )


def _http_error(exc: ResearchOperationError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.to_detail())


@topic_runs_router.post("/{topic_id}/runs", status_code=201)
async def start(
    topic_id: str,
    body: TopicRunStartIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Start a run from an active topic (run + job + link + audit, one commit)."""

    try:
        result = await start_topic_run(
            session,
            topic_id=topic_id,
            goal_version=body.goal_version,
            payload=body.run_payload(),
            ctx=_web_context(
                request,
                operation_id=body.operation_id,
                actor_type=ResearchActorType(body.actor_type),
            ),
            sandbox_enabled=_sandbox_enabled(request),
        )
    except ResearchOperationError as exc:
        await session.rollback()
        raise _http_error(exc) from exc
    if not result.get("created"):
        response.status_code = 200
    return result


@topic_runs_router.get("/{topic_id}/runs")
async def list_runs(
    topic_id: str,
    limit: int = Query(20, ge=1, le=50),
    cursor: str | None = Query(default=None, max_length=32),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Links joined with live run/job state (progress, error, result checksum)."""

    try:
        return await list_topic_runs(session, topic_id, limit=limit, cursor=cursor)
    except ResearchOperationError as exc:
        raise _http_error(exc) from exc


@topic_runs_router.get("/{topic_id}/runs/{run_id}")
async def get_run(
    topic_id: str,
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        return await get_topic_run(session, topic_id, run_id)
    except ResearchOperationError as exc:
        raise _http_error(exc) from exc


@topic_runs_router.post("/{topic_id}/runs/{run_id}/retry", status_code=201)
async def retry(
    topic_id: str,
    run_id: str,
    body: TopicRunRetryIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Replay the linked run as a new ``RR-`` and link the new run to the topic."""

    try:
        result = await retry_topic_run(
            session,
            topic_id=topic_id,
            run_id=run_id,
            payload=body.replay_payload(),
            ctx=_web_context(request, operation_id=body.operation_id),
            sandbox_enabled=_sandbox_enabled(request),
        )
    except ResearchOperationError as exc:
        await session.rollback()
        raise _http_error(exc) from exc
    if not result.get("created"):
        response.status_code = 200
    return result


@topic_runs_router.post("/{topic_id}/runs/{run_id}/cancel")
async def cancel(
    topic_id: str,
    run_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Proxy the existing cooperative cancel; terminal runs are a 409."""

    try:
        return await cancel_topic_run(
            session,
            topic_id=topic_id,
            run_id=run_id,
            ctx=_web_context(request, operation_id=None),
        )
    except ResearchOperationError as exc:
        await session.rollback()
        raise _http_error(exc) from exc


@run_links_router.post("/{run_id}/link", status_code=201)
async def link_run(
    run_id: str,
    body: RunLinkIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Attach a standalone run to a topic (idempotent late link)."""

    try:
        result = await link_run_to_topic(
            session,
            run_id=run_id,
            topic_id=body.topic_id,
            goal_version=body.goal_version,
            operation_id=body.operation_id,
            ctx=_web_context(request, operation_id=body.operation_id),
        )
    except ResearchOperationError as exc:
        await session.rollback()
        raise _http_error(exc) from exc
    if not result.get("created"):
        response.status_code = 200
    return result


@run_links_router.get("/{run_id}/topics")
async def run_topics(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Reverse lookup: a run may be referenced by several topics."""

    try:
        return await list_run_topics(session, run_id)
    except ResearchOperationError as exc:
        raise _http_error(exc) from exc


__all__ = ["run_links_router", "topic_runs_router"]
