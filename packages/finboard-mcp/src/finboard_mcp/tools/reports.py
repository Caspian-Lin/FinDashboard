"""``finboard.report.*`` 工具 —— 报告聚合与导出(issue #141)。

只读(3):``report_run``(聚合 ResearchRun 报告:result 指标 + 全部 artifacts)/
``report_backtest``(聚合回测报告:metrics + equity_curve + fills)/
``report_export``(导出为 CSV / Markdown 文件,返回绝对路径)。

聚合与渲染逻辑在 ``finboard_mcp.reporting``(纯标准库 csv + 字符串模板,
零新依赖);本模块只做 DB 读取 + 工具封装。导出文件写入
``FINBOARD_EXPORT_DIR``(缺省系统临时目录下 finboard_exports),不持久化
到 DB,不触及交易安全红线。

issue #458:detail / 导出的聚合 + JSON 化是 O(payload) 的同步 CPU 段,
统一挪 ``asyncio.to_thread`` 执行(重调用不毒化事件循环);``view=detail``
在聚合前做廉价规模估计,超过 ``RUN_DETAIL_MAX_ESTIMATED_BYTES`` 具名抛
``payload_too_large``(不静默截断),大 run 经 ``decision_id`` 按决策下钻。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp import reporting
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable

if TYPE_CHECKING:
    from finboard_persistence.models import (
        ResearchRunArtifactModel,
        ResearchRunModel,
    )
    from finboard_persistence.research_run_repo import (
        ResearchRunArtifactSummary,
        ResearchRunRepository,
    )

__all__ = ["register"]


def _payload_too_large_error(run_id: str, estimated_bytes: int) -> McpToolError:
    """构造 detail / 导出载荷超限的具名错误(附规模与替代路径,#458)。"""
    estimated_mb = estimated_bytes / (1024 * 1024)
    limit_mb = reporting.RUN_DETAIL_MAX_ESTIMATED_BYTES / (1024 * 1024)
    return McpToolError(
        "payload_too_large",
        f"run {run_id} 的 detail 载荷估计约 {estimated_mb:.1f}MB,超过上限 "
        f"{limit_mb:.0f}MB,拒绝加载与序列化(不静默截断)。替代路径:view=summary "
        '看聚合计数;view="detail" + decision_id=... 按决策下钻(单决策 '
        'artifacts 有界);report_export(kind="run", decision_id=...) 导出'
        "单决策。",
    )


async def _require_detail_payload_within_limit(
    repo: ResearchRunRepository,
    run_id: str,
    decision_id: str | None,
) -> None:
    """detail 未下钻时,加载前用数据库侧估计做载荷护栏(#480)。

    旧口径(#458)在 ``list_artifacts`` 全量物化之后才估计,只能拒绝序列化、
    挡不住加载本身 —— 真实 run 实测加载阶段就把进程顶到 10GB+。现在先
    ``estimate_artifact_payload_bytes``(SQL 侧 SUM(octet_length),不取回
    payload),超限直接具名 ``payload_too_large``,根本不加载;下钻单决策
    artifacts 有界,维持豁免。
    """
    if decision_id is not None:
        return
    estimated = await repo.estimate_artifact_payload_bytes(run_id)
    if estimated > reporting.RUN_DETAIL_MAX_ESTIMATED_BYTES:
        raise _payload_too_large_error(run_id, estimated)


def _build_run_report(
    row: ResearchRunModel,
    artifacts: list[ResearchRunArtifactModel] | None,
    *,
    view: str,
    decision_id: str | None,
    artifact_summary: ResearchRunArtifactSummary | None = None,
) -> dict[str, Any]:
    """聚合 run 报告(线程池内执行,O(payload) 的同步 CPU 段,#458)。

    ``McpToolError`` 经 ``await`` 透传,``run_tool`` 的异常映射语义不变。
    ``report_run`` 与 ``report_export(kind="run")`` 共用同一聚合出口。
    下钻 ``decision_id`` 无匹配 artifacts 时具名 ``not_found``(run 级
    artifact 如 report 的 decision_id 为 null,不参与下钻匹配)。

    issue #478:``view=summary`` 可传 ``artifact_summary``(数据库侧聚合)
    替代全量 artifacts,与 ``finboard_run_get`` 同源同口径;detail 仍需
    全量 artifacts(序列化消费 payload)。

    issue #480:载荷护栏前移到加载之前(``report_run`` / ``report_export``
    先经 ``estimate_artifact_payload_bytes`` 数据库侧估计再决定是否加载,
    超限根本不把 payload 拉进进程);本函数不再做加载后估计。
    """
    report = reporting.aggregate_run_report(
        row,
        artifacts,
        view=view,
        decision_id=decision_id,
        artifact_summary=artifact_summary,
    )
    if decision_id is not None and not report["filtered_artifact_count"]:
        raise McpToolError(
            "not_found",
            f"run {row.run_id} 没有 decision_id={decision_id} 的 artifacts"
            "(run 级 artifact 的 decision_id 为 null,不参与下钻);可用决策"
            "见 view=summary 的 fills.by_decision 键",
        )
    return report


def _run_report_jsonable(
    row: ResearchRunModel,
    artifacts: list[ResearchRunArtifactModel] | None,
    *,
    view: str,
    decision_id: str | None,
    artifact_summary: ResearchRunArtifactSummary | None = None,
) -> dict[str, Any]:
    """:func:`_build_run_report` + JSON 兼容化(线程池内一次完成,#458)。"""
    report = _build_run_report(
        row,
        artifacts,
        view=view,
        decision_id=decision_id,
        artifact_summary=artifact_summary,
    )
    return cast(dict[str, Any], to_jsonable(report))


async def report_run(
    app: McpAppContext,
    run_id: str,
    *,
    view: str = "summary",
    decision_id: str | None = None,
) -> ToolEnvelope:
    """聚合 ResearchRun 报告:view=summary 默认(聚合计数)/ detail(全量)。

    issue #458:``decision_id`` 仅 detail 可用 —— 只返回该决策的 artifacts
    (大 run 的诊断下钻通道,护栏豁免);summary 传 ``decision_id`` 拒绝
    (invalid_argument)。detail 未下钻且载荷估计超限时抛 ``payload_too_large``。
    """

    async def _do() -> dict[str, Any]:
        from finboard_persistence import ResearchRunRepository

        if view not in ("summary", "detail"):
            raise McpToolError("invalid_argument", f"未知视图: {view}")
        if decision_id is not None and view != "detail":
            raise McpToolError(
                "invalid_argument",
                "decision_id 仅支持 view=detail(summary 视图为聚合计数,"
                "没有逐 artifact 概念)",
            )
        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {run_id}")
            if view == "summary":
                # issue #478:与 finboard_run_get 同走数据库侧聚合,不再
                # 为计数摘要物化全量 payload(真实 run 曾 13GB OOM)。
                summary = await repo.summarize_artifacts(run_id)
                return await asyncio.to_thread(
                    _run_report_jsonable,
                    row,
                    None,
                    view=view,
                    decision_id=None,
                    artifact_summary=summary,
                )
            # issue #480:护栏前移 —— 超限在加载前拒绝,不把 payload 拉进进程。
            await _require_detail_payload_within_limit(repo, run_id, decision_id)
            artifacts = await repo.list_artifacts(run_id)
            # 聚合 + JSON 化挪线程池:GB 级 payload 的同步 CPU 段不再阻塞
            # 事件循环,重调用期间其它工具请求照常响应(#458)。
            return await asyncio.to_thread(
                _run_report_jsonable,
                row,
                artifacts,
                view=view,
                decision_id=decision_id,
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.report.run",
        arguments={"run_id": run_id, "view": view, "decision_id": decision_id},
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
            # 聚合 + JSON 化挪线程池(full 模式的 equity/fills 也是
            # O(行数) 的同步 CPU 段,#458 同款防毒化)。
            report = await asyncio.to_thread(
                lambda: to_jsonable(
                    reporting.aggregate_backtest_report(
                        row,
                        equity_mode=equity_mode,
                        max_points=max_points,
                        fills_limit=fills_limit,
                        fills_offset=fills_offset,
                    )
                )
            )
            return cast(dict[str, Any], report)

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
    *,
    decision_id: str | None = None,
) -> ToolEnvelope:
    """把报告聚合后导出为 CSV / Markdown 文件,返回绝对路径。

    kind ∈ run | backtest;fmt ∈ csv | markdown;id 为 run_id(RR-)
    或回测记录 id。文件写入导出目录,不持久化到 DB。

    issue #458:kind=run 的 detail 聚合与 ``report_run(view="detail")``
    同护栏 —— 载荷估计超限抛 ``payload_too_large``;``decision_id``(仅
    kind=run)过滤导出单个决策的 artifacts,作为大 run 的导出下钻通道
    (传入时豁免护栏)。
    """

    async def _do() -> dict[str, Any]:
        from finboard_persistence import BacktestRunRepository, ResearchRunRepository

        if kind not in reporting.REPORT_KINDS:
            raise McpToolError("invalid_argument", f"未知报告类型: {kind}")
        if fmt not in reporting.EXPORT_FORMATS:
            raise McpToolError("invalid_argument", f"未知导出格式: {fmt}")
        if decision_id is not None and kind != "run":
            raise McpToolError(
                "invalid_argument", "decision_id 仅支持 kind=run"
            )
        async with app.session_maker() as session:
            if kind == "run":
                run_repo = ResearchRunRepository(session)
                run_row = await run_repo.get(id)
                if run_row is None:
                    raise McpToolError("not_found", f"研究运行不存在: {id}")
                # issue #480:与 report_run 同款加载前护栏(下钻豁免)。
                await _require_detail_payload_within_limit(run_repo, id, decision_id)
                artifacts = await run_repo.list_artifacts(id)
                # 聚合(含 #458 载荷护栏)挪线程池,与 report_run 同出口。
                report = await asyncio.to_thread(
                    _build_run_report,
                    run_row,
                    artifacts,
                    view="detail",
                    decision_id=decision_id,
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
                report = await asyncio.to_thread(
                    lambda: reporting.aggregate_backtest_report(
                        bt_row, fills_limit=None
                    )
                )
        # 文件写入放到线程池,避免阻塞事件循环。
        return cast(
            dict[str, Any],
            await asyncio.to_thread(reporting.export_report, kind, report, fmt),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.report.export",
        arguments={"kind": kind, "id": id, "format": fmt, "decision_id": decision_id},
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
            "artifacts 含 report/equity/decisions 各阶段 payload(诊断用,体积大;"
            "估计超过 64MB 时返回 payload_too_large,不静默截断)。decision_id"
            "(仅 view=detail):只返回该决策的 artifacts(单决策有界),大 run "
            "诊断下钻通道,summary 传 decision_id 返回 invalid_argument;detail "
            "不匹配的 decision_id 返回 not_found。只读。"
        ),
    )
    async def _run(
        run_id: str,
        view: str = "summary",
        decision_id: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await report_run(
            app_context(ctx), run_id, view=view, decision_id=decision_id
        )

    @mcp.tool(
        name="finboard_report_backtest",
        description=(
            "聚合单条回测历史报告:运行元信息 + metrics + equity_curve + fills +"
            "summary(标准化结构)。equity_mode(summary 默认:降采样到 max_points"
            "个关键点;full:完整曲线)、max_points(默认 200)、fills_limit/"
            "fills_offset(fills 分页,默认有界 200 条;fills_limit=null 返回全部,"
            "返回含 fills_total/fills_offset 元信息)。"
            "Sharpe 口径(#262):metrics.sharpe_ratio=主口径(rf 见 "
            "risk_free_annual,默认 3%/年,ddof=0);sharpe_rf0=rf=0 对照口径"
            "(ddof=1),与 research_run 报告 sharpe_ratio 同口径,跨报告比较用 "
            "sharpe_rf0。只读。"
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
            "系统临时目录),返回文件绝对路径。kind=run 的全量导出与 "
            "finboard_report_run(view=detail)同护栏:载荷估计超 64MB 返回 "
            "payload_too_large,此时用 decision_id(仅 kind=run)过滤导出单个"
            "决策的 artifacts。只读(不写 DB)。"
        ),
    )
    async def _export(
        kind: str,
        id: str,
        format: str,
        decision_id: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await report_export(
            app_context(ctx), kind, id, format, decision_id=decision_id
        )

