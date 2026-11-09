"""Topic-scoped factor / sandbox / OOS actions and artifact links (#516).

The REST layer is a thin adapter: parse the body, resolve the server-side
``ResearchOperationContext``, call
:mod:`finboard_app.research_topic_artifacts`, and map the unified
``ResearchOperationError`` family to HTTP. The application service owns the
single commit (artifact/job + link + audit).

The request model is ``extra="forbid"`` and contains **no** code field: the Web
channel can only reference an already-registered ``research_code_artifacts`` row
via ``code_run.execute``. Python source / module paths / expressions stay
MCP-only (``finboard_research_code_submit``).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_app.research_operation import (
    ResearchOperationContext,
    ResearchOperationError,
)
from finboard_app.research_topic_artifacts import (
    TOPIC_ACTION_ENTITY_TYPES,
    list_topic_artifact_links,
    run_topic_action,
)
from finboard_backtest.research_run import ResearchActorType

topic_actions_router = APIRouter(prefix="/api/research/topics", tags=["research-topic-actions"])


class TopicActionIn(BaseModel):
    """One whitelisted action; ``payload`` is validated per action server-side."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "run.submit",
        "run.replay",
        "factor_experiment.create",
        "code_run.execute",
        "validation_experiment.create",
        "validation_experiment.add_trial",
        "validation_experiment.unseal",
    ]
    goal_version: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    operation_id: str | None = Field(default=None, min_length=1, max_length=64)
    actor_type: Literal["human", "agent"] = "human"


def _sandbox_enabled(request: Request) -> bool:
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return bool(getattr(settings, "research_sandbox_enabled", False))
    from finboard_app.config import load_settings

    return load_settings().research_sandbox_enabled


def _context(request: Request, body: TopicActionIn) -> ResearchOperationContext:
    return ResearchOperationContext.for_web(
        request_id=request.headers.get("x-request-id"),
        actor_type=ResearchActorType(body.actor_type),
        operation_id=body.operation_id,
    )


def _http_error(exc: ResearchOperationError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.to_detail())


@topic_actions_router.post("/{topic_id}/actions", status_code=201)
async def topic_action(
    topic_id: str,
    body: TopicActionIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Execute one whitelisted topic action (never auto-runs from a save)."""

    try:
        result = await run_topic_action(
            session,
            topic_id=topic_id,
            action=body.action,
            goal_version=body.goal_version,
            payload=body.payload,
            ctx=_context(request, body),
            sandbox_enabled=_sandbox_enabled(request),
        )
    except ResearchOperationError as exc:
        await session.rollback()
        raise _http_error(exc) from exc
    # 201 whenever *something* was newly created; 200 only for a pure receipt
    # replay (both flags false). #515 run receipts keep their own `created`.
    created = bool(
        result.get("artifact_created")
        or result.get("link_created")
        or result.get("created", False)
    )
    if not created:
        response.status_code = 200
    return result


@topic_actions_router.get("/{topic_id}/artifacts")
async def topic_artifacts(
    topic_id: str,
    limit: int = Query(20, ge=1, le=50),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Immutable links to factor experiments / sandbox runs / OOS experiments.

    State, ``oos_outcome``, trial budget, ``final_test_unsealed`` and named
    evidence gaps are derived at read time; nothing is stored on the link.
    """

    try:
        return await list_topic_artifact_links(session, topic_id, limit=limit)
    except ResearchOperationError as exc:
        raise _http_error(exc) from exc


@topic_actions_router.get("/{topic_id}/artifacts/actions")
async def topic_action_catalog(topic_id: str) -> dict[str, Any]:
    """The action whitelist plus the ``entity_type`` each action links."""

    return {
        "topic_id": topic_id,
        "actions": [
            {"action": action, "entity_type": entity_type}
            for action, entity_type in sorted(TOPIC_ACTION_ENTITY_TYPES.items())
        ],
        "code_channel": "mcp_only",
        "notice": "Web 只能引用已登记 artifact;代码提交仅在 MCP finboard_research_code_submit",
    }


__all__ = ["topic_actions_router"]
