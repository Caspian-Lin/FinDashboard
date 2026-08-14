"""MCP 工具调用审计查询 API(issue #157)。

只读查询 ``mcp_audit_events``(``FINBOARD_MCP_AUDIT_PERSIST=true`` 时由
finboard-mcp AuditRecorder 追加)。供运维 / 用户核查 Agent 行为;
不触及实盘 ``audit_logs``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_persistence import McpAuditRepository

router = APIRouter(prefix="/api/mcp", tags=["mcp-audit"])


class McpAuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    operation_id: str
    tool_name: str
    status: str
    latency_ms: int
    error_kind: str | None
    caller: str | None
    arguments_summary: dict[str, Any]
    recorded_at: datetime


@router.get("/audit", response_model=list[McpAuditEventOut])
async def list_mcp_audit(
    tool_name: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[McpAuditEventOut]:
    """最近 MCP 工具调用审计事件(时间升序;可选按工具名过滤)。"""
    rows = await McpAuditRepository(session).list_recent(
        tool_name=tool_name, limit=limit
    )
    return [McpAuditEventOut.model_validate(row) for row in rows]


__all__ = ["router"]
