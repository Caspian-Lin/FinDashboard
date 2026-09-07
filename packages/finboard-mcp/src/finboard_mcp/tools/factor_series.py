"""``finboard.factor_series.*`` 工具 —— 因子序列构建入队与查询(issue #360)。

内容寻址因子序列(FS- 前缀,``research_factor_series``)的两个工具:

* ``finboard_factor_series_build``(写)—— 入队 ``kind=factor_series_build``
  后台任务(worker 单并发);入队前同步做缓存检查:``series_key``(由
  resolved commit x bars 主发布 x 研究发布联合集 x params x 窗口 内容寻址)
  已存在且 ``content_checksum`` 一致 → 直接返回 ``unchanged``,不创建 job。
* ``finboard_factor_series_get``(只读)—— ``view=summary|detail`` 默认
  summary(#206 瘦身先例:头部 + 覆盖统计,不含逐日 values;detail 才给
  dates + values 全量)。

换 bars 发布的托管批量重建 = 一次入队 N 个 build job(内容寻址缓存使未受
影响的输入组合自动 unchanged);入队/validate 的失配守卫见
``finboard_backtest.research_code.factor_series``。

权限:build 是研究写操作(agent 可自主执行,#122 先例),受
``mcp_readonly_only`` 守卫;get 只读。纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_backtest.research_code import is_promoted_artifact, promotion_status
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence import (
    BackgroundJobRepository,
    FactorSeriesRepository,
    ResearchCodeArtifactRepository,
    ResearchDatasetReleaseRepository,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

_AGENT_ACTOR = "agent:mcp"
_USER_FACTOR_KIND = "factor"


async def _require_write_enabled(app: McpAppContext) -> None:
    if not app.write_tools_enabled:
        raise McpToolError(
            "permission_denied",
            "MCP server 处于只读模式(mcp_readonly_only=true),写操作不可用",
        )


def _parse_date_field(raw: str, field: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise McpToolError(
            "invalid_argument", f"{field} 不是合法 ISO 日期: {raw!r}"
        ) from exc


def _payload_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _series_summary(record: Any) -> dict[str, Any]:
    """series 头部投影(#206 瘦身:不含逐日 values)。"""
    symbols = sorted(
        {symbol for day in record.values.values() for symbol in day}
    )
    return cast(
        dict[str, Any],
        to_jsonable(
            {
                "series_id": record.series_id,
                "series_key": record.series_key,
                "code_artifact": record.code_artifact,
                "code_commit": record.code_commit,
                "kind": record.kind,
                "factor_name": f"u_{record.code_artifact}",
                "release_id": record.release_id,
                "dataset_release_ids": list(record.dataset_release_ids),
                "params": record.params,
                "window_start": record.window_start,
                "window_end": record.window_end,
                "date_count": len(record.dates),
                "symbol_count": len(symbols),
                "content_checksum": record.content_checksum,
                "quality": record.quality,
                "source_run_id": record.source_run_id,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            }
        ),
    )


async def build_enqueue(
    app: McpAppContext,
    *,
    name: str,
    release_id: str,
    window_start: str,
    window_end: str,
    dataset_release_ids: list[str] | None = None,
    commit: str | None = None,
    artifact_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> ToolEnvelope:
    """预检 + 缓存检查通过后入队 ``kind=factor_series_build``。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        settings = app.settings
        if not getattr(settings, "research_sandbox_enabled", False):
            raise McpToolError(
                "invalid_argument",
                "研究沙箱未启用(research_sandbox_enabled=false);"
                "启用前置:Docker Desktop 运行 + 构建镜像 docker/research-sandbox",
            )
        start = _parse_date_field(window_start, "window_start")
        end = _parse_date_field(window_end, "window_end")
        if end < start:
            raise McpToolError(
                "invalid_argument", "window_end 不得早于 window_start"
            )
        joint = sorted(set(dataset_release_ids or []))
        if release_id in joint:
            raise McpToolError(
                "invalid_argument",
                "dataset_release_ids 是研究发布联合集,bars 主发布 "
                f"release_id({release_id})不应重复出现在其中",
            )

        async with app.session_maker() as session:
            artifact_repo = ResearchCodeArtifactRepository(session)
            if artifact_id is not None:
                artifact = await artifact_repo.get(artifact_id)
                if artifact is None:
                    raise McpToolError(
                        "not_found", f"研究代码产物不存在: {artifact_id}"
                    )
                if artifact.kind != _USER_FACTOR_KIND or artifact.name != name:
                    raise McpToolError(
                        "invalid_argument",
                        f"artifact_id 与 (factor,{name}) 不一致: {artifact_id}",
                    )
                if artifact.status == "retired":
                    raise McpToolError(
                        "invalid_argument",
                        f"artifact 已 retired,不能构建新序列: {artifact_id}",
                    )
                if artifact.status == "active" and not is_promoted_artifact(artifact):
                    raise McpToolError(
                        "invalid_argument",
                        f"artifact active 但未通过 screen+OOS 晋级门: {artifact_id}"
                        f" promotion_status={promotion_status(artifact)}",
                    )
            else:
                artifact = await artifact_repo.get_active(
                    kind=_USER_FACTOR_KIND, name=name
                )
                if artifact is None:
                    raise McpToolError(
                        "not_found",
                        f"没有 active+passed 的因子研究代码: name={name}"
                        "(先 finboard_research_code_submit 并完成晋级)",
                    )
                if not is_promoted_artifact(artifact):
                    raise McpToolError(
                        "invalid_argument",
                        f"artifact 未通过 screen+OOS 晋级门: {artifact.artifact_id}"
                        f" promotion_status={promotion_status(artifact)}",
                    )
            if commit is not None and commit != artifact.commit:
                raise McpToolError(
                    "invalid_argument",
                    f"指定 commit {commit[:12]} 不是该 artifact 的 active 引用"
                    f"(artifact={artifact.commit[:12]});历史版本先 "
                    "finboard_research_code_rollback",
                )
            resolved_commit = commit or artifact.commit

            release_repo = ResearchDatasetReleaseRepository(session)
            if await release_repo.get(release_id) is None:
                raise McpToolError("not_found", f"研究数据发布不存在: {release_id}")
            for joint_id in joint:
                if await release_repo.get(joint_id) is None:
                    raise McpToolError(
                        "not_found", f"研究数据发布不存在: {joint_id}"
                    )

            # 缓存检查:series_key 已存在且 checksum 一致 → unchanged,不建 job。
            series_repo = FactorSeriesRepository(session)
            cached = await series_repo.find_matching(
                code_artifact=name,
                release_id=release_id,
                dataset_release_ids=joint,
                params=params or {},
                window_start=start,
                window_end=end,
            )
            if cached is not None:
                return cast(
                    dict[str, Any],
                    to_jsonable(
                        {
                            "unchanged": True,
                            "series_id": cached.series_id,
                            "series_key": cached.series_key,
                            "content_checksum": cached.content_checksum,
                            "message": (
                                "series_key 已存在且 content_checksum 一致"
                                "(内容寻址缓存命中),未创建构建任务"
                            ),
                        }
                    ),
                )

            payload: dict[str, Any] = {
                "kind": _USER_FACTOR_KIND,
                "name": name,
                "commit": resolved_commit,
                "release_id": release_id,
                "dataset_release_ids": joint,
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
            }
            if artifact_id is not None:
                payload["artifact_id"] = artifact_id
            if params is not None:
                payload["params"] = params

            idempotency_key = f"factor_series_build:{_payload_checksum(payload)[:16]}"
            from sqlalchemy.exc import IntegrityError

            try:
                row, created = await BackgroundJobRepository(session).create_or_get(
                    job_id=generate_background_job_id(),
                    idempotency_key=idempotency_key,
                    kind="factor_series_build",
                    queue="default",
                    status=BackgroundJobStatus.QUEUED.value,
                    priority=0,
                    payload=payload,
                    payload_checksum=_payload_checksum(payload),
                    max_attempts=1,
                    requested_by=_AGENT_ACTOR,
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise McpToolError("conflict", "重复 idempotency_key") from exc
        return cast(
            dict[str, Any],
            to_jsonable(
                {
                    "job_id": row.job_id,
                    "kind": "factor_series_build",
                    "status": row.status,
                    "created": created,
                    "idempotency_key": idempotency_key,
                    "unchanged": False,
                    "detail_hint": (
                        "finboard_job_get 轮询执行进度(成功 result_ref=FS-...;"
                        "error_summary 含 cache_hit 表示缓存命中);终态后 "
                        "finboard_factor_series_get 取序列内容"
                    ),
                }
            ),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.factor_series.build",
        arguments={
            "name": name,
            "release_id": release_id,
            "window_start": window_start,
            "window_end": window_end,
            "dataset_release_ids": dataset_release_ids,
            "commit": commit,
            "artifact_id": artifact_id,
        },
        handler=_do,
    )


async def series_get(
    app: McpAppContext,
    series_id: str,
    *,
    view: str = "summary",
) -> ToolEnvelope:
    """查询因子序列;detail 附 dates + 逐日 values 全量。"""

    async def _do() -> dict[str, Any]:
        if view not in ("summary", "detail"):
            raise McpToolError("invalid_argument", f"未知视图: {view}")
        async with app.session_maker() as session:
            record = await FactorSeriesRepository(session).get(series_id)
        if record is None:
            raise McpToolError("not_found", f"因子序列不存在: {series_id}")
        data = _series_summary(record)
        data["view"] = view
        if view == "detail":
            data["dates"] = [item.isoformat() for item in record.dates]
            data["values"] = record.values
        return data

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.factor_series.get",
        arguments={"series_id": series_id, "view": view},
        handler=_do,
    )


def register(mcp: MCPServer) -> None:
    """把因子序列工具注册到 MCP server。"""

    @mcp.tool(
        name="finboard_factor_series_build",
        description=(
            "入队因子序列构建(kind=factor_series_build 后台任务,worker 单并发):"
            "已晋级 active 因子代码按窗口逐决策日在沙箱容器构建内容寻址序列"
            "(series_key = sha256(commit|bars 主发布|研究发布联合集|params|窗口)),"
            "窗口内逐决策日截面 values + 质量归档冻结进 research_factor_series"
            "(FS- 前缀),供 research run 的 factor_series_ids 引用(u_ 因子按"
            "决策日索引,multi_period 可用)。入队预检:sandbox 开启、"
            "(factor,name) 有 active 产物(显式 artifact_id 也可,但未通过晋级门"
            "拒绝)、release 均已登记。缓存命中(series_key 已存在且 content_checksum"
            " 一致)直接返回 unchanged,不创建任务。构建完成后抽 2 个截断点做前缀"
            "不变性审计,检出前视即 failed=lookahead_detected。换 bars 发布的托管"
            "批量重建 = 对每个失效序列逐条调用本工具(内容寻址缓存使未受影响的"
            "组合自动 unchanged)。失败分类:sandbox_disabled / static 类 / "
            "runtime_error / timeout / oom_killed / output_contract_violation / "
            "lookahead_detected / quality_gate_failed。返回 job_id,"
            "finboard_job_get 轮询(result_ref=FS-...),终态后 "
            "finboard_factor_series_get 取内容。纯离线研究域,不连 broker 不下单。"
        ),
    )
    async def _build(
        name: str,
        release_id: str,
        window_start: str,
        window_end: str,
        dataset_release_ids: list[str] | None = None,
        commit: str | None = None,
        artifact_id: str | None = None,
        params: dict[str, Any] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await build_enqueue(
            app_context(ctx),
            name=name,
            release_id=release_id,
            window_start=window_start,
            window_end=window_end,
            dataset_release_ids=dataset_release_ids,
            commit=commit,
            artifact_id=artifact_id,
            params=params,
        )

    @mcp.tool(
        name="finboard_factor_series_get",
        description=(
            "查询内容寻址因子序列(research_factor_series,FS- 前缀):三向审计引用"
            "(code_artifact/commit/kind)、bars 主发布锚定与研究发布联合集、窗口、"
            "decision 日数与标的数、content_checksum、质量门归档(quality)。"
            "view=summary 默认(#206 瘦身,不含逐日 values);view=detail 附 dates"
            " 与逐日 values({date:{symbol:float|null}})全量。该序列可被 research"
            " run 入队 payload 的 factor_series_ids 引用(换发布失配将被入队秒拒,"
            "见 series_release_mismatch)。"
        ),
    )
    async def _get(
        series_id: str,
        view: str = "summary",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> ToolEnvelope:
        return await series_get(app_context(ctx), series_id, view=view)


__all__ = ["build_enqueue", "register", "series_get"]
