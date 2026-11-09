"""``finboard.topic_run.*`` tools -- topic-initiated ResearchRuns (#515).

The tools are thin adapters over :mod:`finboard_app.research_topic_actions`:
the same application service backs the REST endpoints, so the topic lock,
single commit (run + job + link + audit), operation-id idempotency, replay
retry and immutable late link behave identically on both channels.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_app.research_operation import ResearchOperationError
from finboard_app.research_topic_actions import (
    cancel_topic_run,
    get_topic_run,
    list_run_topics,
    list_topic_runs,
    retry_topic_run,
    start_topic_run,
)
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_mcp.tools.runs import (
    _mcp_error,
    _mcp_operation_context,
    _require_write_enabled,
)


async def topic_run_start(
    app: McpAppContext,
    *,
    topic_id: str,
    goal_version: str,
    payload: dict[str, Any],
    operation_id: str | None = None,
) -> ToolEnvelope:
    """Start a run from an active topic inside the service's single transaction."""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            try:
                result = await start_topic_run(
                    session,
                    topic_id=topic_id,
                    goal_version=goal_version,
                    payload=payload,
                    ctx=_mcp_operation_context(app, operation_id),
                    sandbox_enabled=app.settings.research_sandbox_enabled,
                )
            except ResearchOperationError as exc:
                await session.rollback()
                raise _mcp_error(exc) from exc
            return to_jsonable(result)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.topic_run.start",
        arguments={
            "topic_id": topic_id,
            "goal_version": goal_version,
            "operation_id": operation_id,
            "strategy_id": payload.get("strategy_id"),
            "strategy_version": payload.get("strategy_version"),
        },
        handler=_do,
    )


