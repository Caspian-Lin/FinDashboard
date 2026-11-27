"""统一离线研究运行的排队、历史、血缘、取消与重放 API(issue #80)。

安全边界:本路由没有 execute/run 端点。它只冻结输入并登记 queued 任务;实际
执行由受控的离线 worker/CLI 调用 ``ResearchRunCoordinator``。LLM actor 被 schema
和领域 manifest 双重拒绝。

issue #513:入队 / 重放的编排(预检 → 冻结 → manifest → 双写 run+job)已下沉到
``finboard_app.research_run_submission``,与 MCP ``finboard_run_queue`` 共用同一
实现。本模块只做三件事:解析输入 → 调服务 → 把 ``ResearchOperationError`` 映射
成 HTTP,并在唯一一次 ``commit()`` 前保持事务所有权。
"""

from __future__ import annotations

import asyncio
import re
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.research_run_schemas import (
    ResearchArtifactOut,
    ResearchLineageOut,
    ResearchRunOut,
    ResearchRunQueueIn,
    ResearchRunReplayIn,
)
from finboard_app.research_operation import (
    ResearchOperationContext,
    ResearchOperationError,
)
from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_app.research_run_submission import ResearchRunSubmissionService
from finboard_backtest.research_run import (
    ResearchActorType,
    ResearchRunConflictError,
    ResearchRunStatus,
)
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
    ResearchRunPersistenceConflictError,
    ResearchRunRepository,
)

router = APIRouter(prefix="/api/research/runs", tags=["research-runs"])


def _sandbox_enabled(request: Request) -> bool:
    """研究沙箱开关由应用配置注入(服务层不自行读环境,#513)。"""

    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return bool(settings.research_sandbox_enabled)
    from finboard_app.config import load_settings

    return load_settings().research_sandbox_enabled


def _http_error(exc: ResearchOperationError) -> HTTPException:
    """统一业务错误 → HTTP(状态码取自错误类,``detail`` 为结构化统一体)。"""

    return HTTPException(status_code=exc.http_status, detail=exc.to_detail())


def _web_context(request: Request, actor_type: str) -> ResearchOperationContext:
    return ResearchOperationContext.for_web(
        request_id=request.headers.get("x-request-id"),
        actor_type=ResearchActorType(actor_type),
    )


