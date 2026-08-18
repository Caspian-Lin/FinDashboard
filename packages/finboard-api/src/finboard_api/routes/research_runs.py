"""统一离线研究运行的排队、历史、血缘、取消与重放 API(issue #80)。

安全边界:本路由没有 execute/run 端点。它只冻结输入并登记 queued 任务;实际
执行由受控的离线 worker/CLI 调用 ``ResearchRunCoordinator``。LLM actor 被 schema
和领域 manifest 双重拒绝。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import replace
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.research_run_schemas import (
    ResearchArtifactOut,
    ResearchLineageOut,
    ResearchRunOut,
    ResearchRunQueueIn,
    ResearchRunReplayIn,
)
from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.research_run import (
    FrozenArtifactRef,
    ResearchActorType,
    ResearchRunConflictError,
    ResearchRunManifest,
    ResearchRunStatus,
    UnsupportedResearchCapabilityError,
    to_json_value,
    validate_strategy_dataset_capabilities,
)
from finboard_backtest.research_run.contracts import JsonValue
from finboard_backtest.research_run.signal_engine import single_shot_snapshot_gate_error
from finboard_backtest.strategy_spec import ResearchStrategySpec
from finboard_backtest.strategy_spec.contracts import FeatureKind
from finboard_backtest.strategy_spec.universe_precheck import (
    describe_empty_pool,
    preview_universe_pool,
    resolvable_feature_names,
)
from finboard_data.releases import (
    ReleaseCapabilityError,
    ReleaseDatasetKind,
    ResearchDatasetRelease,
)
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    BackgroundJobRepository,
    FeatureSnapshotRepository,
    ResearchDatasetReleaseRepository,
    ResearchRunPersistenceConflictError,
    ResearchRunRepository,
    ResearchStrategySpecRepository,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)

router = APIRouter(prefix="/api/research/runs", tags=["research-runs"])


@router.post("", response_model=ResearchRunOut, status_code=201)
async def queue_research_run(
    body: ResearchRunQueueIn,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunOut:
    """冻结输入并登记 queued 运行;不会在 HTTP 请求内执行回测。"""

    strategy_row = await ResearchStrategySpecRepository(session).get_version(
        body.strategy_id, body.strategy_version
    )
    if strategy_row is None:
        raise HTTPException(status_code=404, detail="策略规格版本不存在")
    if strategy_row.status != "published":
        raise HTTPException(status_code=409, detail="仅已发布策略规格可以进入研究运行")
    spec = ResearchStrategySpec.model_validate(strategy_row.payload)
    if sorted(body.dataset_release_ids) != sorted(spec.validation_plan.dataset_release_ids):
        raise HTTPException(
            status_code=422,
            detail="运行数据发布必须与策略验证计划完全一致",
        )
    try:
        releases = [
            await ResearchDatasetReleaseRepository(session).require_usable(release_id)
            for release_id in body.dataset_release_ids
        ]
    except ReleaseCapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        validate_strategy_dataset_capabilities(
            spec.strategy_kind,
            {item.key for release in releases for item in release.capabilities if item.ready},
        )
    except UnsupportedResearchCapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    feature_repo = FeatureSnapshotRepository(session)
    snapshots = []
    for snapshot_id in body.factor_snapshot_ids:
        snapshot = await feature_repo.get(snapshot_id)
        if snapshot is None:
            raise HTTPException(
                status_code=422,
                detail=f"因子快照不存在: {snapshot_id}",
            )
        snapshots.append(snapshot)
    required_factor_sources = {
        node.source
        for node in spec.feature_graph.nodes
        if node.source is not None and node.kind in {FeatureKind.FACTOR, FeatureKind.RISK_FACTOR}
    }
    # issue #203:入队期 single_shot 缺快照秒级拒绝(与 MCP 共用同一门控函数,
    # 对齐 #186 预检风格)。multi_period 声明 rebalance_frequency 后不受影响。
    gate_error = single_shot_snapshot_gate_error(
        strategy_kind=spec.strategy_kind,
        required_factor_sources=required_factor_sources,
        frozen_snapshot_count=len(snapshots),
        parameters=body.parameters,
    )
    if gate_error is not None:
        raise HTTPException(status_code=422, detail=gate_error)
    release_ids = {release.release_id for release in releases}
    for snapshot in snapshots:
        if snapshot.dataset_release_id not in release_ids:
            raise HTTPException(
                status_code=422,
                detail=(f"因子快照 {snapshot.snapshot_id} 绑定的数据发布不在本次冻结清单中"),
            )

    # issue #186:入队同步候选池非空校验。用 bars 主发布(信号引擎实际使用的
    # 发布)的 instruments 做静态评估,空池秒级 422(invalid_argument 语义),附
    # 各过滤条件排除统计与缺失字段名,不再等执行期跑 30 分钟后才报泛化错误。
    # issue #187:联合发布中研究数据 release(daily_metrics/financial_indicators)
    # 只提供因子观测,候选池评估必须落在 bars 主发布上。
    primary = _bars_release(releases)
    preview = preview_universe_pool(
        spec.universe,
        primary.instruments,
        decision_date=primary.end_date,
        available_features=resolvable_feature_names(
            feature_graph_sources=[
                node.source for node in spec.feature_graph.nodes if node.source is not None
            ],
            snapshot_feature_names=[
                observation.feature_name
                for snapshot in snapshots
                for observation in snapshot.observations
            ],
        ),
    )
    if preview.is_empty:
        raise HTTPException(
            status_code=422,
            detail=(
                "运行数据发布候选池为空,拒绝入队: "
                f"{describe_empty_pool(preview, decision_date=primary.end_date)}"
            ),
        )

    run_id = _run_id(body.idempotency_key)
    try:
        manifest = ResearchRunManifest(
            run_id=run_id,
            idempotency_key=body.idempotency_key,
            strategy_spec=spec,
            strategy_spec_checksum=strategy_row.checksum,
            dataset_releases=tuple(
                FrozenArtifactRef(
                    artifact_id=release.release_id,
                    version=release.version,
                    checksum=release.release_checksum,
                    capabilities=tuple(item.key for item in release.capabilities if item.ready),
                )
                for release in releases
            ),
            strategy_version=body.strategy_version,
            factor_snapshots=tuple(
                FrozenArtifactRef(
                    artifact_id=snapshot.snapshot_id,
                    version=snapshot.framework_version,
                    checksum=snapshot.checksum,
                    capabilities=tuple(
                        sorted({f"factor:{item.feature_name}" for item in snapshot.observations})
                    ),
                )
                for snapshot in snapshots
            ),
            parameters=cast(dict[str, JsonValue], body.parameters),
            validation_config=cast(
                dict[str, JsonValue],
                {
                    "strategy_spec": spec.validation_plan.model_dump(mode="json"),
                    "overrides": body.validation_config,
                },
            ),
            portfolio_config=cast(
                dict[str, JsonValue],
                {
                    "strategy_spec": spec.portfolio_policy.model_dump(mode="json"),
                    "overrides": body.portfolio_config,
                },
            ),
            risk_config=cast(
                dict[str, JsonValue],
                {
                    "strategy_spec": spec.risk_exit_policy.model_dump(mode="json"),
                    "overrides": body.risk_config,
                },
            ),
            execution_config=cast(
                dict[str, JsonValue],
                {
                    "strategy_spec": spec.execution_model.model_dump(mode="json"),
                    "overrides": body.execution_config,
                },
            ),
            fee_config=cast(
                dict[str, JsonValue],
                {
                    "commission_rate": spec.execution_model.commission_rate,
                    "minimum_commission": spec.execution_model.minimum_commission,
                    "sell_tax_rate": spec.execution_model.sell_tax_rate,
                    "slippage_bps": spec.execution_model.slippage_bps,
                    "overrides": body.fee_config,
                },
            ),
            benchmark_config=cast(
                dict[str, JsonValue],
                {
                    "symbol": spec.validation_plan.benchmark_symbol,
                    "overrides": body.benchmark_config,
                },
            ),
            code_version=body.code_version,
            initial_capital=body.initial_capital,
            requested_by=body.requested_by,
            actor_type=ResearchActorType.HUMAN,
        )
        row, _ = await ResearchRunRepository(session).create_or_get(
            run_id=manifest.run_id,
            idempotency_key=manifest.idempotency_key,
            replay_of_run_id=None,
            strategy_id=manifest.strategy_spec.strategy_id,
            strategy_kind=manifest.strategy_kind,
            status=ResearchRunStatus.QUEUED.value,
            schema_version=manifest.schema_version,
            manifest_checksum=manifest.checksum,
            manifest=cast(dict[str, object], to_json_value(manifest)),
            requested_by=manifest.requested_by,
        )
        # issue #143:同事务双写 background_jobs,共用 idempotency_key 保证幂等。
        # 已存在的 research_run(幂等命中)若已有 job_id 则保留,否则补建。
        if not row.job_id:
            row.job_id = await _enqueue_research_run_job(session, manifest=manifest)
        await session.commit()
    except (ValueError, ValidationError) as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (
        ResearchRunPersistenceConflictError,
        BackgroundJobPersistenceConflictError,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # 提交后重读整行:created_at/updated_at 是 server_default(RETURNING 不保证
    # 覆盖 onupdate 列),直接序列化新插入对象会在 async 上下文触发 lazy-load
    # MissingGreenlet。与 cancel/replay 的「提交后 get 重读」口径一致。
    fresh = await ResearchRunRepository(session).get(run_id)
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
    store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
    from finboard_backtest.research_run import ResearchRunCoordinator

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
    session: AsyncSession = Depends(get_db_session),
) -> ResearchRunOut:
    """复制完整冻结清单为新 queued 运行;仍不在 HTTP 中执行。"""

    store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
    source = await store.get(run_id)
    if source is None:
        raise HTTPException(status_code=404, detail="源研究运行不存在")
    if source.status is not ResearchRunStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="仅允许重放已完成运行")
    manifest = replace(
        source.manifest,
        run_id=_run_id(body.idempotency_key),
        idempotency_key=body.idempotency_key,
        requested_by=body.requested_by,
        actor_type=ResearchActorType.HUMAN,
        replay_of_run_id=run_id,
    )
    try:
        record, created = await store.create_or_get(manifest)
        # issue #143:重放也走双写;background_jobs 用重放后的新 idempotency_key。
        if created:
            row = await ResearchRunRepository(session).get(record.manifest.run_id)
            assert row is not None
            if not row.job_id:
                row.job_id = await _enqueue_research_run_job(session, manifest=manifest)
        await session.commit()
    except (
        ResearchRunPersistenceConflictError,
        BackgroundJobPersistenceConflictError,
        IntegrityError,
    ) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    row = await ResearchRunRepository(session).get(record.manifest.run_id)
    assert row is not None
    return ResearchRunOut.model_validate(row)


def _run_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"RR-{digest}"


def _bars_release(releases: list[ResearchDatasetRelease]) -> ResearchDatasetRelease:
    """取联合发布中的 bars 主发布(issue #187)。

    排除 daily_metrics / financial_indicators 研究数据发布;恰好一个 bars
    发布才有候选池评估意义,否则 422。
    """
    bars_releases = [
        item for item in releases if item.dataset_kind is ReleaseDatasetKind.BARS
    ]
    if len(bars_releases) != 1:
        raise HTTPException(
            status_code=422,
            detail=(
                "联合发布必须恰好包含一个 bars 主发布(行情/候选池来源): "
                + ", ".join(f"{item.release_id}({item.dataset_kind.value})" for item in releases)
            ),
        )
    return bars_releases[0]


def _payload_checksum(payload: dict[str, object]) -> str:
    """计算 background_jobs payload checksum(与 jobs.py 路由保持一致)。"""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def _enqueue_research_run_job(
    session: AsyncSession,
    *,
    manifest: ResearchRunManifest,
) -> str:
    """在同事务内为研究运行创建 background_jobs 行,返回 job_id(issue #143)。

    与 research_run 共用 idempotency_key,保证两系统幂等一致:任一系统命中即
    不重复创建。调用方负责把返回的 job_id 回填到 research_runs.job_id。
    """
    payload: dict[str, object] = {
        "run_id": manifest.run_id,
        "strategy_kind": manifest.strategy_kind,
    }
    job_repo = BackgroundJobRepository(session)
    job_row, _ = await job_repo.create_or_get(
        job_id=generate_background_job_id(),
        idempotency_key=manifest.idempotency_key,
        kind="research_run",
        queue="research",
        status=BackgroundJobStatus.QUEUED.value,
        priority=0,
        payload=payload,
        payload_checksum=_payload_checksum(payload),
        max_attempts=3,
        requested_by=manifest.requested_by,
    )
    return job_row.job_id


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
    artifacts = await repo.list_artifacts(run_id)
    report = reporting.aggregate_run_report(row, artifacts)
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


def _safe_filename(value: str) -> str:
    """把 run_id 清理为安全的文件名片段(RR- 前缀本身安全,防御性处理)。"""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value) or "unknown"


def _report_extension(fmt: str) -> str:
    return "md" if fmt == "markdown" else fmt


__all__ = ["router"]
