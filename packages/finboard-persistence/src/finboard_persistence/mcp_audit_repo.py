"""MCP 工具调用审计事件仓储(issue #157,``mcp_audit_events`` 只追加)。

``FINBOARD_MCP_AUDIT_PERSIST=true`` 时由 ``finboard_mcp.audit.AuditRecorder``
写入;查询供 REST ``/api/mcp/audit`` 使用。入参摘要必须已脱敏
(``summarize_arguments``),本仓储不做二次脱敏(保持与 structlog 输出一致)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_persistence.models import McpAuditEventModel


class McpAuditRepository:
    """``mcp_audit_events`` 只追加仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        operation_id: str,
        tool_name: str,
        status: str,
        latency_ms: int,
        error_kind: str | None,
        caller: str | None,
        arguments_summary: dict[str, Any],
        recorded_at: datetime | None = None,
    ) -> McpAuditEventModel:
        model = McpAuditEventModel(
            operation_id=operation_id,
            tool_name=tool_name,
            status=status,
            latency_ms=latency_ms,
            error_kind=error_kind,
            caller=caller,
            arguments_summary=dict(arguments_summary),
            recorded_at=recorded_at or datetime.now(UTC),
        )
        self._session.add(model)
        await self._session.flush()
        return model

    async def list_recent(
        self, *, tool_name: str | None = None, limit: int = 100
    ) -> list[McpAuditEventModel]:
        stmt = select(McpAuditEventModel).order_by(
            McpAuditEventModel.recorded_at.desc(), McpAuditEventModel.id.desc()
        )
        if tool_name:
            stmt = stmt.where(McpAuditEventModel.tool_name == tool_name)
        stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        entries = list(result.scalars())
        entries.reverse()
        return entries

    async def count(self) -> int:
        result = await self._session.execute(select(func.count(McpAuditEventModel.id)))
        return int(result.scalar_one())


__all__ = ["McpAuditRepository"]
