"""Bounded research context and frozen explanation tools (#499/#500/#514)."""

import json
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_app.research_explanation import decision_evidence, explain
from finboard_app.research_operation import ResearchOperationContext
from finboard_app.research_topic_lifecycle import TopicWriteConflictError
from finboard_app.research_workspace import (
    memory_page,
    source_fact,
    workspace_read,
    workspace_write,
)
from finboard_backtest.factor_research.sanitizer import sanitize_prompt
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_prompt(value)
    if isinstance(value, list):
        return [sanitize(x) for x in value]
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    return value


async def call(app: McpAppContext, operation: str, arguments: dict[str, Any]) -> ToolEnvelope:
    args = sanitize(arguments)

    async def perform() -> dict[str, Any]:
        if operation == "write" and not app.write_tools_enabled:
            raise McpToolError("permission_denied", "MCP 只读模式不允许写课题")
        async with app.session_maker() as session:
            if operation == "write":
                identity = getattr(app, "identity", None)
                # issue #518:服务端身份上下文(actor/source/client_id 不可由载荷覆盖)。
                context = ResearchOperationContext.for_mcp(
                    client_id=identity.client_id if identity is not None else None
                )
                try:
                    return await workspace_write(
                        session, actor="agent:mcp", context=context, **args
                    )
                except TopicWriteConflictError as exc:
                    # Structured 409 equivalent: state/revision conflicts are not
                    # invalid arguments. ToolError has no details field, so the
                    # machine-readable detail rides along as JSON in the message.
                    raise McpToolError(
                        "conflict",
                        f"{exc} | details={json.dumps(exc.detail, ensure_ascii=False, sort_keys=True)}",
                    ) from exc
            if operation == "read":
                return await workspace_read(
                    session,
                    docs_root=Path(app.settings.research_docs_dir),
                    **args,
                )
            if operation == "memories":
                return await memory_page(session, **args)
            if operation == "explain":
                return await explain(session, **args)
            if operation == "source":
                return await source_fact(session, args, Path(app.settings.research_docs_dir))
            return await decision_evidence(session, **args)

    return await run_tool(
        audit=app.audit,
        tool_name=f"finboard.workspace.{operation}",
        arguments=args,
        handler=perform,
        sensitive=("payload",),
    )


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        name="finboard_source_check",
        description="核验引用是否存在、版本/checksum,输出自动事实;研究文档为canonical,不自动接受agent结论。",
    )
    async def source(
        kind: str,
        ref_id: str,
        version: str | None = None,
        checksum: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await call(
            app_context(ctx),
            "source",
            {"kind": kind, "ref_id": ref_id, "version": version, "checksum": checksum},
        )

    @mcp.tool(
        name="finboard_topic_read",
        description="分页课题/轮次,默认20最多50;无topic_id列课题(可按status筛选、sort=updated|created),有ID读取详情,entries=true读轮次和引用目录,has_more按offset续页。view=context读有界续接上下文(≤20轮、≤10引用核验、≤64KiB,截断时excerpted=true;含experiment_links与entry_types,轮次带entry_type);view=branches按分支聚合;view=refs逐条核验最近轮次引用(每次≤20条,offset续页);view=artifacts读因子实验/沙箱运行/OOS实验链接(≤50),OOS先看oos_outcome再看status,并列trial_budget/final_test_unsealed/evidence_gaps;entry_id按精确RE-ID读单条全文与checksum,跨课题404。课题是持续锚点,工作摘要不是canonical结论。",
    )
    async def read(
        topic_id: str | None = None,
        entries: bool = False,
        limit: int = 20,
        offset: int = 0,
        view: str | None = None,
        entry_id: str | None = None,
        status: str | None = None,
        sort: str = "updated",
        rounds: int = 10,
        refs: int = 10,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await call(
            app_context(ctx),
            "read",
            {
                "topic_id": topic_id,
                "entries": entries,
                "limit": limit,
                "offset": offset,
                "view": view,
                "entry_id": entry_id,
                "status": status,
                "sort": sort,
                "rounds": rounds,
                "refs": refs,
            },
        )

    @mcp.tool(
        name="finboard_topic_write",
        description="结构化课题create/draft_create/update/activate/transition/append。draft_create只需title/question生成draft;activate需topic_id+expected_revision+payload.goal(可带summary/open_questions/next_step)完成draft→active;transition的payload.action=pause/resume/close/reopen/discard,close/reopen/discard须reason,全部需expected_revision。更新需expected_revision,追加需幂等键;create/append可带idempotency_key,同键同内容返回原记录(created=false)。非法状态转移409等价conflict(附allowed/current),版本冲突附current_revision/changed_fields。entry_type=round记录尝试,evidence组织已有运行/记忆/实验的精确source_refs,plan只记录设计(不执行,outcome须completed),review复核结论须带document_ref(仓库内.md相对路径+checksum)且证据等级为reviewed_conclusion。只保存记录,不运行研究、不接受正式结论;真正执行走finboard_topic_action。例子见tools.md与topic-organization.md。",
    )
    async def write(
        operation: str,
        payload: dict[str, Any],
        topic_id: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await call(
            app_context(ctx),
            "write",
            {
                "operation": operation,
                "payload": payload,
                "topic_id": topic_id,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
        )

    @mcp.tool(
        name="finboard_memory_page",
        description="分页研究记忆摘录与纠正链,含旧状态和不可验证refs提示;默认20最多50,每条正文最多1200字。",
    )
    async def memories(limit: int = 20, offset: int = 0, ctx: Context = None) -> ToolEnvelope:  # type: ignore[assignment]
        return await call(app_context(ctx), "memories", {"limit": limit, "offset": offset})

    @mcp.tool(
        name="finboard_strategy_explain",
        description="冻结规则说明:指定run_id,或strategy_id+version;因子定义只在冻结commit匹配时解释,缺证据具名提示。当前目录不冒充历史参数。",
    )
    async def explanation(
        run_id: str | None = None,
        strategy_id: str | None = None,
        version: int | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await call(
            app_context(ctx),
            "explain",
            {"run_id": run_id, "strategy_id": strategy_id, "version": version},
        )

    @mcp.tool(
        name="finboard_decision_explain",
        description="有界决策目录;symbol+decision_id/business_date下钻因子→信号→约束→目标→成交。SQL先投影分页,默认100最多200条,每条16KiB。空阶段不猜原因。",
    )
    async def decision(
        run_id: str,
        symbol: str | None = None,
        decision_id: str | None = None,
        business_date: str | None = None,
        limit: int = 100,
        offset: int = 0,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await call(
            app_context(ctx),
            "decision",
            {
                "run_id": run_id,
                "symbol": symbol,
                "decision_id": decision_id,
                "business_date": business_date,
                "limit": limit,
                "offset": offset,
            },
        )
