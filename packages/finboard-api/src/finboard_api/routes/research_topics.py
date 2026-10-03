"""Research topic control plane. No execution side effects."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_app.research_explanation import decision_evidence, explain
from finboard_app.research_workspace import (
    RoundInput,
    TopicInput,
    memory_page,
    source_fact,
    workspace_read,
    workspace_write,
)

router = APIRouter(prefix="/api/research/topics", tags=["research-topics"])
explanation_router = APIRouter(prefix="/api/research/explanation", tags=["research-explanation"])


@explanation_router.get("")
async def explanation(
    run_id: str | None = None,
    strategy_id: str | None = None,
    version: int | None = Query(None, ge=1),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        return await explain(session, run_id=run_id, strategy_id=strategy_id, version=version)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@explanation_router.get("/{run_id}/decisions")
async def decisions(
    run_id: str,
    symbol: str | None = Query(None, max_length=32),
    decision_id: str | None = None,
    business_date: str | None = None,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100000),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        return await decision_evidence(
            session,
            run_id=run_id,
            symbol=symbol,
            decision_id=decision_id,
            business_date=business_date,
            limit=limit,
            offset=offset,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class TopicUpdate(TopicInput):
    expected_revision: int = Field(ge=1)


class RoundCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=128)
    round: RoundInput


async def _write(session: AsyncSession, **kwargs: Any) -> dict[str, Any]:
    try:
        return await workspace_write(session, actor="user:api", **kwargs)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("")
async def topics(
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0, le=100000),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    return await workspace_read(session, limit=limit, offset=offset)


@router.get("/memories")
async def memories(
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0, le=100000),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    return await memory_page(session, limit, offset)


@router.get("/source")
async def source(
    request: Request,
    kind: str,
    ref_id: str = Query(max_length=256),
    version: str | None = None,
    checksum: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    from finboard_api.routes.research_docs import _docs_root

    try:
        return await source_fact(
            session,
            {"kind": kind, "ref_id": ref_id, "version": version, "checksum": checksum},
            _docs_root(request),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{topic_id}")
async def topic(topic_id: str, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    try:
        return await workspace_read(session, topic_id=topic_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{topic_id}/entries")
async def entries(
    topic_id: str,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0, le=100000),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        return await workspace_read(
            session, topic_id=topic_id, entries=True, limit=limit, offset=offset
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("", status_code=201)
async def create(
    body: TopicInput, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    return await _write(session, operation="create", payload=body.model_dump(mode="json"))


@router.put("/{topic_id}")
async def update(
    topic_id: str, body: TopicUpdate, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    return await _write(
        session,
        operation="update",
        topic_id=topic_id,
        expected_revision=body.expected_revision,
        payload=body.model_dump(mode="json", exclude={"expected_revision"}),
    )


@router.post("/{topic_id}/entries", status_code=201)
async def append(
    topic_id: str, body: RoundCreate, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    return await _write(
        session,
        operation="append",
        topic_id=topic_id,
        idempotency_key=body.idempotency_key,
        payload=body.round.model_dump(mode="json"),
    )
