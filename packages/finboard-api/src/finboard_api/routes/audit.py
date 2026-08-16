"""审计日志端点。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.schemas import AuditLogOut, PageResponse
from finboard_persistence import AuditLogRepository

router = APIRouter(prefix="/api/audit-logs", tags=["audit"])


@router.get("", response_model=PageResponse[AuditLogOut])
async def list_audit_logs(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> PageResponse[AuditLogOut]:
    repo = AuditLogRepository(session)
    rows = await repo.list_recent(limit=limit, offset=offset)
    items = [
        AuditLogOut(
            id=r.id,
            actor=r.actor,
            action=r.action,
            target=r.target,
            payload=r.payload,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return PageResponse(
        items=items,
        total=len(items),
        limit=limit,
        offset=offset,
    )
