"""``finboard_audit_query`` —— 研究操作审计只读查询(issue #518)。

作用域 ``audit.read``(#517):只有显式授予该作用域的审计/运维客户端可用,
常规研究客户端预设(readonly / topic-writer / data-operator)不含此权限。

读取的是 ``research_audit_events`` 的**脱敏摘要与 digest**,不返回原始参数
(原始参数从未入库);不触碰实盘 ``audit_logs``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_persistence.research_audit_repo import (
    MAX_AUDIT_PAGE,
    ResearchAuditRepository,
)

_OUTCOMES = {"succeeded", "failed", "rejected", "denied"}
_SOURCES = {"web", "mcp", "worker", "system"}


def _parse_time(value: str | None, *, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise McpToolError(
            "invalid_argument", f"{name} 不是合法 ISO 时间: {value}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _row_out(row: Any) -> dict[str, Any]:
    return {
        "audit_id": row.audit_id,
        "occurred_at": row.occurred_at,
        "actor": row.actor,
        "actor_type": row.actor_type,
        "source": row.source,
        "client_id": row.client_id,
        "topic_id": row.topic_id,
        "entry_id": row.entry_id,
        "operation": row.operation,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "outcome": row.outcome,
        "result_code": row.result_code,
        "correlation": row.correlation,
        "params_summary": row.params_summary,
        "params_digest": row.params_digest,
        "error_summary": row.error_summary,
        "idempotency_key": row.idempotency_key,
        "trust": row.trust,
    }


async def audit_query(
    app: McpAppContext,
    *,
    audit_id: str | None = None,
    actor: str | None = None,
    client_id: str | None = None,
    source: str | None = None,
    topic_id: str | None = None,
    entity_id: str | None = None,
    operation: str | None = None,
    outcome: str | None = None,
    from_: str | None = None,
    to: str | None = None,
    limit: int = 100,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        if outcome is not None and outcome not in _OUTCOMES:
            raise McpToolError(
                "invalid_argument", f"outcome 必须是 {sorted(_OUTCOMES)} 之一"
            )
        if source is not None and source not in _SOURCES:
            raise McpToolError("invalid_argument", f"source 必须是 {sorted(_SOURCES)} 之一")
        if not 1 <= limit <= MAX_AUDIT_PAGE:
            raise McpToolError(
                "invalid_argument", f"limit 必须在 1..{MAX_AUDIT_PAGE} 之间"
            )
        since = _parse_time(from_, name="from")
        until = _parse_time(to, name="to")
        async with app.session_maker() as session:
            repo = ResearchAuditRepository(session)
            if audit_id is not None:
                row = await repo.get(audit_id)
                if row is None:
                    raise McpToolError("not_found", f"审计事件不存在: {audit_id}")
                return {"event": _row_out(row)}
            rows = await repo.list_events(
                actor=actor,
                client_id=client_id,
                source=source,
                topic_id=topic_id,
                entity_id=entity_id,
                operation=operation,
                outcome=outcome,
                since=since,
                until=until,
                limit=limit,
            )
            page = rows[:limit]
            return {
                "items": [_row_out(row) for row in page],
                "count": len(page),
                "has_more": len(rows) > limit,
                "limit": limit,
                "filters": {
                    "actor": actor,
                    "client_id": client_id,
                    "source": source,
                    "topic_id": topic_id,
                    "entity_id": entity_id,
                    "operation": operation,
                    "outcome": outcome,
                    "from": from_,
                    "to": to,
                },
                "coverage_note": (
                    "迁移/接入之前的写操作没有审计行;空结果表示「此前无审计记录」,"
                    "不代表没有发生操作。"
                ),
            }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.audit.query",
        arguments={
            "audit_id": audit_id,
            "actor": actor,
            "client_id": client_id,
            "source": source,
            "topic_id": topic_id,
            "entity_id": entity_id,
            "operation": operation,
            "outcome": outcome,
            "from": from_,
            "to": to,
            "limit": limit,
        },
        handler=_do,
    )


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        name="finboard_audit_query",
        description=(
            "研究操作审计只读查询(scope audit.read)。给 audit_id 读单条;否则按 "
            "actor/client_id/source/topic_id/entity_id/operation/outcome/from/to 过滤,"
            "limit≤200,只返回脱敏摘要与 sha256 digest。迁移/接入前无记录,空结果"
            "不代表没有操作;served 事实与 client_declared 并列展示。"
        ),
    )
    async def audit_query_tool(
        audit_id: str | None = None,
        actor: str | None = None,
        client_id: str | None = None,
        source: str | None = None,
        topic_id: str | None = None,
        entity_id: str | None = None,
        operation: str | None = None,
        outcome: str | None = None,
        from_: str | None = None,
        to: str | None = None,
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await audit_query(
            app_context(ctx),
            audit_id=audit_id,
            actor=actor,
            client_id=client_id,
            source=source,
            topic_id=topic_id,
            entity_id=entity_id,
            operation=operation,
            outcome=outcome,
            from_=from_,
            to=to,
            limit=limit,
        )


__all__ = ["audit_query", "register"]
