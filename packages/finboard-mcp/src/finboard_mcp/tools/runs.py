"""``finboard.run.*`` 工具 —— ResearchRun 查询与写操作(复用 ``ResearchRunRepository``)。

只读(3):list / get / artifacts —— 自动允许。
写(4,issue #127):queue(冻结 + 登记 queued)/ cancel /
replay(复制 completed;interrupted 即事故恢复通道,#305)/
lineage(artifact 血缘)。

写工具在 ``mcp_readonly_only=false`` 时由 agent 自主执行(#122),不执行回测本身
(回测执行由离线 worker 调 ``ResearchRunCoordinator`` 完成)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_app.research_operation import (
    ResearchOperationContext,
    ResearchOperationError,
)
from finboard_app.research_run_submission import (
    ResearchRunReplayInput,
    ResearchRunSubmissionService,
    ResearchRunSubmitInput,
)
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.identity import current_identity
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence.background_job_repo import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)
from finboard_persistence.research_run_repo import ResearchRunRepository

if TYPE_CHECKING:
    from finboard_persistence.models import ResearchRunArtifactModel, ResearchRunModel
    from finboard_persistence.research_run_repo import ResearchRunArtifactSummary


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
        # issue #183:执行模式(single_shot|multi_period),入队即标注。
        "execution_mode": _execution_mode_from_manifest(row.manifest),
    }


def _execution_mode_from_manifest(manifest: object) -> str:
    """从存储的 manifest dict 推导 execution_mode(issue #183 quick win)。"""
    from finboard_backtest.research_run.contracts import execution_mode_for

    if not isinstance(manifest, dict):
        return "single_shot"
    parameters = manifest.get("parameters")
    if not isinstance(parameters, dict):
        return "single_shot"
    return execution_mode_for(parameters).value


def _run_detail(row: ResearchRunModel) -> dict[str, Any]:
    from finboard_backtest.research_run.config_overrides import fee_policy_summary

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
            # issue #183:agent 用 execution_mode 区分单时点决策与全区间回放。
            "execution_mode": _execution_mode_from_manifest(row.manifest),
            # issue #482:生效费用参数与来源(规格 vs 队列覆盖)读侧派生回显,
            # 纯视图不参与 checksum;overrides 非空时 effective ≠ spec。
            "fee_policy": (
                fee_policy_summary(row.manifest)
                if isinstance(row.manifest, dict)
                else None
            ),
            # issue #143:关联 background_jobs.job_id,agent 可用 finboard_job_* 轮询。
            "job_id": getattr(row, "job_id", None),
        }
    )
    return cast(dict[str, Any], to_jsonable(detail))


def _run_view(
    row: ResearchRunModel,
    artifacts: list[ResearchRunArtifactModel] | None = None,
    *,
    view: str = "summary",
    artifact_summary: ResearchRunArtifactSummary | None = None,
) -> dict[str, Any]:
    """run 详情视图(issue #206):summary 默认聚合计数,detail 全量。

    summary 在 detail 的头部字段之上,把 result 剔除 equity_curve(以点数
    提示),并附 universe / fills 服务端聚合计数,不序列化 manifest/result
    与逐标的全量 payload。

    issue #478:``artifact_summary`` 提供数据库侧聚合计数时优先消费,
    summary 不再要求物化全量 artifacts(两者都缺省时保持旧调用形态,
    不输出 artifact_count / universe / fills 键)。
    """
    if view not in ("summary", "detail"):
        raise McpToolError("invalid_argument", f"未知视图: {view}")
    if view == "detail":
        return _run_detail(row)
    from finboard_mcp.reporting import (
        metrics_without_equity_curve,
        summarize_run_artifacts,
    )

    payload = _run_summary(row)
    payload.update(
        {
            "idempotency_key": row.idempotency_key,
            "replay_of_run_id": row.replay_of_run_id,
            "manifest_checksum": row.manifest_checksum,
            "result_checksum": row.result_checksum,
            "error_summary": row.error_summary,
            "job_id": getattr(row, "job_id", None),
            "metrics": metrics_without_equity_curve(
                dict(row.result) if row.result else {}
            ),
            "view": "summary",
        }
    )
    if artifacts is not None:
        payload["artifact_count"] = len(artifacts)
        payload.update(summarize_run_artifacts(artifacts))
    elif artifact_summary is not None:
        payload["artifact_count"] = artifact_summary.artifact_count
        payload.update(artifact_summary.summary_dict())
    return cast(dict[str, Any], to_jsonable(payload))