@router.post("", response_model=ResearchRunOut, status_code=201)
async def queue_research_run(
    body: ResearchRunQueueIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunOut:
    """冻结输入并登记 queued 运行;不会在 HTTP 请求内执行回测。"""

    service = ResearchRunSubmissionService(
        session, sandbox_enabled=_sandbox_enabled(request)
    )
    try:
        outcome = await service.submit(body, _web_context(request, body.actor_type))
        await session.commit()
    except ResearchOperationError as exc:
        await session.rollback()
        raise _http_error(exc) from exc
    # 提交后重读整行:created_at/updated_at 是 server_default(RETURNING 不保证
    # 覆盖 onupdate 列),直接序列化新插入对象会在 async 上下文触发 lazy-load
    # MissingGreenlet。与 cancel/replay 的「提交后 get 重读」口径一致。
    fresh = await ResearchRunRepository(session).get(outcome.run_row.run_id)
    assert fresh is not None
    return ResearchRunOut.model_validate(fresh)


@router.get("", response_model=list[ResearchRunOut])
async def list_research_runs(
    status: list[str] | None = Query(default=None),
    strategy_kind: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[ResearchRunOut]:
    valid = {item.value for item in ResearchRunStatus}
    if status is not None and not set(status).issubset(valid):
        raise HTTPException(status_code=422, detail="未知 ResearchRun 状态")
    rows = await ResearchRunRepository(session).list_recent(
        statuses=status,
        strategy_kind=strategy_kind,
        limit=limit,
    )
    return [ResearchRunOut.model_validate(row) for row in rows]


@router.get("/{run_id}", response_model=ResearchRunOut)
async def get_research_run(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunOut:
    row = await ResearchRunRepository(session).get(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="研究运行不存在")
    return ResearchRunOut.model_validate(row)


@router.get("/{run_id}/artifacts", response_model=list[ResearchArtifactOut])
async def list_research_artifacts(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> list[ResearchArtifactOut]:
    repo = ResearchRunRepository(session)
    if await repo.get(run_id) is None:
        raise HTTPException(status_code=404, detail="研究运行不存在")
    await _require_artifact_payload_within_limit(repo, run_id)
    rows = await repo.list_artifacts(run_id)
    return [ResearchArtifactOut.model_validate(row) for row in rows]


@router.get(
    "/{run_id}/lineage/{trace_id}",
    response_model=ResearchLineageOut,
)
async def get_research_lineage(
    run_id: str,
    trace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchLineageOut:
    repo = ResearchRunRepository(session)
    store = SqlAlchemyResearchRunStore(repo)
    from finboard_backtest.research_run import ResearchRunCoordinator

    # issue #480:lineage 内部全量物化 artifacts,同款加载前护栏。
    await _require_artifact_payload_within_limit(repo, run_id)
    artifacts = await ResearchRunCoordinator(store).lineage(run_id, trace_id)
    if not artifacts:
        raise HTTPException(status_code=404, detail="血缘 trace 不存在")
    return ResearchLineageOut(
        run_id=run_id,
        leaf_trace_id=trace_id,
        artifacts=[
            ResearchArtifactOut(
                artifact_id=item.artifact_id,
                run_id=item.run_id,
                decision_id=item.decision_id,
                sequence=item.sequence,
                stage=item.stage.value,
                trace_id=item.trace_id,
                parent_trace_ids=list(item.parent_trace_ids),
                payload=cast(dict[str, object], item.payload),
                checksum=item.checksum,
                created_at=item.created_at,
            )
            for item in artifacts
        ],
    )


@router.post("/{run_id}/cancel", response_model=ResearchRunOut)
async def cancel_research_run(
    run_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunOut:
    import contextlib

    from finboard_backtest.research_run import ResearchRunCoordinator

    coordinator = ResearchRunCoordinator(SqlAlchemyResearchRunStore(ResearchRunRepository(session)))
    try:
        record = await coordinator.cancel(run_id)
        # issue #143:协作式取消联动 background_jobs(running → cancel_requested,
        # worker 下次 checkpoint 时 research_run CANCELLED 退出,job 收口 cancelled)。
        # 已终态的 job request_cancel 抛 ConflictError,幂等 suppress。
        if record.job_id:
            with contextlib.suppress(BackgroundJobPersistenceConflictError):
                await BackgroundJobRepository(session).request_cancel(record.job_id)
        await session.commit()
    except (ResearchRunConflictError, ResearchRunPersistenceConflictError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    row = await ResearchRunRepository(session).get(record.manifest.run_id)
    assert row is not None
    return ResearchRunOut.model_validate(row)


@router.post("/{run_id}/replay", response_model=ResearchRunOut, status_code=201)
async def queue_research_replay(
    run_id: str,
    body: ResearchRunReplayIn,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunOut:
    """复制完整冻结清单为新 queued 运行;仍不在 HTTP 中执行。

    issue #305:interrupted 源放开为事故恢复通道 —— 新 run 自动继承原
    manifest 全部冻结输入(含 factor_snapshots 全部 ID),血缘标注
    ``replay_of_run_id`` + ``replay_source_status``;cancelled 仍拒绝,
    completed 重放行为不变。
    """

    service = ResearchRunSubmissionService(
        session, sandbox_enabled=_sandbox_enabled(request)
    )
    try:
        outcome = await service.replay(
            run_id, body, _web_context(request, body.actor_type)
        )
        await session.commit()
    except ResearchOperationError as exc:
        await session.rollback()
        raise _http_error(exc) from exc
    row = await ResearchRunRepository(session).get(outcome.run_row.run_id)
    assert row is not None
    return ResearchRunOut.model_validate(row)


@router.get("/{run_id}/report/export")
async def export_research_run_report(
    run_id: str,
    format: str = Query(default="csv", pattern="^(csv|markdown)$"),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """把 ResearchRun 报告导出为 CSV / Markdown 下载(#157,#141 的 Web 闭环)。

    复用 ``finboard_mcp.reporting`` 的聚合与渲染(MCP ``finboard_report_export``
    同一逻辑,不复制实现);与 MCP 不同,这里直接以 HTTP 响应返回内容,不落盘。
    """
    from finboard_mcp import reporting

    repo = ResearchRunRepository(session)
    row = await repo.get(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="研究运行不存在")
    await _require_artifact_payload_within_limit(repo, run_id)
    artifacts = await repo.list_artifacts(run_id)
    report = reporting.aggregate_run_report(row, artifacts, view="detail")
    content = await asyncio.to_thread(reporting.render_report, "run", report, format)
    filename = f"finboard_run_{_safe_filename(run_id)}.{_report_extension(format)}"
    media_type = "text/csv; charset=utf-8" if format == "csv" else "text/markdown; charset=utf-8"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


async def _require_artifact_payload_within_limit(
    repo: ResearchRunRepository,
    run_id: str,
) -> None:
    """全量 payload 路径加载前的数据库侧载荷护栏(issue #480)。

    与 MCP 侧同口径:先 ``estimate_artifact_payload_bytes``
    (``SUM(octet_length(payload::text))``,不取回 payload),超过
    ``RUN_DETAIL_MAX_ESTIMATED_BYTES`` 以 413 具名拒绝 —— 旧口径直接
    ``list_artifacts`` 全量物化,真实 run(7203 artifacts / ≈5.9GB JSON)
    曾把 dev server 进程顶到 10GB+ 且拖到客户端超时。
    """
    from finboard_mcp import reporting

    estimated = await repo.estimate_artifact_payload_bytes(run_id)
    if estimated > reporting.RUN_DETAIL_MAX_ESTIMATED_BYTES:
        estimated_mb = estimated / (1024 * 1024)
        limit_mb = reporting.RUN_DETAIL_MAX_ESTIMATED_BYTES / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=(
                f"run {run_id} 的 artifacts 载荷估计约 {estimated_mb:.1f}MB,"
                f"超过上限 {limit_mb:.0f}MB,拒绝加载(不静默截断)。替代路径:"
                "run 详情的聚合计数与 metrics;MCP finboard_report_run / "
                "finboard_report_export 按 decision_id 按决策下钻。"
            ),
        )


def _safe_filename(value: str) -> str:
    """把 run_id 清理为安全的文件名片段(RR- 前缀本身安全,防御性处理)。"""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value) or "unknown"


def _report_extension(fmt: str) -> str:
    return "md" if fmt == "markdown" else fmt


__all__ = ["router"]
