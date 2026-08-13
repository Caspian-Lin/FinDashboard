"""``finboard.job.*`` 工具 —— 统一后台任务队列监控与提交(issue #136)。

把 #117/#142/#143/#144 建立的持久化 ``background_jobs`` 队列以受控 MCP 工具形式
暴露给外置 Agent(OpenCode):

* 只读(2):list / get —— 直接调 :class:`~finboard_persistence.BackgroundJobRepository`;
* 写(2):enqueue / cancel —— 受 ``_require_write_enabled`` 守卫。

``enqueue`` 复用 ``JobIn`` schema 校验(kind / queue / idempotency_key / payload /
priority / max_attempts / requested_by),kind 白名单只放研究 / 数据 / 回测域
(``_ALLOWED_KINDS``),与 REST ``POST /api/jobs`` 的边界一致;实盘交易内核
(盘前检查 / 收盘撤单 / 日终核对 / Broker 心跳 / Kill Switch)由专用 Scheduler
执行,**不进入**统一队列、不暴露为 MCP 工具。

权限:写操作尊重 ``settings.mcp_readonly_only`` / ``app.write_tools_enabled``
开关;只读工具自动允许。不触及交易安全红线(不连 broker / 账户 / 订单 / 持仓)。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence.background_job_repo import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

#: ``enqueue`` 允许直接提交的 kind 白名单(全部是研究 / 数据 / 回测域,
#: 不含实盘交易内核任务)。与 REST ``POST /api/jobs`` 的边界一致:
#: 业务 kind 走语义化端点,但 MCP 作为研究 Agent 的统一入口,
#: 放开到所有已注册的研究 / 数据域 kind。实盘 Scheduler 任务不在此列。
_ALLOWED_KINDS: frozenset[str] = frozenset(
    {
        "echo",
        "research_run",
        "feature_snapshot",
        "bulk_download",
        "dataset_publish",
        "backtest_run",
        "data_sync",
        "fetch_all",
        "quality_repair",
    }
)


async def _require_write_enabled(app: McpAppContext) -> None:
    """写操作前置检查:``mcp_readonly_only`` 开启时拒绝。"""

    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _job_out(row: Any) -> dict[str, Any]:
    """把 ``BackgroundJobModel`` 行序列化为 ``JobOut`` 兼容的 JSON dict。

    与 REST ``JobOut.model_validate(row)`` 口径一致(含时间戳),
    再经 ``to_jsonable`` 归一为 JSON 兼容结构。
    """

    from finboard_api.job_schemas import JobOut

    return cast(
        dict[str, Any],
        to_jsonable(JobOut.model_validate(row).model_dump(mode="json")),
    )


def _payload_checksum(payload: dict[str, Any]) -> str:
    """计算 background_jobs payload checksum(与 routes/jobs.py 口径一致)。"""

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# 只读:list / get
# --------------------------------------------------------------------------- #


async def job_list(
    app: McpAppContext,
    *,
    kind: list[str] | None = None,
    status: list[str] | None = None,
    queue: list[str] | None = None,
    limit: int = 100,
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        valid = {item.value for item in BackgroundJobStatus}
        if status is not None and not set(status).issubset(valid):
            raise McpToolError("invalid_argument", f"未知 job 状态: {status}")
        safe_limit = max(1, min(500, limit))
        async with app.session_maker() as session:
            rows = await BackgroundJobRepository(session).list_recent(
                kinds=kind,
                statuses=status,
                queues=queue,
                limit=safe_limit,
            )
            return [_job_out(row) for row in rows]

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.job.list",
        arguments={
            "kind": kind,
            "status": status,
            "queue": queue,
            "limit": limit,
        },
        handler=_do,
    )


async def job_get(app: McpAppContext, job_id: str) -> ToolEnvelope:
    async def _do() -> dict[str, Any]:
        async with app.session_maker() as session:
            row = await BackgroundJobRepository(session).get(job_id)
            if row is None:
                raise McpToolError("not_found", f"后台任务不存在: {job_id}")
            return _job_out(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.job.get",
        arguments={"job_id": job_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# 写:enqueue / cancel
# --------------------------------------------------------------------------- #


async def job_enqueue(
    app: McpAppContext,
    *,
    kind: str,
    idempotency_key: str,
    requested_by: str,
    queue: str = "default",
    payload: dict[str, Any] | None = None,
    priority: int = 0,
    max_attempts: int = 3,
) -> ToolEnvelope:
    """登记一个 queued 后台任务并立即返回 202 + job_id(不等待执行)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        from sqlalchemy.exc import IntegrityError

        from finboard_api.job_schemas import JobIn

        body = JobIn(
            kind=kind,
            queue=queue,
            idempotency_key=idempotency_key,
            payload=payload or {},
            priority=priority,
            max_attempts=max_attempts,
            requested_by=requested_by,
        )
        if body.kind not in _ALLOWED_KINDS:
            raise McpToolError(
                "invalid_argument",
                f"未开放 kind: {body.kind}"
                f"(允许 {sorted(_ALLOWED_KINDS)};实盘交易内核任务不进入队列)",
            )
        checksum = _payload_checksum(body.payload)
        async with app.session_maker() as session:
            try:
                row, created = await BackgroundJobRepository(
                    session
                ).create_or_get(
                    job_id=generate_background_job_id(),
                    idempotency_key=body.idempotency_key,
                    kind=body.kind,
                    queue=body.queue,
                    status=BackgroundJobStatus.QUEUED.value,
                    priority=body.priority,
                    payload=body.payload,
                    payload_checksum=checksum,
                    max_attempts=body.max_attempts,
                    requested_by=body.requested_by,
                )
                await session.commit()
            except BackgroundJobPersistenceConflictError as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            except IntegrityError as exc:
                await session.rollback()
                raise McpToolError("conflict", "重复 idempotency_key") from exc
            result = _job_out(row)
            # 幂等命中时在结果里标记,便于 agent 区分「首次提交」与「已存在」。
            result["created"] = created
            return result

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.job.enqueue",
        arguments={
            "kind": kind,
            "idempotency_key": idempotency_key,
            "requested_by": requested_by,
        },
        handler=_do,
        idempotency_key=idempotency_key,
    )