def _run_ack(row: ResearchRunModel) -> dict[str, Any]:
    """写操作精简回执(issue #206 P1):id/status/checksum/execution_mode/created_at。

    全量详情走 ``finboard_run_get(run_id)``;回执附带 job_id 供轮询。
    """
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "run_id": row.run_id,
                "job_id": getattr(row, "job_id", None),
                "strategy_id": row.strategy_id,
                "strategy_kind": row.strategy_kind,
                "status": row.status,
                "checksum": row.manifest_checksum,
                "execution_mode": _execution_mode_from_manifest(row.manifest),
                "created_at": to_jsonable(row.created_at),
                "view": "ack",
                "detail_hint": f"finboard_run_get(run_id={row.run_id!r})",
            }
        ),
    )


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


async def get_run(
    app: McpAppContext,
    run_id: str,
    *,
    view: str = "summary",
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {run_id}")
            if view != "summary":
                return _run_view(row, view=view)
            # issue #478:summary 聚合下沉 PostgreSQL —— 旧路径先全量物化
            # artifacts(真实 run 7203 artifacts / ≈5.9GB JSON 曾把进程顶到
            # 13GB 后 MCP 超时);现在只取回几十个计数字段。
            summary = await repo.summarize_artifacts(run_id)
            return _run_view(row, view=view, artifact_summary=summary)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.get",
        arguments={"run_id": run_id, "view": view},
        handler=_do,
    )


