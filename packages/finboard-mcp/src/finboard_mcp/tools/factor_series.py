"""``finboard.factor_series.*`` 工具 —— 因子序列构建入队与查询(issue #360;
#398 放行平台预置因子 kind=predefined_factor)。

内容寻址因子序列(FS- 前缀,``research_factor_series``)的两个工具:

* ``finboard_factor_series_build``(写)—— 入队 ``kind=factor_series_build``
  后台任务(worker 并发 2,#375;入队前同步做缓存检查:``series_key``(由
  代码锚 x bars 主发布 x 研究发布联合集 x params x 窗口 内容寻址)
  已存在且 ``content_checksum`` 一致 → 直接返回 ``unchanged``,不创建 job。
  ``kind=factor``(用户沙箱因子)的代码锚 = 已晋级 active 产物的 git
  commit;``kind=predefined_factor``(#398)的代码锚 = 预置因子目录的
  实现版本锚,进程内执行免容器(无 Docker 前置),commit/artifact_id/
  params 不可传。
* ``finboard_factor_series_get``(只读)—— ``view=summary|detail`` 默认
  summary(#206 瘦身先例:头部 + 覆盖统计,不含逐日 values;detail 才给
  dates + values 全量,#458 起估计超 64MB 具名 payload_too_large 拒绝,
  载荷组装挪 asyncio.to_thread 防毒化事件循环)。

换 bars 发布的托管批量重建 = 一次入队 N 个 build job(内容寻址缓存使未受
影响的输入组合自动 unchanged);入队/validate 的失配守卫见
``finboard_backtest.research_code.factor_series``。

权限:build 是研究写操作(agent 可自主执行,#122 先例),受
``mcp_readonly_only`` 守卫;get 只读。纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_backtest.research_code import is_promoted_artifact, promotion_status
from finboard_data.factor_lab import (
    PREDEFINED_FACTOR_KIND,
    USER_FACTOR_KIND,
    series_factor_name,
)
from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolEnvelope
from finboard_mcp.execution import McpToolError, run_tool
from finboard_mcp.tools._serde import to_jsonable
from finboard_persistence import (
    BackgroundJobRepository,
    FactorSeriesRecord,
    FactorSeriesRepository,
    ResearchCodeArtifactRepository,
    ResearchDatasetReleaseRepository,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

_AGENT_ACTOR = "agent:mcp"

#: detail 视图载荷硬上限(issue #458):逐日 values 全量返口的估计字节上限,
#: 超过即具名 ``payload_too_large`` 拒绝(不静默截断);summary 不受影响。
SERIES_DETAIL_MAX_ESTIMATED_BYTES = 64 * 1024 * 1024


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
                "factor_name": series_factor_name(
                    str(record.kind), str(record.code_artifact)
                ),
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
    kind: str = USER_FACTOR_KIND,
) -> ToolEnvelope:
    """预检 + 缓存检查通过后入队 ``kind=factor_series_build``。"""

    async def _do() -> dict[str, Any]:
        await _require_write_enabled(app)
        if kind not in (USER_FACTOR_KIND, PREDEFINED_FACTOR_KIND):
            raise McpToolError(
                "invalid_argument",
                "kind 须为 factor(用户沙箱因子)或 predefined_factor"
                f"(平台预置因子,#398),收到 {kind!r}",
            )
        # 预置因子允许以 p_ 引用名传入,统一归一为目录裸名。
        bare_name = name.removeprefix("p_") if kind == PREDEFINED_FACTOR_KIND else name
        settings = app.settings
        if kind == USER_FACTOR_KIND and not getattr(
            settings, "research_sandbox_enabled", False
        ):
            # predefined 进程内执行无 Docker 前置(#398),沙箱门只锁用户因子。
            raise McpToolError(
                "invalid_argument",
                "研究沙箱未启用(research_sandbox_enabled=false);"
                "启用前置:Docker Desktop 运行 + 构建镜像 docker/research-sandbox",
            )
        if kind == PREDEFINED_FACTOR_KIND and (
            commit is not None or artifact_id is not None or params
        ):
            raise McpToolError(
                "invalid_argument",
                "kind=predefined_factor 不接受 commit/artifact_id/params"
                "(实现版本由预置因子目录锚定;参数化因子按窗口变体展开注册)",
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
            if kind == PREDEFINED_FACTOR_KIND:
                from finboard_backtest.factors.predefined.registry import (
                    is_registered_predefined_factor,
                    predefined_factor_commit,
                    predefined_factor_names,
                )

                if not is_registered_predefined_factor(bare_name):
                    raise McpToolError(
                        "not_found",
                        f"未注册的平台预置因子: {bare_name!r};可用: "
                        f"{list(predefined_factor_names())}",
                    )
                resolved_commit = predefined_factor_commit(bare_name)
            else:
                artifact_repo = ResearchCodeArtifactRepository(session)
                if artifact_id is not None:
                    artifact = await artifact_repo.get(artifact_id)
                    if artifact is None:
                        raise McpToolError(
                            "not_found", f"研究代码产物不存在: {artifact_id}"
                        )
                    if artifact.kind != kind or artifact.name != bare_name:
                        raise McpToolError(
                            "invalid_argument",
                            f"artifact_id 与 ({kind},{bare_name}) 不一致: {artifact_id}",
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
                        kind=kind, name=bare_name
                    )
                    if artifact is None:
                        raise McpToolError(
                            "not_found",
                            f"没有 active+passed 的因子研究代码: name={bare_name}"
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
                code_artifact=bare_name,
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
                "kind": kind,
                "name": bare_name,
                "release_id": release_id,
                "dataset_release_ids": joint,
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
            }
            if kind == USER_FACTOR_KIND:
                # 用户因子 payload 形状零变化(commit 恒在);predefined 的
                # 实现锚由执行器从目录解析,payload 不携带。
                payload["commit"] = resolved_commit
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


def _series_payload(record: FactorSeriesRecord, view: str) -> dict[str, Any]:
    """线程池内组装 series 查询载荷(summary 投影遍历全量 values 求标的集;
    detail 附全量 values,#458)。detail 估计超限时具名拒绝,不静默截断。"""
    data = _series_summary(record)
    data["view"] = view
    if view == "detail":
        estimated = len(str(record.values))
        if estimated > SERIES_DETAIL_MAX_ESTIMATED_BYTES:
            estimated_mb = estimated / (1024 * 1024)
            limit_mb = SERIES_DETAIL_MAX_ESTIMATED_BYTES / (1024 * 1024)
            raise McpToolError(
                "payload_too_large",
                f"因子序列 {record.series_id} 的 detail 逐日 values 估计约 "
                f"{estimated_mb:.1f}MB,超过上限 {limit_mb:.0f}MB,拒绝序列化"
                "(不静默截断)。替代路径:view=summary 看头部与覆盖统计"
                "(date_count/symbol_count/content_checksum);全量消费走 "
                "research_run 的 factor_series_ids 加载通道。",
            )
        data["dates"] = [item.isoformat() for item in record.dates]
        data["values"] = record.values
    return data


async def series_get(
    app: McpAppContext,
    series_id: str,
    *,
    view: str = "summary",
) -> ToolEnvelope:
    """查询因子序列;detail 附 dates + 逐日 values 全量(#458:超限具名拒绝)。

    载荷组装(summary 投影 + detail values)是 O(values) 的同步 CPU 段,
    挪 ``asyncio.to_thread`` 执行,重调用不毒化事件循环。
    """

    async def _do() -> dict[str, Any]:
        if view not in ("summary", "detail"):
            raise McpToolError("invalid_argument", f"未知视图: {view}")
        async with app.session_maker() as session:
            record = await FactorSeriesRepository(session).get(series_id)
        if record is None:
            raise McpToolError("not_found", f"因子序列不存在: {series_id}")
        return await asyncio.to_thread(_series_payload, record, view)

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
            "入队因子序列构建(kind=factor_series_build 后台任务,worker 并发 2):"
            "kind=factor(默认,用户沙箱因子)按窗口在沙箱容器逐决策日构建内容"
            "寻址序列(series_key = sha256(commit|bars 主发布|研究发布联合集|"
            "params|窗口)),kind=predefined_factor(#398,平台预置因子)按目录"
            "实现锚在 worker 进程内构建(免容器,无 Docker 前置;name 须为注册"
            "的目录裸名,如 return_21d/return_63d/return_126d/return_252d,"
            "不接受 commit/artifact_id/params),序列冻结进 research_factor_series"
            "(FS- 前缀),供 research run 的 factor_series_ids 引用(u_ / p_ 因子"
            "按决策日索引,multi_period 可用)。release_id 为 bars 主发布,自动"
            "进挂载(必须为 bars 类发布,否则秒拒);dataset_release_ids 是"
            "研究数据发布联合集,不得含 bars 主发布。入队预检:factor 需 sandbox "
            "开启且 (factor,name) 有 active 产物(显式 artifact_id 也可,但未通过"
            "晋级门拒绝);predefined 只需名称已注册;release 均已登记。缓存命中"
            "(series_key 已存在且 content_checksum 一致)直接返回 unchanged,不"
            "创建任务;同参数任务此前 failed/cancelled 时重提交会新建任务(#371),"
            "不会命中失败尸体。构建完成后抽 2 个截断点做前缀不变性审计(基线复用"
            "主构建产物,变体挂载由基线 Arrow 过滤派生),检出前视即 "
            "failed=lookahead_detected。换 bars 发布的托管批量重建 = 对每个失效"
            "序列逐条调用本工具(内容寻址缓存使未受影响的组合自动 unchanged)。"
            "失败分类:sandbox_disabled / static 类 / runtime_error / timeout / "
            "oom_killed / output_contract_violation / lookahead_detected / "
            "quality_gate_failed / unknown_predefined_factor / "
            "predefined_version_mismatch。返回 job_id,finboard_job_get 轮询"
            "(result_ref=FS-...),终态后 finboard_factor_series_get 取内容。"
            "纯离线研究域,不连 broker 不下单。"
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
        kind: str = USER_FACTOR_KIND,
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
            kind=kind,
        )

    @mcp.tool(
        name="finboard_factor_series_get",
        description=(
            "查询内容寻址因子序列(research_factor_series,FS- 前缀):三向审计引用"
            "(code_artifact/commit/kind)、bars 主发布锚定与研究发布联合集、窗口、"
            "decision 日数与标的数、content_checksum、质量门归档(quality)。"
            "view=summary 默认(#206 瘦身,不含逐日 values);view=detail 附 dates"
            " 与逐日 values({date:{symbol:float|null}})全量,估计超过 64MB 时"
            "返回 payload_too_large(不静默截断,summary 不受影响;全量消费走 "
            "research_run 的 factor_series_ids 加载通道)。该序列可被 research"
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
