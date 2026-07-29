"""统一离线研究运行的排队、历史、血缘、取消与重放 API(issue #80)。

安全边界:本路由没有 execute/run 端点。它只冻结输入并登记 queued 任务;实际
执行由受控的离线 worker/CLI 调用 ``ResearchRunCoordinator``。LLM actor 被 schema
和领域 manifest 双重拒绝。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query
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
from finboard_backtest.strategy_spec import ResearchStrategySpec
from finboard_backtest.strategy_spec.contracts import FeatureKind
from finboard_data.releases import ReleaseCapabilityError
from finboard_persistence import (
    FeatureSnapshotRepository,
    ResearchDatasetReleaseRepository,
    ResearchRunPersistenceConflictError,
    ResearchRunRepository,
    ResearchStrategySpecRepository,
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
    if sorted(body.dataset_release_ids) != sorted(
        spec.validation_plan.dataset_release_ids
    ):
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
            {
                item.key
                for release in releases
                for item in release.capabilities
                if item.ready
            },
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
        if node.source is not None
        and node.kind in {FeatureKind.FACTOR, FeatureKind.RISK_FACTOR}
    }
    if required_factor_sources and not snapshots:
        raise HTTPException(
            status_code=422,
            detail=(
                "策略依赖因子输入但未冻结 factor_snapshot_ids: "
                f"{sorted(required_factor_sources)}"
            ),
        )
    release_ids = {release.release_id for release in releases}
    for snapshot in snapshots:
        if snapshot.dataset_release_id not in release_ids:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"因子快照 {snapshot.snapshot_id} 绑定的数据发布"
                    "不在本次冻结清单中"
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
                    capabilities=tuple(
                        item.key for item in release.capabilities if item.ready
                    ),
                )
                for release in releases
            ),
            factor_snapshots=tuple(
                FrozenArtifactRef(
                    artifact_id=snapshot.snapshot_id,
                    version=snapshot.framework_version,
                    checksum=snapshot.checksum,
                    capabilities=tuple(
                        sorted(
                            {
                                f"factor:{item.feature_name}"
                                for item in snapshot.observations
                            }
                        )
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
        await session.commit()
    except (ValueError, ValidationError) as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ResearchRunPersistenceConflictError, IntegrityError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ResearchRunOut.model_validate(row)


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
    from finboard_backtest.research_run import ResearchRunCoordinator

    coordinator = ResearchRunCoordinator(
        SqlAlchemyResearchRunStore(ResearchRunRepository(session))
    )
    try:
        record = await coordinator.cancel(run_id)
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
        record, _ = await store.create_or_get(manifest)
        await session.commit()
    except (ResearchRunPersistenceConflictError, IntegrityError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    row = await ResearchRunRepository(session).get(record.manifest.run_id)
    assert row is not None
    return ResearchRunOut.model_validate(row)


def _run_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"RR-{digest}"


__all__ = ["router"]
