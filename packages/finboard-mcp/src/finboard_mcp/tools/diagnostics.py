"""研究诊断的受控只读接口 (#501)。"""

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_app.research_diagnostics import compare_runs, decision_projection, interval_report
from finboard_app.research_stress import stress_matrix
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools.workspace import sanitize


async def call(app: McpAppContext, operation: str, args: dict[str, Any]) -> ToolEnvelope:
    args = sanitize(args)

    async def perform() -> dict[str, Any]:
        if operation == "stress" and args.get("operation") == "queue" and not app.write_tools_enabled:
            raise McpToolError("permission_denied", "只读模式禁止压力运行入队")
        async with app.session_maker() as session:
            try:
                if operation == "stress":
                    return await stress_matrix(session, actor="agent:mcp", **args)
                if operation == "interval":
                    return await interval_report(session, **args)
                if operation == "compare":
                    return await compare_runs(session, **args)
                return await decision_projection(session, **args)
            except ValueError as exc:
                raise McpToolError(
                    "payload_too_large" if "payload_too_large" in str(exc) else "invalid_argument",
                    str(exc),
                ) from exc
            except LookupError as exc:
                raise McpToolError("not_found", str(exc)) from exc

    return await run_tool(
        audit=app.audit,
        tool_name=f"finboard.diagnostics.{operation}",
        arguments=args,
        handler=perform,
    )


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="finboard_research_stress", description="冻结基线生成有限费用/滑点/资金压力矩阵;operation=plan/get只读,queue显式入队;最多24档;2Bar延迟unsupported。")
    async def stress(baseline_run_id: str, plan_key: str, operation: str = "plan",
                     cost_multipliers: list[float] | None = None, slippage_bps: list[float] | None = None,
                     capitals: list[str] | None = None, execution_delay_bars: list[int] | None = None,
                     ctx: Context = None,  # type: ignore[assignment]
                     ) -> ToolEnvelope:
        return await call(app_context(ctx), "stress", {"baseline_run_id": baseline_run_id, "plan_key": plan_key,
            "operation": operation, "cost_multipliers": cost_multipliers, "slippage_bps": slippage_bps,
            "capitals": capitals, "execution_delay_bars": execution_delay_bars})
    @mcp.tool(
        name="finboard_run_diagnostics",
        description="显式日期闭区间/逐年确定性指标与Markdown同源;rf=0/ddof=1;标明观测/决策/预热证据。",
    )
    async def interval(
        run_id: str, start: str, end: str, yearly: bool = True,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await call(
            app_context(ctx),
            "interval",
            {"run_id": run_id, "start": start, "end": end, "yearly": yearly},
        )

    @mcp.tool(
        name="finboard_run_compare",
        description="2-6个冻结run可比性检查;允许差异须显式声明;混数据源永不称单变量消融。",
    )
    async def comparison(
        run_ids: list[str], allowed_differences: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await call(
            app_context(ctx),
            "compare",
            {"run_ids": run_ids, "allowed_differences": allowed_differences},
        )

    @mcp.tool(
        name="finboard_decision_projection",
        description="SQL先投影/分页;阶段/字段白名单;最多200条/256KiB,单决策同样护栏;附total/next_offset/trace/checksum。",
    )
    async def projection(
        run_id: str,
        stage: str = "features",
        decision_id: str | None = None,
        symbol: str | None = None,
        fields: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await call(
            app_context(ctx),
            "projection",
            {
                "run_id": run_id,
                "stage": stage,
                "decision_id": decision_id,
                "symbol": symbol,
                "fields": fields,
                "limit": limit,
                "offset": offset,
            },
        )