async def list_artifacts(app: McpAppContext, run_id: str) -> ToolEnvelope:
    """run 全量 artifacts(含 payload;逐 artifact 体量见 #206 P0 有界约定)。

    issue #480:加载前先数据库侧估计载荷(``SUM(octet_length)``),超限
    具名 ``payload_too_large``,绝不把全量 payload 拉进进程(真实 run
    7203 artifacts / ≈5.9GB 曾把进程顶到 10GB+);单决策有界数据走
    ``finboard_report_run(view="detail", decision_id=...)`` 下钻。
    """

    async def _do() -> list[dict[str, Any]]:
        from finboard_mcp import reporting
        from finboard_mcp.tools.reports import _payload_too_large_error

        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            row = await repo.get(run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {run_id}")
            estimated = await repo.estimate_artifact_payload_bytes(run_id)
            if estimated > reporting.RUN_DETAIL_MAX_ESTIMATED_BYTES:
                raise _payload_too_large_error(run_id, estimated)
            artifacts = await repo.list_artifacts(run_id)
            return [_artifact_summary(item) for item in artifacts]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.artifacts",
        arguments={"run_id": run_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 写工具(issue #127):queue / cancel / replay / lineage
# --------------------------------------------------------------------------- #
async def _require_write_enabled(app: McpAppContext) -> None:
    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _mcp_error(exc: ResearchOperationError) -> McpToolError:
    """统一业务错误 -> MCP ``McpToolError``(kind 取自错误类,retryable 透传)。

    细业务码(如 ``scope_release_drift``)以 ``(code)`` 后缀进入消息文本 ——
    MCP 信封的 ``error`` 只有 kind / message / retryable,没有 details 字段。
    """

    message = exc.message if exc.code == exc.category else f"{exc.message}({exc.code})"
    return McpToolError(exc.mcp_kind, message, retryable=exc.retryable)


def _mcp_operation_context(
    app: McpAppContext, operation_id: str | None = None
) -> ResearchOperationContext:
    """当前 MCP 调用的服务端身份(#518):客户端 ID 来自 #517 凭据解析。

    ``operation_id`` 由 #515 课题发起工具显式传入,用于响应丢失后的幂等找回。
    #516:已授予作用域随上下文下传,供 per-action 强制(``research.write`` /
    ``research.run``)判断;MCP 通道缺 scopes 时按 fail-closed 拒绝。
    """

    identity = getattr(app, "identity", None) or current_identity()
    return ResearchOperationContext.for_mcp(
        client_id=identity.client_id if identity is not None else None,
        operation_id=operation_id,
        scopes=identity.scopes if identity is not None else None,
    )


async def enqueue_research_run(
    app: McpAppContext,
    body: ResearchRunSubmitInput,
) -> dict[str, Any]:
    """冻结 + 登记 queued ResearchRun(写,不执行回测)。

    供 ``queue_run`` 与 ``backtest_run(strategy_spec=...)`` 复用;编排(预检 /
    冻结 / manifest / 双写 run+job)全部走
    :class:`~finboard_app.research_run_submission.ResearchRunSubmissionService`
    (issue #513),本函数只负责会话、唯一一次 commit 与 MCP 错误映射。
    """

    async with app.session_maker() as session:
        service = ResearchRunSubmissionService(
            session, sandbox_enabled=app.settings.research_sandbox_enabled
        )
        try:
            outcome = await service.submit(body, _mcp_operation_context(app))
            await session.commit()
        except ResearchOperationError as exc:
            await session.rollback()
            raise _mcp_error(exc) from exc
        # issue #206 P1:写操作返回精简回执(六字段 + job 指针),
        # 全量详情走 finboard_run_get。
        return _run_ack(outcome.run_row)


async def queue_run(
    app: McpAppContext,
    *,
    payload: dict[str, Any],
) -> ToolEnvelope:
    """冻结输入 + 登记 queued ResearchRun(写,不执行回测)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        body = ResearchRunSubmitInput.from_json_payload(payload)
        return await enqueue_research_run(app, body)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.queue",
        arguments={"strategy_id": payload.get("strategy_id"),
                   "strategy_version": payload.get("strategy_version")},
        handler=_do,
    )


async def cancel_run(app: McpAppContext, run_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from finboard_app.research_run_store import SqlAlchemyResearchRunStore
        from finboard_backtest.research_run import (
            ResearchRunConflictError,
            ResearchRunCoordinator,
        )
        from finboard_persistence import (
            ResearchRunPersistenceConflictError,
            ResearchRunRepository,
        )

        async with app.session_maker() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            coordinator = ResearchRunCoordinator(store)
            try:
                record = await coordinator.cancel(run_id)
                # issue #143:协作式取消联动 background_jobs(已终态 job 幂等 suppress)。
                if getattr(record, "job_id", None):
                    import contextlib

                    with contextlib.suppress(BackgroundJobPersistenceConflictError):
                        await BackgroundJobRepository(session).request_cancel(
                            record.job_id  # type: ignore[arg-type]
                        )
                await session.commit()
            except (
                ResearchRunConflictError,
                ResearchRunPersistenceConflictError,
            ) as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            row = await ResearchRunRepository(session).get(record.manifest.run_id)
            if row is None:
                raise McpToolError("not_found", f"研究运行不存在: {run_id}")
            return _run_detail(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.cancel",
        arguments={"run_id": run_id},
        handler=_do,
    )


async def replay_run(
    app: McpAppContext,
    run_id: str,
    *,
    idempotency_key: str,
    requested_by: str,
) -> ToolEnvelope:
    """复制 completed/interrupted ResearchRun 为新 queued 运行(写,不执行)。

    issue #305:interrupted 源即事故恢复通道 —— 新 run 经 ``replace(manifest)``
    自动继承全部冻结输入(含 factor_snapshots 全部 ID,零手工),血缘标注
    ``replay_of_run_id`` + ``replay_source_status``;cancelled 仍拒绝。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        body = ResearchRunReplayInput(
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )
        async with app.session_maker() as session:
            service = ResearchRunSubmissionService(
                session, sandbox_enabled=app.settings.research_sandbox_enabled
            )
            try:
                outcome = await service.replay(
                    run_id, body, _mcp_operation_context(app)
                )
                await session.commit()
            except ResearchOperationError as exc:
                await session.rollback()
                raise _mcp_error(exc) from exc
            return _run_detail(outcome.run_row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.replay",
        arguments={"run_id": run_id, "idempotency_key": idempotency_key,
                   "requested_by": requested_by},
        handler=_do,
    )


async def lineage_run(
    app: McpAppContext,
    run_id: str,
    trace_id: str,
) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        from finboard_app.research_run_store import SqlAlchemyResearchRunStore
        from finboard_backtest.research_run import ResearchRunCoordinator

        async with app.session_maker() as session:
            repo = ResearchRunRepository(session)
            store = SqlAlchemyResearchRunStore(repo)
            # issue #480:coordinator.lineage 内部全量物化 artifacts 再按
            # trace 图过滤,与 artifacts / detail 同款加载前护栏。
            from finboard_mcp import reporting
            from finboard_mcp.tools.reports import _payload_too_large_error

            estimated = await repo.estimate_artifact_payload_bytes(run_id)
            if estimated > reporting.RUN_DETAIL_MAX_ESTIMATED_BYTES:
                raise _payload_too_large_error(run_id, estimated)
            coordinator = ResearchRunCoordinator(store)
            artifacts = await coordinator.lineage(run_id, trace_id)
            if not artifacts:
                raise McpToolError("not_found", f"血缘 trace 不存在: {trace_id}")
            return cast(
                dict[str, Any],
                to_jsonable(
                    {
                        "run_id": run_id,
                        "leaf_trace_id": trace_id,
                        "artifacts": [
                            {
                                "artifact_id": item.artifact_id,
                                "run_id": item.run_id,
                                "decision_id": item.decision_id,
                                "sequence": item.sequence,
                                "stage": item.stage.value,
                                "trace_id": item.trace_id,
                                "parent_trace_ids": list(item.parent_trace_ids),
                                "payload": item.payload,
                                "checksum": item.checksum,
                                "created_at": to_jsonable(item.created_at),
                            }
                            for item in artifacts
                        ],
                    }
                ),
            )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.run.lineage",
        arguments={"run_id": run_id, "trace_id": trace_id},
        handler=_do,
    )


def register(mcp: MCPServer) -> None:
    """把 ResearchRun 工具注册到 MCP server(3 只读 + 4 写)。"""

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
        description=(
            "查询单个 ResearchRun。view=summary(默认):头部字段 + metrics"
            "(剔除 equity_curve,以 equity_point_count 提示)+ universe 聚合计数"
            "(total/included/excluded_by_reason)+ fills 按决策计数 + artifact_count,"
            "不序列化 manifest/result 全量;view=detail:含 manifest / result / "
            "逐标的全量 payload(诊断用,可达 MB 级)。失败运行的 error_summary"
            "(issue #263)头部自带定位上下文"
            "[stage=...; decision=...; decision_index=...; release=...; "
            "dataset_releases=...],原始异常消息在尾部,排障先读头部。"
        ),
    )
    async def _get(
        run_id: str,
        view: str = "summary",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await get_run(app_context(ctx), run_id, view=view)

    @mcp.tool(
        name="finboard_run_artifacts",
        description="列出某 ResearchRun 的逐阶段 artifact(含 trace 与 payload)。",
    )
    async def _artifacts(run_id: str, ctx: Context = None) -> ToolEnvelope:  # type: ignore[assignment]
        return await list_artifacts(app_context(ctx), run_id)

    @mcp.tool(
        name="finboard_run_queue",
        description=(
            "冻结输入 + 登记 queued ResearchRun(写,不执行回测,由离线 worker "
            "执行)。payload 为 JSON 对象,模板与取值来源(必填标 *):\n"
            '- "idempotency_key"*: 8-128 字符去重键;重提交返回同一 run\n'
            '- "strategy_id"*: 已发布策略规格 id(finboard_strategy_list / '
            "registry 查询)\n"
            '- "strategy_version"*: 整数 >=1(策略规格版本)\n'
            '- "dataset_release_ids"*: 冻结数据发布 release_id 列表,必须与策略'
            "验证计划完全一致(finboard_dataset_release_list 查询)\n"
            '- "data_scope"/"data_scope_resolution_id"(#510,可选):'
            "统一数据范围声明 + finboard_data_scope_preview 返回的 SC- id。"
            "声明后提交期用同一份 scope 重算比对 resolution_id —— 预览后若上线"
            "了新发布即 conflict(要求重新预览),不静默换最新发布;解析出的"
            " research_window 写进 parameters 并随 manifest 冻结。两个字段必须"
            "成对出现;不声明则行为与旧入口完全一致\n"
            '- "factor_snapshot_ids": 冻结特征快照 snapshot_id 列表'
            "(finboard_feature_snapshot_list 查询);single_shot 必填(决策时点"
            "只能来自快照,缺快照入队即拒 #203);multi_period 声明决策日历后价格"
            "因子不需要,基本面因子(pb/ROE 等)仍需快照/研究数据发布\n"
            '- "parameters": {} —— 不声明即 single_shot;声明 '
            "decision_schedule={kind: daily|weekly|monthly|quarterly|custom"
            "(,dates: [YYYY-MM-DD...] 仅 custom 必填)} 触发多期回放"
            "(#361;weekly=每周最后交易日,custom dates 须 ⊆ 发布交易日且"
            "升序去重,非法值入队即拒);legacy "
            "rebalance_frequency=daily|weekly|monthly|quarterly 仍接受"
            "(等价同名 kind,旧值零变化)。multi_period 特征"
            "可用性入队即判(#253):规格引用的特征必须可由多期供给派生"
            "(标准价格特征/close/attached daily_metrics 与 financial_indicators"
            " 发布/快照观测),声明 pb/roe 等财务因子而未附加对应研究数据发布"
            " → invalid_argument 具名缺失特征与所需发布 kind。multi_period 引用"
            "用户因子(u_ 前缀)不再一刀切拒绝(#361):入队检查因子 series"
            " 覆盖 —— 无 series / 覆盖不足 / series 锚定发布与 bars 主发布"
            "不一致 → invalid_argument(附缺失决策日期预览与"
            " finboard_factor_series_build 重建命令)\n"
            '- "validation_config"/"execution_config": {} —— 留空;非空入队即拒'
            "(#482,死分区不接覆盖):验证门控 / timing 等执行语义须发布新规格"
            "版本调整\n"
            '- "fee_config": {} —— 成交费用覆盖(#482 接线),overrides 按键名'
            "与规格 execution_model 同名合并:{\"overrides\": {\"commission_rate\":"
            " 0.0006, \"minimum_commission\": 10, \"sell_tax_rate\": 0.001, "
            "\"slippage_bps\": 10}}(合法域 佣金/印花 0<=值<=0.1、最低佣金 >=0、"
            "滑点 0<=值<=10000;未声明键继承规格值;未知键/非法值入队即拒;"
            "timing 不支持队列覆盖);run 详情 fee_policy 字段回显生效值与来源\n"
            '- "benchmark_config": {} —— 基准标的覆盖(如 {"symbol": '
            '"000300.SH"})\n'
            '- "portfolio_config"/"risk_config": {} —— 组合约束 / 风险退出'
            "分区覆盖(#303,键位不可混):\n"
            "  · portfolio_config 管组合约束(overrides 直接就是键值),如 "
            '{"max_risk_contribution": 0.5}(默认 0.35,合法域 0<值<=1,=1 '
            "关闭该约束;隐含买入池 n>=ceil(1/值))、risk_factor_limits(#266)\n"
            "  · risk_config 管风险退出(stop-loss 等),形态 "
            '{"rules": [{"rule_type": "price_stop_loss", "enabled": true, '
            '"threshold": 0.08}]},按 rule_type 与规格策略同名合并、覆盖同名键'
            "(未声明字段继承基准值;新 rule_type 须给全字段含 rationale)\n"
            '- "code_version"*: 7-64 字符,**本 run 自身的代码版本标识**(如 '
            "FinBoard git commit),冻结进 manifest/checksum 供追溯;与数据集发布"
            "的 code_version 只是同名字段、互不校验,别拿数据集 git hash 顶替\n"
            '- "initial_capital"*: 100000-500000 数字\n'
            '- "requested_by"*: 归属人(如 user:xxx / agent:mcp)\n'
            '- "actor_type": "agent"(MCP 通道固定,#312 与 requested_by 对齐;'
            'llm 不能触发运行)\n'
            "校验失败返回 invalid_argument 并附原因(复用 ResearchRunQueueIn "
            "schema)。入队预检(#186):universe 候选池为空秒级 invalid_argument,"
            "错误附各过滤条件排除统计与缺失字段名。single_shot 缺冻结快照同样"
            "入队秒级拒绝(#203,报错附 execution_mode 与缺失因子源)。"
            "快照锚定预检(#355):引用快照锚定的数据发布 ⊄ 本次 "
            "dataset_release_ids(典型场景:更换 bars 主发布后未重算沙箱因子)"
            "秒级 invalid_argument,具名 snapshot_anchor_mismatch,逐快照列出"
            "因子名/锚定发布/缺失清单,修复路径二选一:把缺失发布一并加入 "
            "dataset_release_ids,或 finboard_research_code_run 对新发布重算"
            "(RCR → 质量门 → 新快照)后引用。"
            "组合可行性预检(#303):静态候选池 < ceil(1/生效 "
            "max_risk_contribution)秒级 invalid_argument(错误附排除统计、"
            "生效阈值与 portfolio_config.overrides 键位修复路径),"
            "max_risk_contribution 非法值与 risk_config.overrides 形态错误"
            "同样入队即拒;逐期真实买入池不可精确预知,运行期 fail-closed 兜底"
            "不变。"
            "user_code 策略(#218):strategy_kind=user_code 的规格已声明 "
            "code_artifact(name+可选 commit,引用 kind=strategy 的 active "
            "artifact);入队门控 artifact 非 active / commit 不一致 / 沙箱未启用 "
            "/ single_shot 缺快照 → 秒级拒绝,放行时 active commit 冻结进 "
            "manifest;逐决策日沙箱 decide(ctx)→目标权重 复用组合管线,report "
            "附 sandbox_provenance(commit+镜像 digest)。\n"
            "screen 通道(#234):首次晋级需要 screen 证据而 draft 产物不可被"
            "普通运行引用 —— 已发布规格声明 screen_artifact_bindings"
            "({kind, name, artifact_id, commit?})时,入队按 DB 实绑校验"
            "(存在/非 retired/name/commit 一致,错绑秒级 invalid_argument)"
            "并放行 draft 引用,strategy 绑定的 commit+artifact_id 冻结进 "
            "manifest(code_artifact),factor 绑定要求其沙箱快照"
            "(finboard_research_code_run 显式 artifact_id 产出)已进入 "
            "factor_snapshot_ids。未声明绑定的普通运行引用 draft/retired 仍被"
            "秒级拒绝;promote 侧四向校验兜底,screen 运行不可挪作他版代码"
            "的证据。"
            "返回精简回执(issue #206):run_id/job_id/status/checksum/"
            "execution_mode/created_at,全量详情走 finboard_run_get。"
        ),
    )
    async def _queue(
        payload: dict[str, Any],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await queue_run(app_context(ctx), payload=payload)

    @mcp.tool(
        name="finboard_run_cancel",
        description="取消 ResearchRun(queued/running/interrupted/failed → cancelled,写)。",
    )
    async def _cancel(
        run_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await cancel_run(app_context(ctx), run_id)

    @mcp.tool(
        name="finboard_run_replay",
        description=(
            "复制 completed 或 interrupted ResearchRun 为新 queued 运行"
            "(写,不执行)。需要新 idempotency_key + requested_by;"
            "新 run actor_type=agent(MCP 通道固定,#312)。"
            "interrupted 源即事故恢复通道(#305):新 run 自动继承原 manifest "
            "全部冻结输入(dataset_release_ids / factor_snapshots 全部 ID,"
            "零手工重填),血缘标注 replay_of_run_id + replay_source_status;"
            "completed 源保持确定性重放对照(结果漂移判 failed);"
            "cancelled 是显式用户意图,拒绝重放。"
        ),
    )
    async def _replay(
        run_id: str,
        idempotency_key: str,
        requested_by: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await replay_run(
            app_context(ctx),
            run_id,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )

    @mcp.tool(
        name="finboard_run_lineage",
        description="查询某 ResearchRun 内指定 trace_id 的 artifact 血缘(只读,BFS 向上)。",
    )
    async def _lineage(
        run_id: str,
        trace_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await lineage_run(app_context(ctx), run_id, trace_id)


__all__ = [
    "cancel_run",
    "enqueue_research_run",
    "get_run",
    "lineage_run",
    "list_artifacts",
    "list_runs",
    "queue_run",
    "register",
    "replay_run",
]
