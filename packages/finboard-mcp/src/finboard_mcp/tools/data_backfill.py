"""``finboard.data_backfill.*`` 工具 —— 课题缺口 → 全局补数(issue #511)。

四个工具(2 只读 + 2 写):

* ``finboard_data_backfill_preview`` —— 纯读预检(缺口/步骤/配额/发布影响);
* ``finboard_data_backfill_submit`` —— 显式提交(写;``mcp_readonly_only`` 时拒绝);
* ``finboard_data_backfill_list`` —— 列表;带 ``request_id`` 时返回详情;
* ``finboard_data_backfill_cancel`` —— 协作式取消(写)。

与 REST ``/api/data/backfills`` 同一服务层(``finboard_backtest.data_backfill``),
工具面只做入参收敛与序列化。审计**只记录 scope_checksum / fingerprint / 数据集
清单** —— 补数请求可含几千个标的,完整 symbols 不进 ``mcp_audit_events``。
"""

from __future__ import annotations

from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_backtest.data_backfill.contracts import (
    BackfillPlan,
    BackfillPlanDriftError,
    BackfillPlanError,
    BackfillRequestSpec,
    request_fingerprint,
    scope_checksum,
)
from finboard_backtest.data_backfill.planner import plan_backfill
from finboard_backtest.data_backfill.service import (
    BackfillRequestError,
    cancel_backfill,
    submit_backfill,
)
from finboard_backtest.research_run.contracts import ResearchActorType
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_mcp.tools.data_write import _require_write_enabled
from finboard_persistence.data_backfill_repo import DataBackfillRepository
from finboard_persistence.models import (
    DataBackfillRequestItemModel,
    DataBackfillRequestModel,
)

#: 单页上限(与 REST 同口径)。
MAX_PAGE_LIMIT = 100


def _request_spec(raw: Any) -> BackfillRequestSpec:
    """把工具入参收敛成冻结 spec;非法结构一律 ``invalid_argument``。"""

    try:
        return BackfillRequestSpec.model_validate(raw)
    except ValueError as exc:
        raise McpToolError(
            "invalid_argument", f"补数请求 spec 非法: {exc}"
        ) from exc


def _clamp_limit(value: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 20
    return min(max(number, 1), MAX_PAGE_LIMIT)


def _clamp_offset(value: int) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _audit_arguments(spec: BackfillRequestSpec) -> dict[str, Any]:
    """审计摘要:scope checksum / fingerprint / 数据集清单,不含完整 symbols。"""

    return {
        "scope_checksum": scope_checksum(spec.scope),
        "fingerprint": request_fingerprint(spec),
        "datasets": [item.dataset.value for item in spec.ordered_datasets()],
    }


def _plan_out(plan: BackfillPlan) -> dict[str, Any]:
    payload = plan.model_dump(mode="json")
    payload["plan_checksum"] = plan.checksum()
    return payload


def _request_out(row: DataBackfillRequestModel) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "request_id": row.request_id,
                "fingerprint": row.fingerprint,
                "scope_checksum": row.scope_checksum,
                "status": row.status,
                "job_id": row.job_id,
                "release_ids": list(row.release_ids or []),
                "reused_request_ids": list(row.reused_request_ids or []),
                "retry_of_request_id": row.retry_of_request_id,
                "requested_by": row.requested_by,
                "actor_type": row.actor_type,
                "error_code": row.error_code,
                "error_summary": row.error_summary,
                "blockers": [dict(item) for item in (row.blockers or [])],
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "started_at": row.started_at,
                "completed_at": row.completed_at,
            }
        ),
    )


def _item_out(row: DataBackfillRequestItemModel) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "id": int(row.id),
                "dataset": row.dataset,
                "source": row.source,
                "adjustment": row.adjustment,
                "action": row.action,
                "status": row.status,
                "start_date": row.start_date,
                "end_date": row.end_date,
                "estimated_requests": int(row.estimated_requests or 0),
                "batch_ids": [int(item) for item in (row.batch_ids or [])],
                "release_id": row.release_id,
                "error_code": row.error_code,
                "error_summary": row.error_summary,
            }
        ),
    )


