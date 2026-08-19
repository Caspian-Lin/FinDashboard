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


async def report_run(
    app: McpAppContext,
    run_id: str,
    *,
    view: str = "summary",
) -> ToolEnvelope:
    """聚合 ResearchRun 报告:view=summary 默认(聚合计数)/ detail(全量)。"""

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
                to_jsonable(
                    reporting.aggregate_run_report(row, artifacts, view=view)
                ),
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.report.run",
        arguments={"run_id": run_id, "view": view},
        handler=_do,
    )


async def report_backtest(
    app: McpAppContext,
    run_id: int,
    *,
    equity_mode: str = "summary",
    max_points: int = 200,
    fills_limit: int | None = reporting.DEFAULT_FILLS_LIMIT,
    fills_offset: int = 0,
) -> ToolEnvelope:
    """聚合单条回测历史报告:metrics + equity_curve + fills(有界分页)+ summary。"""

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
                        row,
                        equity_mode=equity_mode,
                        max_points=max_points,
                        fills_limit=fills_limit,
                        fills_offset=fills_offset,
                    )
                ),
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.report.backtest",
        arguments={
            "run_id": run_id,
            "equity_mode": equity_mode,
            "fills_limit": fills_limit,
            "fills_offset": fills_offset,
        },
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
                report = reporting.aggregate_run_report(
                    run_row, artifacts, view="detail"
                )
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
                # 导出文件走全量(equity / fills 不降采样不分页,#172 先例)。
                report = reporting.aggregate_backtest_report(
                    bt_row, fills_limit=None
                )
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
            "聚合单个 ResearchRun 报告。view=summary(默认):run 元信息 + result "
            "指标(剔除 equity_curve,以 equity_point_count 提示)+ universe "
            "聚合计数(total/included/excluded_by_reason,与全量判定一致)"
            "+ fills 按决策计数,不序列化逐标的全量 payload;view=detail:全部 "
            "artifacts 含 report/equity/decisions 各阶段 payload(诊断用,体积大)。"
            "只读。"
        ),
    )
    async def _run(
        run_id: str,
        view: str = "summary",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await report_run(app_context(ctx), run_id, view=view)

    @mcp.tool(
        name="finboard_report_backtest",
        description=(
            "聚合单条回测历史报告:运行元信息 + metrics + equity_curve + fills +"
            "summary(标准化结构)。equity_mode(summary 默认:降采样到 max_points"
            "个关键点;full:完整曲线)、max_points(默认 200)、fills_limit/"
            "fills_offset(fills 分页,默认有界 200 条;fills_limit=null 返回全部,"
            "返回含 fills_total/fills_offset 元信息)。只读。"
        ),
    )
    async def _backtest(
        run_id: int,
        equity_mode: str = "summary",
        max_points: int = 200,
        fills_limit: int | None = reporting.DEFAULT_FILLS_LIMIT,
        fills_offset: int = 0,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await report_backtest(
            app_context(ctx),
            run_id,
            equity_mode=equity_mode,
            max_points=max_points,
            fills_limit=fills_limit,
            fills_offset=fills_offset,
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

