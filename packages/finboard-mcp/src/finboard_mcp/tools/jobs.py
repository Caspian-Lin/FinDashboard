"""``finboard.job.*`` 工具 —— 统一后台任务队列监控与提交(issue #136;#221 归档)。

把 #117/#142/#143/#144 建立的持久化 ``background_jobs`` 队列以受控 MCP 工具形式
暴露给外置 Agent(OpenCode):

* 只读(2):list / get —— 直接调 :class:`~finboard_persistence.BackgroundJobRepository`;
* 写(4):enqueue / cancel / archive / unarchive —— 受 ``_require_write_enabled`` 守卫。

``enqueue`` 复用 ``JobIn`` schema 校验(kind / queue / idempotency_key / payload /
priority / max_attempts / requested_by),kind 白名单只放研究 / 数据 / 回测域
(``_ALLOWED_KINDS``),与 REST ``POST /api/jobs`` 的边界一致;实盘交易内核
(盘前检查 / 收盘撤单 / 日终核对 / Broker 心跳 / Kill Switch)由专用 Scheduler
执行,**不进入**统一队列、不暴露为 MCP 工具。已注册 kind 的 payload 契约在
入队期校验(#260,与 REST 共用 ``validate_job_payload``):未知键 / 缺必填 /
枚举非法秒级 ``invalid_argument``。

归档(issue #221)只是 ``background_jobs`` 的展示维度:归档后从默认列表
(``archived=exclude``)隐藏但**不删除**,``archived=only|all`` 与 get 始终可达,
可取消归档;仅终态任务可归档,归档后 worker 维护路径不再触碰(冻结)。

权限:写操作尊重 ``settings.mcp_readonly_only`` / ``app.write_tools_enabled``
开关;只读工具自动允许。不触及交易安全红线(不连 broker / 账户 / 订单 / 持仓)。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_backtest.background_jobs.payload_contracts import (
    PayloadContractError,
    validate_job_payload,
)
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence.background_job_repo import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
)
from finboard_shared.background_jobs import (
    ARCHIVE_FILTER_VALUES,
    TERMINAL_STATUSES,
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
        "research_data_sync",
        "research_code_run",
        "validation_experiment",
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


#: ``job_get(view=none)`` 的轮询最小字段集(issue #206 P2;#306 增 run_status)。
_JOB_POLL_FIELDS: tuple[str, ...] = (
    "job_id",
    "kind",
    "status",
    "phase",
    "progress_done",
    "progress_total",
    "result_ref",
    "error_code",
    "error_summary",
    "attempt",
    "updated_at",
)

#: 参与 ``data_hash`` 的状态字段(issue #206 P3:状态未变 → unchanged 短路;
#: #306 增 run_status —— run 侧状态翻转同样视为状态变化,不再误报 unchanged)。
_JOB_HASH_FIELDS: tuple[str, ...] = (
    "status",
    "phase",
    "progress_done",
    "progress_total",
    "result_ref",
    "error_code",
    "error_summary",
    "attempt",
)


def _job_state_hash(row: Any, *, run_status: str | None = None) -> str:
    """对轮询关心的状态字段计算 sha256,同状态同 hash(不含 payload/时间戳)。"""

    material = json.dumps(
        {
            **{key: to_jsonable(getattr(row, key, None)) for key in _JOB_HASH_FIELDS},
            "run_status": run_status,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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
    archived: str = "exclude",
) -> ToolEnvelope:
    async def _do() -> list[dict[str, Any]]:
        valid = {item.value for item in BackgroundJobStatus}
        if status is not None and not set(status).issubset(valid):
            raise McpToolError("invalid_argument", f"未知 job 状态: {status}")
        if archived not in ARCHIVE_FILTER_VALUES:
            raise McpToolError(
                "invalid_argument",
                f"未知归档过滤值: {archived}(合法 {sorted(ARCHIVE_FILTER_VALUES)})",
            )
        safe_limit = max(1, min(500, limit))
        async with app.session_maker() as session:
            rows = await BackgroundJobRepository(session).list_recent(
                kinds=kind,
                statuses=status,
                queues=queue,
                limit=safe_limit,
                archived=archived,
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
            "archived": archived,
        },
        handler=_do,
    )


async def job_get(
    app: McpAppContext,
    job_id: str,
    *,
    view: str = "summary",
    data_hash: str | None = None,
) -> ToolEnvelope:
    """查询单个后台任务(issue #206:view 三档 + data_hash 幂等短路)。

    * ``view=none``:轮询最小字段集(status/progress/result_ref/error);
    * ``view=summary``(默认):JobOut 全字段,剥离 payload;
    * ``view=detail``:完整 JobOut(含 payload,诊断用)。
    * ``data_hash``:上次返回携带的状态指纹;命中(状态未变)返回
      ``{unchanged: true, data_hash, status}`` 而非重发全量(P3)。
    """

    async def _do() -> dict[str, Any]:
        if view not in ("none", "summary", "detail"):
            raise McpToolError(
                "invalid_argument", f"未知视图: {view}(none|summary|detail)"
            )
        async with app.session_maker() as session:
            row = await BackgroundJobRepository(session).get(job_id)
            if row is None:
                raise McpToolError("not_found", f"后台任务不存在: {job_id}")
            # issue #306:kind=research_run 时透传关联 research_runs 状态,
            # 「run interrupted 但 job 仍 running」的两表不一致一眼可见。
            run_status: str | None = None
            if row.kind == "research_run":
                from finboard_persistence import ResearchRunRepository

                run_status = await ResearchRunRepository(session).get_status_by_job_id(
                    job_id
                )
        current_hash = _job_state_hash(row, run_status=run_status)
        if data_hash is not None and data_hash == current_hash:
            return {
                "job_id": job_id,
                "unchanged": True,
                "data_hash": current_hash,
                "status": row.status,
                "run_status": run_status,
                "view": view,
            }
        payload_dict = _job_out(row)
        if view == "none":
            out = {
                key: payload_dict[key]
                for key in _JOB_POLL_FIELDS
                if key in payload_dict
            }
        elif view == "summary":
            out = {key: item for key, item in payload_dict.items() if key != "payload"}
        else:
            out = payload_dict
        out["run_status"] = run_status
        out["data_hash"] = current_hash
        return out

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.job.get",
        arguments={"job_id": job_id, "view": view, "data_hash": data_hash},
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
        # per-kind payload 入队期契约(#260,与 REST POST /api/jobs 共用同一
        # 校验器):未知键 / 缺必填 / 枚举非法秒级拒绝,不再等 worker 执行期。
        try:
            validate_job_payload(body.kind, body.payload)
        except PayloadContractError as exc:
            raise McpToolError(
                "invalid_argument",
                f"payload 契约校验失败[{exc.code}]: {exc.summary}",
            ) from exc
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
# 写:archive / unarchive(issue #221)
# --------------------------------------------------------------------------- #


def _parse_finished_before(value: str | None) -> datetime | None:
    """把 MCP 入参的 ISO 时间串解析为 aware datetime;非法值报 invalid_argument。"""

    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise McpToolError(
            "invalid_argument", f"finished_before 不是合法 ISO 时间: {value}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def job_archive(
    app: McpAppContext,
    *,
    job_id: str | None = None,
    kinds: list[str] | None = None,
    statuses: list[str] | None = None,
    queues: list[str] | None = None,
    finished_before: str | None = None,
    limit: int = 100,
) -> ToolEnvelope:
    """归档后台任务(issue #221):从默认列表隐藏但**不删除**,可取消归档。

    * 单个:传 ``job_id``(幂等,已归档原样返回);
    * 批量:省略 ``job_id``,按 ``kinds`` / ``statuses`` / ``queues`` /
      ``finished_before``(ISO 时间)过滤,从旧到新归档 ``limit`` 条,返回
      ``archived_count`` 计数(#206 精神:不回全量任务列表)。
    仅终态任务可归档;``statuses`` 只接受终态子集(空 = 全部终态)。
    """

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        finished_at = _parse_finished_before(finished_before)
        async with app.session_maker() as session:
            repo = BackgroundJobRepository(session)
            if job_id is not None:
                if kinds or statuses or queues or finished_before is not None:
                    raise McpToolError(
                        "invalid_argument",
                        "job_id 与批量过滤参数(kinds/statuses/queues/"
                        "finished_before)互斥,二选一",
                    )
                try:
                    row = await repo.archive(job_id)
                    await session.commit()
                except BackgroundJobPersistenceConflictError as exc:
                    await session.rollback()
                    message = str(exc)
                    if "不存在" in message:
                        raise McpToolError("not_found", message) from exc
                    raise McpToolError("conflict", message) from exc
                return _job_out(row)
            if statuses is not None and not set(statuses).issubset(TERMINAL_STATUSES):
                raise McpToolError(
                    "invalid_argument",
                    f"仅终态任务可归档(合法 {sorted(TERMINAL_STATUSES)})",
                )
            try:
                count = await repo.archive_bulk(
                    kinds=kinds,
                    statuses=statuses,
                    queues=queues,
                    finished_before=finished_at,
                    limit=limit,
                )
                await session.commit()
            except BackgroundJobPersistenceConflictError as exc:
                await session.rollback()
                raise McpToolError("conflict", str(exc)) from exc
            return {"archived_count": count}

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.job.archive",
        arguments={
            "job_id": job_id,
            "kinds": kinds,
            "statuses": statuses,
            "queues": queues,
            "finished_before": finished_before,
            "limit": limit,
        },
        handler=_do,
    )


async def job_unarchive(app: McpAppContext, job_id: str) -> ToolEnvelope:
    """取消归档:任务重新出现在默认列表;幂等(未归档原样返回)。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        async with app.session_maker() as session:
            repo = BackgroundJobRepository(session)
            try:
                row = await repo.unarchive(job_id)
                await session.commit()
            except BackgroundJobPersistenceConflictError as exc:
                await session.rollback()
                raise McpToolError("not_found", str(exc)) from exc
            return _job_out(row)

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.job.unarchive",
        arguments={"job_id": job_id},
        handler=_do,
    )


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #


def register(mcp: MCPServer) -> None:
    """把任务队列工具注册到 MCP server(2 只读 + 4 写)。"""

    @mcp.tool(
        name="finboard_job_list",
        description=(
            "列出后台任务(最近优先,可选按 kind/status/queue 过滤,默认 100 条)。"
            "archived=exclude(默认)只看未归档;only 只看已归档;all 不区分"
            "(issue #221:归档隐藏不删除,单查 finboard_job_get 始终可达)。"
            "返回 JobOut 列表(job_id/kind/queue/status/priority/payload/"
            "progress_done/progress_total/phase/result_ref/error_*/attempt/"
            "max_attempts/worker_id/archived_at/时间戳)。"
            "status 取值:queued|running|retry_waiting|succeeded|failed|"
            "cancel_requested|cancelled|interrupted。只读。"
        ),
    )
    async def _list(
        kind: list[str] | None = None,
        status: list[str] | None = None,
        queue: list[str] | None = None,
        limit: int = 100,
        archived: str = "exclude",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await job_list(
            app_context(ctx),
            kind=kind,
            status=status,
            queue=queue,
            limit=limit,
            archived=archived,
        )

    @mcp.tool(
        name="finboard_job_get",
        description=(
            "查询单个后台任务详情。view=summary(默认):JobOut 全字段但剥离 "
            "payload(issue #206);view=none:轮询最小集(status/phase/progress_*/"
            "result_ref/error_*/attempt);view=detail:完整 JobOut 含 payload"
            "(诊断用)。返回附 data_hash(状态指纹,含 run_status):轮询时把上次 "
            "data_hash 传回,状态未变则返回 {unchanged: true, data_hash, status,"
            " run_status} 而非重发全量。kind=research_run 的任务附带 run_status"
            " 字段(issue #306:关联 research_runs.status,查不到为 null)——"
            "「run interrupted 但 job 仍 running」的两表不一致一眼可见。"
            "research_run 的 phase 携带实时进度(issue #308):加载期为"
            " `research_run:decision_load k/N`(k=已完成期数,N=决策期总数,"
            "multi_period/single_shot 同机制),决策执行期为"
            " `research_run:<stage>#<序号>@<YYYY-MM-DD>`(序号 1-based),"
            "REPORT/终态保持 `research_run:report` / `research_run:<status>`;"
            "done/total 数值恒为「stage x decision」工件计数(issue #188 口径)。"
            "成功后 result_ref 携带产物引用(如特征快照的 snapshot_id)。"
            "未找到返回 not_found。只读。"
        ),
    )
    async def _get(
        job_id: str,
        view: str = "summary",
        data_hash: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await job_get(
            app_context(ctx), job_id, view=view, data_hash=data_hash
        )

    @mcp.tool(
        name="finboard_job_enqueue",
        description=(
            "[写] 登记一个 queued 后台任务并立即返回 202 + job_id(不等待执行,"
            "由独立 worker 进程消费)。"
            "参数:kind(白名单:echo/research_run/feature_snapshot/bulk_download/"
            "dataset_publish/backtest_run/data_sync/fetch_all/quality_repair/"
            "research_data_sync/research_code_run/validation_experiment),"
            "queue(默认 default)、idempotency_key(8-128 字符,幂等键)、"
            "payload(任务参数,具体结构取决于 kind)、priority(-1000..1000,默认 0)、"
            "max_attempts(1..10,默认 3)、requested_by。"
            "research_data_sync payload 模板(#260 起入队期契约校验,违规秒级 "
            "invalid_argument):{start_date: 'YYYY-MM-DD'(必填), "
            "end_date: 'YYYY-MM-DD'(必填), datasets: ['profiles'|'name_changes'|"
            "'convertible_profiles'|'daily_metrics'|'financial_indicators'|"
            "'industry_memberships']"
            "(可选,缺省=全部六类;convertible_profiles=#265 转债条款快照,"
            "tushare cb_basic→convertible_metadata + akshare 评级/强赎兜底), "
            "symbols: ['000001.SZ', ...](可选,字符串列表;"
            "省略时逐标的数据集以 profiles 同步结果为 symbol 池,此时 datasets "
            "须含 profiles,否则入队即拒)}。未知键(如误把 datasets 写成 "
            "data_types)入队即拒,不会被静默忽略。"
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

    @mcp.tool(
        name="finboard_job_archive",
        description=(
            "[写] 归档后台任务(issue #221):从默认列表(archived=exclude)隐藏"
            "但**不删除**,finboard_job_get 单查与 archived=only|all 列表始终可达,"
            "finboard_job_unarchive 可恢复。"
            "两种用法:(1) 传 job_id 归档单个任务(幂等,返回 JobOut);"
            "(2) 省略 job_id 批量归档——按 kinds/statuses/queues/finished_before"
            "(ISO 时间)过滤终态任务,从旧到新归档 limit(1..1000,默认 100)条,"
            "返回 {archived_count} 计数(不回全量列表)。"
            "仅终态(succeeded/failed/cancelled/interrupted)任务可归档,"
            "statuses 只接受终态子集(空=全部终态);归档即冻结,"
            "worker 不再自动重排该任务。未找到返回 not_found,"
            "非终态返回 conflict。写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _archive(
        job_id: str | None = None,
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
        queues: list[str] | None = None,
        finished_before: str | None = None,
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await job_archive(
            app_context(ctx),
            job_id=job_id,
            kinds=kinds,
            statuses=statuses,
            queues=queues,
            finished_before=finished_before,
            limit=limit,
        )

    @mcp.tool(
        name="finboard_job_unarchive",
        description=(
            "[写] 取消归档单个后台任务:任务重新出现在默认列表;幂等"
            "(未归档任务原样返回)。未找到返回 not_found。"
            "写操作,mcp_readonly_only=true 时拒绝。"
        ),
    )
    async def _unarchive(
        job_id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await job_unarchive(app_context(ctx), job_id)


__all__ = [
    "job_archive",
    "job_cancel",
    "job_enqueue",
    "job_get",
    "job_list",
    "job_unarchive",
    "register",
]