async def topic_run_list(
    app: McpAppContext,
    *,
    topic_id: str,
    limit: int = 20,
    cursor: str | None = None,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            return to_jsonable(
                await list_topic_runs(session, topic_id, limit=limit, cursor=cursor)
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.topic_run.list",
        arguments={"topic_id": topic_id, "limit": limit, "cursor": cursor},
        handler=_do,
    )


async def topic_run_get(
    app: McpAppContext, *, topic_id: str, run_id: str
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            return to_jsonable(await get_topic_run(session, topic_id, run_id))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.topic_run.get",
        arguments={"topic_id": topic_id, "run_id": run_id},
        handler=_do,
    )


async def topic_run_retry(
    app: McpAppContext,
    *,
    topic_id: str,
    run_id: str,
    payload: dict[str, Any],
    operation_id: str,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            try:
                result = await retry_topic_run(
                    session,
                    topic_id=topic_id,
                    run_id=run_id,
                    payload=payload,
                    ctx=_mcp_operation_context(app, operation_id),
                    sandbox_enabled=app.settings.research_sandbox_enabled,
                )
            except ResearchOperationError as exc:
                await session.rollback()
                raise _mcp_error(exc) from exc
            return to_jsonable(result)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.topic_run.retry",
        arguments={
            "topic_id": topic_id,
            "run_id": run_id,
            "operation_id": operation_id,
            "idempotency_key": payload.get("idempotency_key"),
        },
        handler=_do,
    )


async def topic_run_cancel(
    app: McpAppContext, *, topic_id: str, run_id: str
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            try:
                result = await cancel_topic_run(
                    session,
                    topic_id=topic_id,
                    run_id=run_id,
                    ctx=_mcp_operation_context(app),
                )
            except ResearchOperationError as exc:
                await session.rollback()
                raise _mcp_error(exc) from exc
            return to_jsonable(result)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.topic_run.cancel",
        arguments={"topic_id": topic_id, "run_id": run_id},
        handler=_do,
    )


async def run_topics(app: McpAppContext, *, run_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            return to_jsonable(await list_run_topics(session, run_id))

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.topics",
        arguments={"run_id": run_id},
        handler=_do,
    )


def register(mcp: MCPServer) -> None:
    """Register topic-run tools (3 read + 3 write)."""

    @mcp.tool(
        name="finboard_topic_run_start",
        description=(
            "从 active 课题发起 ResearchRun(写,不执行回测)。服务端在同一事务"
            "写入 run + background job + 课题链接 + 研究审计;topic/目标版本不匹配"
            "或被其他 operation_id 占用时拒绝。参数:topic_id(必填)、"
            "goal_version(必填,须等于课题当前目标版本)、operation_id(可选,"
            "响应丢失后同键重放返回原 RR-/BJ-,不新建)、payload(与 "
            "finboard_run_queue 同构的 run 入队 JSON,另需 goal_version 作为"
            "独立参数)。返回精简回执(topic_id/run_id/job_id/status/"
            "manifest_checksum/created/link);全量详情走 finboard_topic_run_get "
            "或 finboard_run_get。"
        ),
    )
    async def _start(
        topic_id: str,
        goal_version: str,
        payload: dict[str, Any],
        operation_id: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await topic_run_start(
            app_context(ctx),
            topic_id=topic_id,
            goal_version=goal_version,
            payload=payload,
            operation_id=operation_id,
        )

    @mcp.tool(
        name="finboard_topic_run_list",
        description=(
            "列出课题关联的研究运行(只读)。链接本身不保存状态,读取时 join "
            "research_runs 与 background_jobs 派生 state(queued/running/"
            "succeeded/failed/cancelled/interrupted)、进度、错误与 result_checksum。"
            "参数:topic_id、limit(1-50,默认 20)、cursor(上一页 next_cursor)。"
        ),
    )
    async def _list(
        topic_id: str,
        limit: int = 20,
        cursor: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await topic_run_list(
            app_context(ctx), topic_id=topic_id, limit=limit, cursor=cursor
        )

    @mcp.tool(
        name="finboard_topic_run_get",
        description=(
            "读取课题与单个研究运行的关联详情(只读):链接元数据 + join 派生的"
            "run/job 状态、进度、error 与 result_checksum,并附 "
            "/research/runs?run=RR- 深链。跨课题的 run_id 返回 not_found。"
        ),
    )
    async def _get(
        topic_id: str,
        run_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await topic_run_get(
            app_context(ctx), topic_id=topic_id, run_id=run_id
        )

    @mcp.tool(
        name="finboard_topic_run_retry",
        description=(
            "重试课题关联的失败/中断运行(写,不执行)。走既有 replay:生成新 RR-"
            "(replay_of_run_id 指向旧 run)与新的链接,旧链接与历史保留;"
            "goal_version 继承原链接且仍须匹配课题当前目标。必须提供新的 "
            "operation_id(旧键已绑定原链接)与 payload{idempotency_key,"
            "requested_by}。"
        ),
    )
    async def _retry(
        topic_id: str,
        run_id: str,
        payload: dict[str, Any],
        operation_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await topic_run_retry(
            app_context(ctx),
            topic_id=topic_id,
            run_id=run_id,
            payload=payload,
            operation_id=operation_id,
        )

    @mcp.tool(
        name="finboard_topic_run_cancel",
        description=(
            "取消课题关联的研究运行(写;代理既有协作式取消,非实盘撤单)。"
            "queued/running/interrupted/failed 可取消;completed/cancelled 已终态"
            "返回 conflict。"
        ),
    )
    async def _cancel(
        topic_id: str,
        run_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await topic_run_cancel(
            app_context(ctx), topic_id=topic_id, run_id=run_id
        )

    @mcp.tool(
        name="finboard_run_topics",
        description=(
            "反查引用某个 ResearchRun 的全部课题(只读):一个 run 可被多个课题"
            "引用,返回 topic_id/title/topic_status/goal_version/operation_id 等"
            "链接元数据,不修改 run 或课题。"
        ),
    )
    async def _run_topics(
        run_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await run_topics(app_context(ctx), run_id=run_id)


__all__ = [
    "register",
    "run_topics",
    "topic_run_cancel",
    "topic_run_get",
    "topic_run_list",
    "topic_run_retry",
    "topic_run_start",
]
