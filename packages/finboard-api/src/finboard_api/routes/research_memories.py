"""研究长期记忆 / 研究笔记路由(issue #110)。

提供研究记忆 CRUD 与生命周期操作(记住 / 忘记 / 纠正 / 确认 / 归档)。
记忆通过 ``source_refs`` 关联研究产物,只引用不修改产物。

红线:不写入实盘 orders/fills/positions/audit_logs;AI / OpenCode Agent 通过
MCP 工具(``finboard.memory.*``)访问同一 ``research_memories`` 表,本路由
服务于人工 / 前端操作。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_persistence import ResearchMemory, ResearchMemoryRepository, SourceRef

router = APIRouter(prefix="/api/research/memories", tags=["research-memories"])

_USER_ACTOR = "user:api"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SourceRefSchema(BaseModel):
    kind: str
    ref_id: str
    label: str | None = None


class MemoryCreateSchema(BaseModel):
    memory_type: str
    content: str
    source_refs: list[SourceRefSchema] = []
    tags: list[str] = []
    conversation_id: str | None = None


class MemoryCorrectSchema(BaseModel):
    content: str
    source_refs: list[SourceRefSchema] | None = None
    tags: list[str] | None = None
    conversation_id: str | None = None


class MemoryConfirmSchema(BaseModel):
    actor: str | None = None


class MemoryOutSchema(BaseModel):
    memory_id: str
    memory_type: str
    content: str
    source_refs: list[SourceRefSchema]
    status: str
    tags: list[str]
    created_by: str
    conversation_id: str | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    supersedes_id: str | None = None
    created_at: datetime
    updated_at: datetime


def _to_out(m: ResearchMemory) -> MemoryOutSchema:
    return MemoryOutSchema(
        memory_id=m.memory_id,
        memory_type=m.memory_type,
        content=m.content,
        source_refs=[
            SourceRefSchema(kind=s.kind, ref_id=s.ref_id, label=s.label)
            for s in m.source_refs
        ],
        status=m.status,
        tags=list(m.tags),
        created_by=m.created_by,
        conversation_id=m.conversation_id,
        confirmed_by=m.confirmed_by,
        confirmed_at=m.confirmed_at,
        supersedes_id=m.supersedes_id,
        created_at=m.created_at or datetime.utcnow(),
        updated_at=m.updated_at or datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=MemoryOutSchema, status_code=201)
async def create_memory(
    body: MemoryCreateSchema, session: AsyncSession = Depends(get_db_session)
) -> MemoryOutSchema:
    """记住一条研究记忆。"""
    repo = ResearchMemoryRepository(session)
    try:
        record = await repo.remember(
            memory_type=body.memory_type,
            content=body.content,
            source_refs=[
                SourceRef(kind=r.kind, ref_id=r.ref_id, label=r.label)
                for r in body.source_refs
            ],
            tags=body.tags,
            created_by=_USER_ACTOR,
            conversation_id=body.conversation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return _to_out(record)


@router.get("", response_model=list[MemoryOutSchema])
async def list_memories(
    status: str | None = None,
    memory_type: str | None = None,
    source_kind: str | None = None,
    source_ref: str | None = None,
    tag: str | None = None,
    conversation_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[MemoryOutSchema]:
    """列表查询(可选过滤)。"""
    repo = ResearchMemoryRepository(session)
    try:
        records = await repo.list_memories(
            status=status,
            memory_type=memory_type,
            source_kind=source_kind,
            source_ref=source_ref,
            tag=tag,
            conversation_id=conversation_id,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_to_out(r) for r in records]


@router.get("/{memory_id}", response_model=MemoryOutSchema)
async def get_memory(
    memory_id: str, session: AsyncSession = Depends(get_db_session)
) -> MemoryOutSchema:
    repo = ResearchMemoryRepository(session)
    record = await repo.get(memory_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"研究记忆不存在: {memory_id}")
    return _to_out(record)


@router.post("/{memory_id}/forget", response_model=MemoryOutSchema)
async def forget_memory(
    memory_id: str, session: AsyncSession = Depends(get_db_session)
) -> MemoryOutSchema:
    repo = ResearchMemoryRepository(session)
    try:
        record = await repo.forget(memory_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return _to_out(record)


@router.post("/{memory_id}/correct", response_model=MemoryOutSchema)
async def correct_memory(
    memory_id: str,
    body: MemoryCorrectSchema,
    session: AsyncSession = Depends(get_db_session),
) -> MemoryOutSchema:
    repo = ResearchMemoryRepository(session)
    try:
        refs = (
            None
            if body.source_refs is None
            else [
                SourceRef(kind=r.kind, ref_id=r.ref_id, label=r.label)
                for r in body.source_refs
            ]
        )
        record = await repo.correct(
            memory_id=memory_id,
            content=body.content,
            source_refs=refs,
            tags=body.tags,
            created_by=_USER_ACTOR,
            conversation_id=body.conversation_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return _to_out(record)


@router.post("/{memory_id}/confirm", response_model=MemoryOutSchema)
async def confirm_memory(
    memory_id: str,
    body: MemoryConfirmSchema | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> MemoryOutSchema:
    repo = ResearchMemoryRepository(session)
    try:
        record = await repo.confirm(
            memory_id, actor=(body.actor if body and body.actor else _USER_ACTOR)
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return _to_out(record)


@router.post("/{memory_id}/archive", response_model=MemoryOutSchema)
async def archive_memory(
    memory_id: str, session: AsyncSession = Depends(get_db_session)
) -> MemoryOutSchema:
    repo = ResearchMemoryRepository(session)
    try:
        record = await repo.archive(memory_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return _to_out(record)