async def data_backfill_preview(
    app: McpAppContext, *, spec: Any, as_of: str | None = None
) -> ToolEnvelope:
    """只读预检:缺口范围、支持来源、配额、步骤与发布影响。"""

    try:
        arguments = _audit_arguments(_request_spec(spec))
    except McpToolError:
        arguments = {"spec": "<invalid>"}

    async def _do() -> dict[str, Any]:
        from datetime import datetime

        frozen = _request_spec(spec)
        parsed_as_of = None
        if as_of:
            try:
                parsed_as_of = datetime.fromisoformat(as_of)
            except ValueError as exc:
                raise McpToolError(
                    "invalid_argument", f"as_of 必须是 ISO 时间,收到: {as_of!r}"
                ) from exc
        async with app.session_maker() as session:
            try:
                plan = await plan_backfill(
                    session, frozen, as_of=parsed_as_of
                )
            except BackfillPlanError as exc:
                raise McpToolError(
                    "invalid_argument", f"{exc.code}: {exc.message}"
                ) from exc
        return _plan_out(plan)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data_backfill.preview",
        arguments=arguments,
        handler=_do,
    )


async def data_backfill_submit(
    app: McpAppContext,
    *,
    spec: Any,
    expected_plan_checksum: str | None = None,
    idempotency_key: str | None = None,
    actor_type: str = "agent",
    requested_by: str = "agent:mcp",
) -> ToolEnvelope:
    """提交补数请求(写;默认按 fingerprint 生成幂等键)。"""

    try:
        summary = _audit_arguments(_request_spec(spec))
        arguments: dict[str, Any] = {
            **summary,
            "idempotency_key": idempotency_key,
            "expected_plan_checksum": expected_plan_checksum,
        }
        key: str | None = (idempotency_key or "").strip() or (
            f"mcp:{summary['fingerprint']}"
        )
    except McpToolError:
        arguments = {"spec": "<invalid>"}
        key = idempotency_key

    async def _do() -> dict[str, Any]:
        # 写权限门在 handler 首行:run_tool 统一映射为 denied 信封(#137 约定)。
        await _require_write_enabled(app)
        frozen = _request_spec(spec)
        resolved_key = key or (
            f"mcp:{_audit_arguments(frozen)['fingerprint']}"
        )
        async with app.session_maker() as session:
            try:
                result = await submit_backfill(
                    session,
                    frozen,
                    idempotency_key=resolved_key,
                    requested_by=requested_by,
                    actor_type=(
                        ResearchActorType.AGENT
                        if actor_type == "agent"
                        else ResearchActorType.HUMAN
                    ),
                    expected_plan_checksum=expected_plan_checksum,
                )
                await session.commit()
            except BackfillPlanDriftError as exc:
                raise McpToolError(
                    "conflict",
                    f"backfill_plan_drift: 计划已变化(expected={exc.expected}, "
                    f"actual={exc.actual}),请重新预览",
                ) from exc
            except BackfillPlanError as exc:
                raise McpToolError(
                    "invalid_argument", f"{exc.code}: {exc.message}"
                ) from exc
            except BackfillRequestError as exc:
                raise McpToolError(
                    "not_found" if exc.code == "backfill_not_found" else "conflict",
                    f"{exc.code}: {exc.message}",
                ) from exc
            items = await DataBackfillRepository(session).list_items(
                result.request_id
            )
        payload = _request_out(result.request)
        payload["created"] = result.created
        payload["reused"] = result.reused
        payload["plan_checksum"] = result.plan.checksum()
        payload["items"] = [_item_out(item) for item in items]
        return payload

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data_backfill.submit",
        arguments=arguments,
        handler=_do,
        idempotency_key=key,
    )


async def data_backfill_list(
    app: McpAppContext,
    *,
    request_id: str | None = None,
    status: str | None = None,
    dataset: str | None = None,
    requested_by: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> ToolEnvelope:
    """只读:补数请求列表;给 ``request_id`` 时返回该请求详情(含明细)。"""

    safe_limit = _clamp_limit(limit)
    safe_offset = _clamp_offset(offset)
    arguments = {
        "request_id": request_id,
        "status": status,
        "dataset": dataset,
        "requested_by": requested_by,
        "limit": safe_limit,
        "offset": safe_offset,
    }

    async def _list_or_detail() -> dict[str, Any]:
        async with app.session_maker() as session:
            repo = DataBackfillRepository(session)
            if request_id:
                row = await repo.get(request_id.strip())
                if row is None:
                    raise McpToolError(
                        "not_found", f"补数请求不存在: {request_id}"
                    )
                items = await repo.list_items(row.request_id)
                payload = _request_out(row)
                payload["items"] = [_item_out(item) for item in items]
                return payload
            rows, total = await repo.list_requests(
                statuses=(
                    [item.strip() for item in status.split(",")]
                    if status
                    else None
                ),
                dataset=dataset or None,
                requested_by=requested_by or None,
                limit=safe_limit,
                offset=safe_offset,
            )
        return {
            "items": [_request_out(row) for row in rows],
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
        }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data_backfill.list",
        arguments=arguments,
        handler=_list_or_detail,
    )