async def job_cancel(
    app: McpAppContext, job_id: str
) -> ToolEnvelope:
    """请求协作式取消(running → cancel_requested);终态任务返回当前状态不报错。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            repo = BackgroundJobRepository(session)
            row = await repo.get(job_id)
            if row is None:
                raise McpToolError("not_found", f"后台任务不存在: {job_id}")
            try:
                await repo.request_cancel(job_id)
                await session.commit()
            except BackgroundJobPersistenceConflictError:
                await session.rollback()
                # 不在 running(可能已排队 / 已终态)——返回当前状态给调用方判断。
            refreshed = await repo.get(job_id)
            assert refreshed is not None
            return _job_out(refreshed)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.job.cancel",
        arguments={"job_id": job_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #


def register(mcp: MCPServer) -> None:
    """把任务队列工具注册到 MCP server(2 只读 + 2 写)。"""

    @mcp.tool(
        name="finboard_job_list",
        description=(
            "列出后台任务(最近优先,可选按 kind/status/queue 过滤,默认 100 条)。"
            "返回 JobOut 列表(job_id/kind/queue/status/priority/payload/"
            "progress_done/progress_total/phase/result_ref/error_*/attempt/"
            "max_attempts/worker_id/时间戳)。"
            "status 取值:queued|running|retry_waiting|succeeded|failed|"
            "cancel_requested|cancelled|interrupted。只读。"
        ),
    )
    async def _list(
        kind: list[str] | None = None,
        status: list[str] | None = None,
        queue: list[str] | None = None,
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await job_list(
            app_context(ctx),
            kind=kind,
            status=status,
            queue=queue,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_job_get",
        description=(
            "查询单个后台任务详情。返回 JobOut(含完整进度 / 错误 / 时间戳)。"
            "成功后 result_ref 携带产物引用(如特征快照的 snapshot_id)。"
            "未找到返回 not_found。只读。"
        ),
    )
    async def _get(job_id: str, ctx: Context = None) -> ToolEnvelope:  # type: ignore[assignment]
        return await job_get(app_context(ctx), job_id)

    @mcp.tool(
        name="finboard_job_enqueue",
        description=(
            "[写] 登记一个 queued 后台任务并立即返回 202 + job_id(不等待执行,"
            "由独立 worker 进程消费)。"
            "参数:kind(白名单:echo/research_run/feature_snapshot/bulk_download/"
            "dataset_publish/backtest_run/data_sync/fetch_all/quality_repair)、"
            "queue(默认 default)、idempotency_key(8-128 字符,幂等键)、"
            "payload(任务参数,具体结构取决于 kind)、priority(-1000..1000,默认 0)、"
            "max_attempts(1..10,默认 3)、requested_by。"
            "返回 JobOut + created(首次提交 true / 幂等命中 false)。"
            "实盘交易内核任务不进入队列。写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _enqueue(
        kind: str,
        idempotency_key: str,
        requested_by: str,
        queue: str = "default",
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await job_enqueue(
            app_context(ctx),
            kind=kind,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            queue=queue,
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
        )

    @mcp.tool(
        name="finboard_job_cancel",
        description=(
            "[写] 请求协作式取消后台任务(running → cancel_requested,"
            "executor checkpoint 时退出)。"
            "已在终态(succeeded/failed/cancelled/interrupted)的任务返回当前"
            "状态不报错。未找到返回 not_found。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _cancel(
        job_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await job_cancel(app_context(ctx), job_id)


__all__ = [
    "job_cancel",
    "job_enqueue",
    "job_get",
    "job_list",
    "register",
]
