"""版本化无代码研究策略端点(issue #79)。

所有写操作只保存/发布结构化研究规格。端点不启动回测、模拟盘或实盘。不构造订单。
也不允许源码、模块路径、模板或通用表达式。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.strategy_spec_schemas import (
    StrategySpecDiffOut,
    StrategySpecDraftIn,
    StrategySpecPublishIn,
    StrategySpecRegistryOut,
    StrategySpecRollbackIn,
    StrategySpecSupersedeIn,
    StrategySpecValidateIn,
    StrategySpecValidationOut,
    StrategySpecVersionOut,
)
from finboard_backtest.strategy_spec import (
    FEATURE_SOURCE_CATALOG,
    LIFECYCLE_STAGES,
    FeatureOperator,
    ResearchStrategySpec,
    ResolvedStrategyPlan,
    StrategySpecError,
    build_strategy_template,
    compile_registered_strategy_spec,
    list_strategy_capabilities,
    structured_diff,
)
from finboard_data.releases import ReleaseCapabilityError
from finboard_persistence import (
    ResearchDatasetReleaseRepository,
    ResearchStrategySpecModel,
    ResearchStrategySpecRepository,
    StrategySpecChangeType,
    StrategySpecTransitionError,
    StrategySpecVersionConflictError,
)

router = APIRouter(
    prefix="/api/research/strategy-specs",
    tags=["research-strategy-specs"],
)


def _version_out(row: ResearchStrategySpecModel) -> StrategySpecVersionOut:
    return StrategySpecVersionOut(
        strategy_id=row.strategy_id,
        version=row.version,
        schema_version=row.schema_version,
        name=row.name,
        strategy_kind=row.strategy_kind,
        status=row.status,
        change_type=row.change_type,
        checksum=row.checksum,
        spec=ResearchStrategySpec.model_validate(row.payload),
        validation_errors=list(row.validation_errors),
        parent_version=row.parent_version,
        rollback_of_version=row.rollback_of_version,
        created_at=row.created_at,
        published_at=row.published_at,
    )


def _validation_out(plan: ResolvedStrategyPlan) -> StrategySpecValidationOut:
    return StrategySpecValidationOut(
        checksum=plan.checksum,
        feature_order=list(plan.feature_order),
        required_factor_sources=list(plan.required_factor_sources),
        required_datasets=list(plan.required_datasets),
        dataset_release_ids=list(plan.dataset_release_ids),
        lifecycle_stages=list(plan.lifecycle_stages),
        can_execute=plan.can_execute,
    )


def _strategy_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": "strategy_spec_invalid",
            "message": str(exc),
        },
    )


async def _compile_with_releases(
    spec: ResearchStrategySpec,
    session: AsyncSession,
    *,
    disabled_factors: frozenset[str] = frozenset(),
) -> ResolvedStrategyPlan:
    release_repo = ResearchDatasetReleaseRepository(session)
    available: set[str] = set()
    try:
        for release_id in spec.validation_plan.dataset_release_ids:
            release = await release_repo.require_usable(release_id)
            available.add(release.release_id)
        return compile_registered_strategy_spec(
            spec,
            disabled_factors=disabled_factors,
            available_dataset_release_ids=frozenset(available),
        )
    except (ReleaseCapabilityError, StrategySpecError, ValueError) as exc:
        raise _strategy_error(exc) from exc


@router.get("/registry", response_model=StrategySpecRegistryOut)
async def get_strategy_spec_registry() -> StrategySpecRegistryOut:
    """返回前端可安全生成控件的注册表。不返回 Python 类或模块路径。"""

    sources: list[dict[str, object]] = [
        {
            "name": item.name,
            "kind": item.kind.value,
            "required_datasets": list(item.required_datasets),
            "description": item.description,
        }
        for item in sorted(FEATURE_SOURCE_CATALOG.values(), key=lambda value: value.name)
    ]
    operators = [
        {
            "name": operator.value,
            "executable_expression": False,
        }
        for operator in FeatureOperator
    ]
    return StrategySpecRegistryOut(
        strategies=[item.as_dict() for item in list_strategy_capabilities()],
        feature_sources=sources,
        operators=operators,
        lifecycle_stages=list(LIFECYCLE_STAGES),
    )


@router.get("/templates/{kind}", response_model=ResearchStrategySpec)
async def get_strategy_spec_template(
    kind: str,
    strategy_id: str = Query(min_length=1, max_length=64),
    dataset_release_ids: list[str] = Query(min_length=1),
) -> ResearchStrategySpec:
    try:
        return build_strategy_template(
            kind,
            strategy_id=strategy_id,
            dataset_release_ids=tuple(dataset_release_ids),
        )
    except (StrategySpecError, ValidationError, ValueError) as exc:
        raise _strategy_error(exc) from exc


@router.post("/validate", response_model=StrategySpecValidationOut)
async def validate_strategy_spec(
    body: StrategySpecValidateIn,
    session: AsyncSession = Depends(get_db_session),
) -> StrategySpecValidationOut:
    plan = await _compile_with_releases(
        body.spec,
        session,
        disabled_factors=body.disabled_factors,
    )
    return _validation_out(plan)


@router.post("/drafts", response_model=StrategySpecVersionOut, status_code=201)
async def create_strategy_spec_draft(
    body: StrategySpecDraftIn,
    session: AsyncSession = Depends(get_db_session),
) -> StrategySpecVersionOut:
    plan = await _compile_with_releases(body.spec, session)
    repo = ResearchStrategySpecRepository(session)
    try:
        row = await repo.create_draft(
            strategy_id=body.spec.strategy_id,
            schema_version=body.spec.schema_version,
            name=body.spec.name,
            strategy_kind=body.spec.strategy_kind,
            checksum=plan.checksum,
            payload=body.spec.canonical_payload(),
            expected_version=body.expected_version,
        )
        await session.commit()
    except StrategySpecVersionConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="策略版本冲突") from exc
    return _version_out(row)


@router.post(
    "/{strategy_id}/supersede",
    response_model=StrategySpecVersionOut,
    status_code=201,
)
async def supersede_strategy_spec(
    strategy_id: str,
    body: StrategySpecSupersedeIn,
    session: AsyncSession = Depends(get_db_session),
) -> StrategySpecVersionOut:
    if body.spec.strategy_id != strategy_id:
        raise HTTPException(status_code=422, detail="路径 strategy_id 与规格不一致")
    plan = await _compile_with_releases(body.spec, session)
    repo = ResearchStrategySpecRepository(session)
    try:
        row = await repo.create_draft(
            strategy_id=strategy_id,
            schema_version=body.spec.schema_version,
            name=body.spec.name,
            strategy_kind=body.spec.strategy_kind,
            checksum=plan.checksum,
            payload=body.spec.canonical_payload(),
            expected_version=body.expected_version,
            change_type=StrategySpecChangeType.SUPERSEDE,
        )
        await session.commit()
    except StrategySpecVersionConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="策略版本冲突") from exc
    return _version_out(row)


@router.post("/{strategy_id}/publish", response_model=StrategySpecVersionOut)
async def publish_strategy_spec(
    strategy_id: str,
    body: StrategySpecPublishIn,
    session: AsyncSession = Depends(get_db_session),
) -> StrategySpecVersionOut:
    repo = ResearchStrategySpecRepository(session)
    target = await repo.get_version(strategy_id, body.version)
    if target is None:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    try:
        spec = ResearchStrategySpec.model_validate(target.payload)
        await _compile_with_releases(spec, session)
        row = await repo.publish(
            strategy_id,
            body.version,
            expected_version=body.expected_version,
        )
        await session.commit()
    except StrategySpecVersionConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StrategySpecTransitionError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _version_out(row)


@router.post("/{strategy_id}/rollback", response_model=StrategySpecVersionOut)
async def rollback_strategy_spec(
    strategy_id: str,
    body: StrategySpecRollbackIn,
    session: AsyncSession = Depends(get_db_session),
) -> StrategySpecVersionOut:
    repo = ResearchStrategySpecRepository(session)
    target = await repo.get_version(strategy_id, body.target_version)
    if target is None:
        raise HTTPException(status_code=404, detail="回滚目标版本不存在")
    try:
        spec = ResearchStrategySpec.model_validate(target.payload)
        await _compile_with_releases(spec, session)
        row = await repo.rollback(
            strategy_id,
            body.target_version,
            expected_version=body.expected_version,
        )
        await session.commit()
    except StrategySpecVersionConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StrategySpecTransitionError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _version_out(row)


@router.get("", response_model=list[StrategySpecVersionOut])
async def list_strategy_specs(
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[StrategySpecVersionOut]:
    rows = await ResearchStrategySpecRepository(session).list_latest(limit=limit)
    return [_version_out(row) for row in rows]


@router.get("/{strategy_id}/history", response_model=list[StrategySpecVersionOut])
async def get_strategy_spec_history(
    strategy_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> list[StrategySpecVersionOut]:
    rows = await ResearchStrategySpecRepository(session).list_history(strategy_id)
    if not rows:
        raise HTTPException(status_code=404, detail="策略规格不存在")
    return [_version_out(row) for row in rows]


@router.get("/{strategy_id}/versions/{version}", response_model=StrategySpecVersionOut)
async def get_strategy_spec_version(
    strategy_id: str,
    version: int,
    session: AsyncSession = Depends(get_db_session),
) -> StrategySpecVersionOut:
    row = await ResearchStrategySpecRepository(session).get_version(strategy_id, version)
    if row is None:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    return _version_out(row)


@router.get("/{strategy_id}/diff", response_model=StrategySpecDiffOut)
async def diff_strategy_spec_versions(
    strategy_id: str,
    from_version: int = Query(ge=1),
    to_version: int = Query(ge=1),
    session: AsyncSession = Depends(get_db_session),
) -> StrategySpecDiffOut:
    repo = ResearchStrategySpecRepository(session)
    before = await repo.get_version(strategy_id, from_version)
    after = await repo.get_version(strategy_id, to_version)
    if before is None or after is None:
        raise HTTPException(status_code=404, detail="对比版本不存在")
    changes = structured_diff(before.payload, after.payload)
    return StrategySpecDiffOut(
        strategy_id=strategy_id,
        from_version=from_version,
        to_version=to_version,
        changes=[change.as_dict() for change in changes],
    )
