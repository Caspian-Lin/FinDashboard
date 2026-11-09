"""``finboard_topic_action`` -- whitelisted factor / sandbox / OOS topic actions (#516).

Thin adapter over :mod:`finboard_app.research_topic_artifacts`: the same service
backs ``POST /api/research/topics/{id}/actions``. Scope enforcement is two-layer:
``run_tool`` requires ``research.write`` for the tool, and the service re-checks
the per-action capability (``research.write`` for metadata, ``research.run`` for
anything that starts execution) against the resolved ``MC-`` identity.

There is no code parameter anywhere in this tool: ``code_run.execute`` can only
name an already-registered artifact (``artifact_id`` or ``(kind, name)``); the
commit is pinned server-side from ``research_code_artifacts``.
"""

from __future__ import annotations

from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_app.research_operation import ResearchOperationError
from finboard_app.research_topic_artifacts import run_topic_action
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_mcp.tools.runs import (
    _mcp_error,
    _mcp_operation_context,
    _require_write_enabled,
)


async def topic_action(
    app: McpAppContext,
    *,
    topic_id: str,
    action: str,
    goal_version: str,
    payload: dict[str, Any] | None = None,
    operation_id: str | None = None,
) -> ToolEnvelope:
    """Run one whitelisted action inside the service's single transaction."""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            try:
                result = await run_topic_action(
                    session,
                    topic_id=topic_id,
                    action=action,
                    goal_version=goal_version,
                    payload=payload or {},
                    ctx=_mcp_operation_context(app, operation_id),
                    sandbox_enabled=app.settings.research_sandbox_enabled,
                )
            except ResearchOperationError as exc:
                await session.rollback()
                raise _mcp_error(exc) from exc
            return cast(dict[str, Any], to_jsonable(result))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.topic.action",
        arguments={
            "topic_id": topic_id,
            "action": action,
            "goal_version": goal_version,
            "operation_id": operation_id,
            "artifact_id": (payload or {}).get("artifact_id"),
            "experiment_id": (payload or {}).get("experiment_id"),
            "run_id": (payload or {}).get("run_id"),
        },
        handler=_do,
        idempotency_key=operation_id,
    )


def register(mcp: MCPServer) -> None:
    """Register the single ``finboard_topic_action`` write tool (#516)."""

    @mcp.tool(
        name="finboard_topic_action",
        description=(
            "在 active 课题内执行一个白名单动作(写;与 REST "
            "POST /api/research/topics/{id}/actions 同一实现)。action 只能是:"
            "run.submit / run.replay(委托 #515,需 research.run)、"
            "factor_experiment.create(登记因子实验,不执行,需 research.write)、"
            "code_run.execute(仅执行已登记 artifact,需 research.run)、"
            "validation_experiment.create / add_trial(需 research.write)、"
            "validation_experiment.unseal(一次性揭盲入队,需 research.run)。"
            "每个动作要求 topic 为 active 且 goal_version 等于当前目标版本,并写入"
            "不可变课题链接 + 回执(research_topic_action_receipts)+ 研究审计(同一事务)。"
            "operation_id 是响应丢失锚:同键重放返回原产物/原 job,不会重复执行"
            "(add_trial 不再新增 trial、unseal 不再新建任务、也不误报 already_unsealed);"
            "同键用于不同动作返回 conflict(operation_id_bound)。返回 artifact_created / "
            "link_created / replayed 区分新建与回执重放。code_run.execute 的 payload 只能"
            "含 artifact_id 或 (kind,name)+dataset_release_ids+decision_at+symbols+"
            "params;任何动作出现 Python/模块路径/表达式键(如 code/module/expression)"
            "一律 invalid_argument(code_channel_forbidden);代码提交只走 "
            "finboard_research_code_submit。已揭盲实验的 unseal 返回 conflict"
            "(already_unsealed)。只读链接用 finboard_topic_read(view=context|artifacts)。"
        ),
    )
    async def _action(
        topic_id: str,
        action: str,
        goal_version: str,
        payload: dict[str, Any] | None = None,
        operation_id: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await topic_action(
            app_context(ctx),
            topic_id=topic_id,
            action=action,
            goal_version=goal_version,
            payload=payload,
            operation_id=operation_id,
        )


__all__ = ["register", "topic_action"]
