"""``finboard.report.*`` 工具 —— 报告聚合与导出(issue #141)。

只读(3):``report_run``(聚合 ResearchRun 报告:result 指标 + 全部 artifacts)/
``report_backtest``(聚合回测报告:metrics + equity_curve + fills)/
``report_export``(导出为 CSV / Markdown 文件,返回绝对路径)。

聚合与渲染逻辑在 ``finboard_mcp.reporting``(纯标准库 csv + 字符串模板,
零新依赖);本模块只做 DB 读取 + 工具封装。导出文件写入
``FINBOARD_EXPORT_DIR``(缺省系统临时目录下 finboard_exports),不持久化
到 DB,不触及交易安全红线。
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp import reporting
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable

__all__ = ["register"]


async def report_run(app: McpAppContext, run_id: str) -> ToolEnvelope:
    """聚合 ResearchRun 报告:run 元信息 + result 指标 + 全部 artifacts。"""

    async def _do() -> dict[str, Any]:
        from finboard_persistence import ResearchRunRepository

        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {run_id}")
            artifacts = await repo.list_artifacts(run_id)
            return cast(
                dict[str, Any],
                to_jsonable(reporting.aggregate_run_report(row, artifacts)),
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.report.run",
        arguments={"run_id": run_id},
        handler=_do,
    )


async def report_backtest(
    app: McpAppContext,
    run_id: int,
    *,
    equity_mode: str = "summary",
    max_points: int = 200,
) -> ToolEnvelope:
    """聚合单条回测历史报告:metrics + equity_curve + fills + summary。"""

    async def _do() -> dict[str, Any]:
        from finboard_persistence import BacktestRunRepository

        async with app.session_maker() as session:
            repo = BacktestRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"回测记录不存在: {run_id}")
            return cast(
                dict[str, Any],
                to_jsonable(
                    reporting.aggregate_backtest_report(
                        row, equity_mode=equity_mode, max_points=max_points
                    )
                ),
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.report.backtest",
        arguments={"run_id": run_id},
        handler=_do,
    )


async def report_export(
    app: McpAppContext,
    kind: str,
    id: str,
    fmt: str,
) -> ToolEnvelope:
    """把报告聚合后导出为 CSV / Markdown 文件,返回绝对路径。

    kind ∈ run | backtest;fmt ∈ csv | markdown;id 为 run_id(RR-)
    或回测记录 id。文件写入导出目录,不持久化到 DB。
    """

    async def _do() -> dict[str, Any]:
        from finboard_persistence import BacktestRunRepository, ResearchRunRepository

        if kind not in reporting.REPORT_KINDS:
            raise McpToolError("invalid_argument", f"未知报告类型: {kind}")
        if fmt not in reporting.EXPORT_FORMATS:
            raise McpToolError("invalid_argument", f"未知导出格式: {fmt}")
        async with app.session_maker() as session:
            if kind == "run":
                run_repo = ResearchRunRepository(session)
                run_row = await run_repo.get(id)
                if run_row is None:
                    raise McpToolError("not_found", f"研究运行不存在: {id}")
                artifacts = await run_repo.list_artifacts(id)
                report = reporting.aggregate_run_report(run_row, artifacts)
            else:
                try:
                    run_id = int(id)
                except ValueError:
                    raise McpToolError(
                        "invalid_argument", f"回测记录 id 必须是整数: {id}"
                    ) from None
                bt_repo = BacktestRunRepository(session)
                bt_row = await bt_repo.get(run_id)
                if bt_row is None:
                    raise McpToolError("not_found", f"回测记录不存在: {run_id}")
                report = reporting.aggregate_backtest_report(bt_row)
        # 文件写入放到线程池,避免阻塞事件循环。
        return cast(
            dict[str, Any],
            await asyncio.to_thread(reporting.export_report, kind, report, fmt),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.report.export",
        arguments={"kind": kind, "id": id, "format": fmt},
        handler=_do,
    )


def register(mcp: MCPServer) -> None:
    """把报告聚合与导出工具注册到 MCP server(3 只读)。"""

    @mcp.tool(
        name="finboard_report_run",
        description=(
            "聚合单个 ResearchRun 报告:run 元信息 + result 指标"
            "(ResearchRunReport 扁平字段)+ 全部 artifacts(含 report / equity /"
            "decisions 各阶段的 payload)。只读。"
        ),
    )
    async def _run(
        run_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await report_run(app_context(ctx), run_id)

    @mcp.tool(
        name="finboard_report_backtest",
        description=(
            "聚合单条回测历史报告:运行元信息 + metrics + equity_curve + fills +"
            "summary(标准化结构)。equity_mode(summary 默认:降采样到 max_points"
            "个关键点;full:完整曲线)、max_points(默认 200)。只读。"
        ),
    )
    async def _backtest(
        run_id: int,
        equity_mode: str = "summary",
        max_points: int = 200,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await report_backtest(
            app_context(ctx),
            run_id,
            equity_mode=equity_mode,
            max_points=max_points,
        )

    @mcp.tool(
        name="finboard_report_export",
        description=(
            "把报告聚合后导出为文件:kind=run|backtest,id 为 run_id(RR-)或回测"
            "记录 id,format=csv|markdown。写入导出目录(FINBOARD_EXPORT_DIR 或"
            "系统临时目录),返回文件绝对路径。只读(不写 DB)。"
        ),
    )
    async def _export(
        kind: str,
        id: str,
        format: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await report_export(app_context(ctx), kind, id, format)

