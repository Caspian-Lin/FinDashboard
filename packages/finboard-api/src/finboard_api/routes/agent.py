"""OpenCode 研究会话路由(issue #109)。

提供研究会话的创建、列表、详情、SSE 事件流、消息发送、中断与历史回放。
所有操作经 :class:`ConversationService` 编排 OpenCode 运行时,不触碰实盘交易域。

红线:OpenCode 是受控研究运行时;未启用时端点返回 503。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from finboard_api.deps import get_conversation_service
from finboard_opencode import (
    ConversationNotFoundError,
    ConversationService,
    OpenCodeRuntimeError,
    OpenCodeUnavailableError,
)

router = APIRouter(prefix="/api/agent/conversations", tags=["agent-opencode"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ConversationCreate(BaseModel):
    title: str | None = None
    agent_run_id: str | None = Field(default=None, max_length=32)
    agent: str | None = None
    model: str | None = None


class ConversationOut(BaseModel):
    conversation_id: str
    opencode_session_id: str
    agent_run_id: str | None
    title: str | None
    status: str
    agent_name: str
    model_ref: str | None
    last_event_seq: int
    created_at: datetime
    updated_at: datetime


class PromptRequest(BaseModel):
    prompt: str
    wait: bool = True


class EventOut(BaseModel):
    seq: int
    type: str
    role: str | None
    payload: dict[str, Any]
    timestamp: datetime


def _to_out(record: object) -> ConversationOut:
    return ConversationOut(
        conversation_id=record.conversation_id,  # type: ignore[attr-defined]
        opencode_session_id=record.opencode_session_id,  # type: ignore[attr-defined]
        agent_run_id=record.agent_run_id,  # type: ignore[attr-defined]
        title=record.title,  # type: ignore[attr-defined]
        status=record.status.value,  # type: ignore[attr-defined]
        agent_name=record.agent_name,  # type: ignore[attr-defined]
        model_ref=record.model_ref,  # type: ignore[attr-defined]
        last_event_seq=record.last_event_seq,  # type: ignore[attr-defined]
        created_at=record.created_at,  # type: ignore[attr-defined]
        updated_at=record.updated_at,  # type: ignore[attr-defined]
    )


def _event_to_out(event: object) -> EventOut:
    return EventOut(
        seq=event.event_seq,  # type: ignore[attr-defined]
        type=event.event_type,  # type: ignore[attr-defined]
        role=event.role,  # type: ignore[attr-defined]
        payload=event.payload,  # type: ignore[attr-defined]
        timestamp=event.timestamp,  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(
    body: ConversationCreate,
    service: ConversationService = Depends(get_conversation_service),
) -> ConversationOut:
    try:
        record = await service.start_conversation(
            title=body.title,
            agent_run_id=body.agent_run_id,
            agent=body.agent,
            model=body.model,
        )
    except OpenCodeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"opencode unavailable: {exc}") from exc
    except OpenCodeRuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _to_out(record)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    limit: int = Query(default=50, ge=1, le=200),
    service: ConversationService = Depends(get_conversation_service),
) -> list[ConversationOut]:
    records = await service.list_conversations(limit=limit)
    return [_to_out(r) for r in records]


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
) -> ConversationOut:
    record = await service.get_conversation(conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _to_out(record)


@router.get("/{conversation_id}/events")
async def conversation_events(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
) -> StreamingResponse:
    """SSE 事件流:从 last_event_seq 续订,实时推送关键事件。"""

    async def _stream() -> AsyncIterator[bytes]:
        try:
            async for event in service.subscribe(conversation_id):
                data = {
                    "seq": event.event_seq,
                    "type": event.event_type,
                    "role": event.role,
                    "payload": event.payload,
                    "timestamp": event.timestamp.isoformat(),
                }
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
        except ConversationNotFoundError as exc:
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n".encode()

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/{conversation_id}/prompt")
async def send_prompt(
    conversation_id: str,
    body: PromptRequest,
    service: ConversationService = Depends(get_conversation_service),
) -> dict[str, Any]:
    try:
        return await service.send_prompt(conversation_id, body.prompt, wait=body.wait)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OpenCodeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"opencode unavailable: {exc}") from exc
    except OpenCodeRuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/{conversation_id}/interrupt", status_code=204)
async def interrupt_conversation(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
) -> None:
    try:
        await service.interrupt(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OpenCodeRuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/{conversation_id}/abort", status_code=204)
async def abort_conversation(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
) -> None:
    try:
        await service.abort(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OpenCodeRuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/{conversation_id}/history", response_model=list[EventOut])
async def conversation_history(
    conversation_id: str,
    after_seq: int = Query(default=0, ge=0),
    service: ConversationService = Depends(get_conversation_service),
) -> list[EventOut]:
    events = await service.replay_events(conversation_id, after_seq=after_seq)
    return [_event_to_out(e) for e in events]