async def data_backfill_cancel(
    app: McpAppContext,
    *,
    request_id: str,
    requested_by: str = "agent:mcp",
) -> ToolEnvelope:
    """协作式取消补数请求(写;已发布不回滚)。"""

    safe_request_id = (request_id or "").strip()

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        if not safe_request_id:
            raise McpToolError("invalid_argument", "request_id 不能为空")
        async with app.session_maker() as session:
            try:
                row = await cancel_backfill(
                    session, safe_request_id, requested_by=requested_by
                )
                await session.commit()
            except BackfillRequestError as exc:
                raise McpToolError(
                    "not_found" if exc.code == "backfill_not_found" else "conflict",
                    f"{exc.code}: {exc.message}",
                ) from exc
        return {
            "request_id": row.request_id,
            "status": row.status,
            "job_id": row.job_id,
            "cancel_requested": row.status == "cancelled",
        }

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.data_backfill.cancel",
        arguments={"request_id": safe_request_id},
        handler=_do,
    )


def register(mcp: MCPServer) -> None:
    """把补数工具注册到 MCP server(2 只读 + 2 写)。"""

    @mcp.tool(
        name="finboard_data_backfill_preview",
        description=(
            "只读:补数预检。入参 spec = {scope(#510 DataScopeSpec), datasets:["
            "{dataset, source?, adjustment?, start?, end?, symbols?, publish?}], "
            "quality_profile?}。返回 request_fingerprint / scope_checksum / "
            "plan_checksum + items[{dataset, source, action, start, end, "
            "missing_ranges, missing_symbols{count,preview}, covered_by, "
            "estimated_requests, publish_preview, blockers}] + quota + "
            "overlaps + publish_impact + blockers。纯读:不建请求、不触发同步"
            "或发布;显式 start/end 必须落在 scope 窗口内(否则 422 语义的 "
            "invalid_argument);bars 缓存只支持 adjustment=qfq,非 qfq 预检即 blocker。"
        ),
    )
    async def _preview(
        spec: dict[str, Any],
        as_of: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_backfill_preview(app_context(ctx), spec=spec, as_of=as_of)

    @mcp.tool(
        name="finboard_data_backfill_submit",
        description=(
            "写:显式提交补数请求(需写权限,只读模式拒绝)。入参 spec 同 "
            "preview;可选 expected_plan_checksum(提交前复算,不一致 → conflict "
            "backfill_plan_drift,必须重新预览)与 idempotency_key(缺省按 "
            "fingerprint 生成 mcp:<fingerprint>,等价重试回同一请求)。返回 "
            "request_id/job_id/status/created/reused/plan_checksum/items。"
            "失败/取消项用同一 request_id 的 retry 端点续补;取消走 "
            "finboard_data_backfill_cancel。"
        ),
    )
    async def _submit(
        spec: dict[str, Any],
        expected_plan_checksum: str | None = None,
        idempotency_key: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_backfill_submit(
            app_context(ctx),
            spec=spec,
            expected_plan_checksum=expected_plan_checksum,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(
        name="finboard_data_backfill_list",
        description=(
            "只读:补数请求列表 / 详情。给 request_id 时返回该请求详情(含逐个 "
            "item 的 action/status/batch_ids/release_id/error);否则按 "
            "status(逗号分隔)/ dataset / requested_by 过滤分页 "
            "{items,total,limit,offset}(limit 1..100)。只读:不触发执行。"
        ),
    )
    async def _list(
        request_id: str | None = None,
        status: str | None = None,
        dataset: str | None = None,
        requested_by: str | None = None,
        limit: int = 20,
        offset: int = 0,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_backfill_list(
            app_context(ctx),
            request_id=request_id,
            status=status,
            dataset=dataset,
            requested_by=requested_by,
            limit=limit,
            offset=offset,
        )

    @mcp.tool(
        name="finboard_data_backfill_cancel",
        description=(
            "写:协作式取消补数请求(需写权限)。job 走既有取消语义;未完成 "
            "item 标 cancelled;已成功 item 的发布**不回滚**(发布不可变)。"
            "返回 {request_id,status,job_id,cancel_requested}。未终态前重复"
            "取消幂等;终态(含 partial)拒绝。"
        ),
    )
    async def _cancel(
        request_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await data_backfill_cancel(app_context(ctx), request_id=request_id)


__all__ = [
    "MAX_PAGE_LIMIT",
    "data_backfill_cancel",
    "data_backfill_list",
    "data_backfill_preview",
    "data_backfill_submit",
    "register",
]
