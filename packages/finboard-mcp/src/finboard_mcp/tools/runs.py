"""``finboard.run.*`` 工具 —— ResearchRun 只读查询(复用 ``ResearchRunRepository``)。

只读工具自动允许。研究写工具(创建 Run / 启动回测)在后续 issue(#124-#128)
中扩展,agent 可自主执行(#122 放开审批门)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence.research_run_repo import ResearchRunRepository

if TYPE_CHECKING:
    from finboard_persistence.models import ResearchRunArtifactModel, ResearchRunModel


def _run_summary(row: ResearchRunModel) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "strategy_id": row.strategy_id,
        "strategy_kind": row.strategy_kind,
        "status": row.status,
        "schema_version": row.schema_version,
        "requested_by": row.requested_by,
        "created_at": to_jsonable(row.created_at),
        "started_at": to_jsonable(row.started_at),
        "completed_at": to_jsonable(row.completed_at),
        "error_code": row.error_code,
    }


def _run_detail(row: ResearchRunModel) -> dict[str, Any]:
    detail = _run_summary(row)
    detail.update(
        {
            "idempotency_key": row.idempotency_key,
            "replay_of_run_id": row.replay_of_run_id,
            "manifest_checksum": row.manifest_checksum,
            "result_checksum": row.result_checksum,
            "manifest": row.manifest,
            "result": row.result,
            "error_summary": row.error_summary,
        }
    )
    return cast(dict[str, Any], to_jsonable(detail))


def _artifact_summary(row: ResearchRunArtifactModel) -> dict[str, Any]:
    return cast(dict[str, Any], to_jsonable(
        {
            "artifact_id": row.artifact_id,
            "sequence": row.sequence,
            "stage": row.stage,
            "trace_id": row.trace_id,
            "decision_id": row.decision_id,
            "checksum": row.checksum,
            "payload": row.payload,
        }
    ))


async def list_runs(
    app: McpAppContext,
    *,
    statuses: list[str] | None = None,
    strategy_kind: str | None = None,
    limit: int = 50,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            rows = await repo.list_recent(
                statuses=tuple(statuses) if statuses else None,
                strategy_kind=strategy_kind,
                limit=limit,
            )
            return [_run_summary(row) for row in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.list",
        arguments={
            "statuses": statuses,
            "strategy_kind": strategy_kind,
            "limit": limit,
        },
        handler=_do,
    )


async def get_run(app: McpAppContext, run_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {run_id}")
            return _run_detail(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.get",
        arguments={"run_id": run_id},
        handler=_do,
    )


async def list_artifacts(app: McpAppContext, run_id: str) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {run_id}")
            artifacts = await repo.list_artifacts(run_id)
            return [_artifact_summary(item) for item in artifacts]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.artifacts",
        arguments={"run_id": run_id},
        handler=_do,
    )


def register(mcp: MCPServer) -> None:
    """把 ResearchRun 只读工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_run_list",
        description="列出 ResearchRun(可选按状态/策略类型过滤,默认最近 50 条)。",
    )
    async def _list(
        statuses: list[str] | None = None,
        strategy_kind: str | None = None,
        limit: int = 50,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await list_runs(
            app_context(ctx),
            statuses=statuses,
            strategy_kind=strategy_kind,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_run_get",
        description="查询单个 ResearchRun 详情(含 manifest / result)。",
    )
    async def _get(run_id: str, ctx: Context = None) -> ToolEnvelope:  # type: ignore[assignment]
        return await get_run(app_context(ctx), run_id)

    @mcp.tool(
        name="finboard_run_artifacts",
        description="列出某 ResearchRun 的逐阶段 artifact(含 trace 与 payload)。",
    )
    async def _artifacts(run_id: str, ctx: Context = None) -> ToolEnvelope:  # type: ignore[assignment]
        return await list_artifacts(app_context(ctx), run_id)


__all__ = ["get_run", "list_artifacts", "list_runs", "register"]
