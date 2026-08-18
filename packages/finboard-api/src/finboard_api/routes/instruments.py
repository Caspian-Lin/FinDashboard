"""多资产元数据端点(issue #58)。

提供 ETF / 可转债 / 国债 / 期货合约的元数据查询,以及数据集覆盖率审计。
本端点只读,不触及交易红线。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.job_schemas import JobOut
from finboard_api.schemas import (
    BondMetadataOut,
    ConvertibleMetadataOut,
    DatasetManifestOut,
    DatasetReleaseCapabilityOut,
    EtfAuditOut,
    EtfBatchConfirmRequest,
    EtfClassificationUpdate,
    EtfMetadataOut,
    EtfMetadataSummaryOut,
    EtfSyncPreviewOut,
    EtfSyncRequest,
    FuturesContractOut,
    InstrumentOut,
    LifecycleEventOut,
    ResearchDatasetReleaseCreate,
    ResearchDatasetReleaseOut,
    ResearchDatasetReleaseSummaryOut,
)
from finboard_persistence import (
    BackgroundJobPersistenceConflictError,
    BondMetadataModel,
    ConvertibleMetadataModel,
    DatasetManifestModel,
    EtfMetadataAuditModel,
    EtfMetadataModel,
    EtfMetadataRepository,
    FuturesContractModel,
    InstrumentLifecycleEventModel,
    InstrumentModel,
    ResearchDatasetReleaseRepository,
)
from finboard_shared.types import EtfExecutionProfile

router = APIRouter(prefix="/api/instruments", tags=["instruments"])

_DEFAULT_CACHE_DIR = "data_cache"
_DEFAULT_RELEASE_ROOT = "data_releases"


def _current_code_version() -> str:
    """返回服务端代码版本,不接受网页传入的可伪造版本。"""

    configured = os.getenv("FINBOARD_CODE_VERSION")
    if configured:
        return configured
    result = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return "unknown"
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        check=False,
        capture_output=True,
        text=True,
    )
    return f"{value}-dirty" if dirty.returncode == 0 and dirty.stdout.strip() else value


def _release_detail_payload(release: Any) -> dict[str, object]:
    """把领域对象转换成详情响应,同时补齐列表摘要字段。"""
    # 这里不把摘要字段写入 manifest,避免改变已有发布的 checksum 契约。
    dataset_release = release
    payload = cast(dict[str, object], dataset_release.as_dict())
    payload.update(
        {
            "symbol_count": dataset_release.symbol_count,
            "row_count": dataset_release.row_count,
            "coverage_pct": dataset_release.coverage_pct,
        }
    )
    return payload


@router.get("", response_model=list[InstrumentOut])
async def list_instruments(
    market: str | None = Query(default=None),
    instrument_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_db_session),
) -> list[InstrumentOut]:
    """列出标的元数据(可按市场 / 类型过滤)。"""
    stmt = select(InstrumentModel)
    if market is not None:
        stmt = stmt.where(InstrumentModel.market == market)
    if instrument_type is not None:
        stmt = stmt.where(InstrumentModel.instrument_type == instrument_type)
    stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return [_instrument_to_out(r) for r in result.scalars().all()]


@router.get("/etf-summary", response_model=EtfMetadataSummaryOut)
async def etf_metadata_summary(
    session: AsyncSession = Depends(get_db_session),
) -> EtfMetadataSummaryOut:
    """ETF 元数据分类统计(各 review_status 数量 + 缺失数)。"""
    count_stmt = select(InstrumentModel).where(
        InstrumentModel.instrument_type == "etf"
    )
    total_etf = len(
        (await session.execute(count_stmt)).scalars().all()
    )
    summary = await EtfMetadataRepository(session).summary(total_etf)
    return EtfMetadataSummaryOut(
        total=summary.total,
        auto_adopted=summary.auto_adopted,
        needs_review=summary.needs_review,
        manually_confirmed=summary.manually_confirmed,
        manually_overridden=summary.manually_overridden,
        missing_metadata=summary.missing_metadata,
    )


@router.get("/etf-review", response_model=list[EtfMetadataOut])
async def etf_review_queue(
    review_status: str | None = Query(default="needs_review"),
    limit: int = Query(default=200, ge=1, le=2000),
    session: AsyncSession = Depends(get_db_session),
) -> list[EtfMetadataOut]:
    """ETF 元数据待复核队列(默认查 needs_review)。"""
    from finboard_shared.types import ReviewStatus

    status = ReviewStatus(review_status) if review_status else None
    rows = await EtfMetadataRepository(session).list_by_review_status(
        status, limit=limit
    )
    return [_etf_to_out(r) for r in rows]


@router.get("/{code}", response_model=InstrumentOut)
async def get_instrument(
    code: str,
    session: AsyncSession = Depends(get_db_session),
) -> InstrumentOut:
    stmt = select(InstrumentModel).where(InstrumentModel.code == code)
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到标的: {code}")
    return _instrument_to_out(row)


@router.get("/etf/{fund_code}", response_model=EtfMetadataOut | None)
async def get_etf_metadata(
    fund_code: str,
    session: AsyncSession = Depends(get_db_session),
) -> EtfMetadataOut | None:
    stmt = select(EtfMetadataModel).where(EtfMetadataModel.fund_code == fund_code)
    result = await session.execute(stmt)
    row = result.scalars().first()
    if row is None:
        return None
    return _etf_to_out(row)


@router.put("/etf/{code}", response_model=EtfMetadataOut)
async def update_etf_classification(
    code: str,
    request: EtfClassificationUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> EtfMetadataOut:
    """人工修正研究用 ETF 多维分类(issue #97)。

    写审计流水(操作者/时间/理由/前后值),设 ``manual_override=True``,
    后续自动同步不再覆盖。
    """

    normalized = code.strip().upper()
    instrument = (
        await session.execute(
            select(InstrumentModel).where(InstrumentModel.code == normalized)
        )
    ).scalar_one_or_none()
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"未找到标的: {normalized}")
    if instrument.instrument_type != "etf":
        raise HTTPException(
            status_code=409,
            detail=f"{normalized} 不是 ETF,不能写入 ETF 分类",
        )

    repo = EtfMetadataRepository(session)
    profile: EtfExecutionProfile | None = None
    if request.execution_profile is not None:
        profile = EtfExecutionProfile(request.execution_profile)
    row = await repo.apply_manual_override(
        normalized,
        execution_profile=profile,
        underlying_market=request.underlying_market,
        strategy_type=request.strategy_type,
        underlying_index=request.underlying_index,
        reason=request.reason,
    )
    await session.commit()
    return _etf_to_out(row)


@router.post("/etf-sync", response_model=EtfSyncPreviewOut)
async def etf_metadata_sync(
    request: EtfSyncRequest,
    session: AsyncSession = Depends(get_db_session),
) -> EtfSyncPreviewOut:
    """批量同步 ETF 元数据(akshare → 分类 → 写库)。

    ``dry_run=True`` 只预览不写入;``enrich_codes`` 指定的标的会额外
    拉取单基金档案补充跟踪标的 / 费率。
    """

    from finboard_data.assets import (
        AkShareEtfMetadataSource,
        EtfClassifier,
        EtfMetadataSync,
    )

    try:
        sync = EtfMetadataSync(AkShareEtfMetadataSource(), EtfClassifier())
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"数据源依赖未安装,无法同步 ETF 元数据: {exc}",
        ) from exc
    try:
        classifications = await sync.discover_and_classify(
            enrich_codes=request.enrich_codes or None,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ETF 元数据同步失败: {exc}",
        ) from exc

    repo = EtfMetadataRepository(session)
    if request.dry_run:
        preview = await repo.preview_upsert(classifications)
        return EtfSyncPreviewOut(
            total=preview.total,
            to_insert=preview.to_insert,
            to_update=preview.to_update,
            skipped_override=preview.skipped_override,
            needs_review=preview.needs_review,
            auto_adopted=preview.auto_adopted,
        )
    inserted, updated, skipped = await repo.upsert_batch(classifications)
    await session.commit()
    return EtfSyncPreviewOut(
        total=len(classifications),
        to_insert=inserted,
        to_update=updated,
        skipped_override=skipped,
        needs_review=sum(
            1 for c in classifications if c.review_status.value == "needs_review"
        ),
        auto_adopted=sum(
            1 for c in classifications if c.review_status.value == "auto_adopted"
        ),
    )


@router.post("/etf-batch-confirm", response_model=int)
async def etf_batch_confirm(
    request: EtfBatchConfirmRequest,
    session: AsyncSession = Depends(get_db_session),
) -> int:
    """批量确认待复核 ETF(needs_review → manually_confirmed)。"""
    confirmed = await EtfMetadataRepository(session).batch_confirm(
        request.codes, reason=request.reason
    )
    await session.commit()
    return confirmed


@router.get("/etf-audits/{code}", response_model=list[EtfAuditOut])
async def etf_audits(
    code: str,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[EtfAuditOut]:
    """查看 ETF 分类的审计流水(人工覆盖历史)。"""
    stmt = (
        select(EtfMetadataAuditModel)
        .where(EtfMetadataAuditModel.code == code.strip().upper())
        .order_by(EtfMetadataAuditModel.changed_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        EtfAuditOut(
            id=r.id,
            code=r.code,
            field_name=r.field_name,
            old_value=r.old_value,
            new_value=r.new_value,
            changed_by=r.changed_by,
            reason=r.reason,
            changed_at=r.changed_at,
        )
        for r in rows
    ]


@router.get("/bond/{code}", response_model=BondMetadataOut | None)
async def get_bond_metadata(
    code: str,
    session: AsyncSession = Depends(get_db_session),
) -> BondMetadataOut | None:
    stmt = select(BondMetadataModel).where(BondMetadataModel.code == code)
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _bond_to_out(row)


@router.get("/convertible/{code}", response_model=ConvertibleMetadataOut | None)
async def get_convertible_metadata(
    code: str,
    session: AsyncSession = Depends(get_db_session),
) -> ConvertibleMetadataOut | None:
    stmt = select(ConvertibleMetadataModel).where(ConvertibleMetadataModel.code == code)
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _convertible_to_out(row)


@router.get("/futures/{series_id}", response_model=list[FuturesContractOut])
async def list_futures_contracts(
    series_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> list[FuturesContractOut]:
    """列出某品种的所有期货合约。"""
    stmt = select(FuturesContractModel).where(
        FuturesContractModel.series_id == series_id.upper()
    )
    result = await session.execute(stmt)
    return [_futures_to_out(r) for r in result.scalars().all()]


@router.get("/lifecycle/{symbol}", response_model=list[LifecycleEventOut])
async def list_lifecycle_events(
    symbol: str,
    event_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[LifecycleEventOut]:
    """列出标的的生命周期事件(分红 / 强赎 / 换月 ...)。"""
    stmt = select(InstrumentLifecycleEventModel).where(
        InstrumentLifecycleEventModel.symbol == symbol
    )
    if event_type is not None:
        stmt = stmt.where(InstrumentLifecycleEventModel.event_type == event_type)
    stmt = stmt.order_by(InstrumentLifecycleEventModel.effective_date.desc()).limit(limit)
    result = await session.execute(stmt)
    return [_lifecycle_to_out(r) for r in result.scalars().all()]


@router.get("/datasets/manifests", response_model=list[DatasetManifestOut])
async def list_dataset_manifests(
    dataset_name: str | None = Query(default=None),
    quality_status: Literal[
        "unknown", "pending", "passed", "warnings", "failed"
    ]
    | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> list[DatasetManifestOut]:
    """列出数据集发布清单。"""
    stmt = select(DatasetManifestModel)
    if dataset_name is not None:
        stmt = stmt.where(DatasetManifestModel.dataset_name == dataset_name)
    if quality_status is not None:
        stmt = stmt.where(DatasetManifestModel.quality_status == quality_status)
    stmt = stmt.order_by(DatasetManifestModel.published_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return [_manifest_to_out(r) for r in result.scalars().all()]


@router.get(
    "/datasets/releases",
    response_model=list[ResearchDatasetReleaseSummaryOut],
)
async def list_dataset_releases(
    dataset_name: str | None = Query(default=None),
    source: str | None = Query(default=None),
    quality_status: Literal["passed", "warnings"] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> list[ResearchDatasetReleaseSummaryOut]:
    """列出版本化、时点安全的不可变研究数据发布。"""

    releases = await ResearchDatasetReleaseRepository(session).list(
        dataset_name=dataset_name,
        source=source,
        quality_status=quality_status,
        limit=limit,
    )
    return [
        ResearchDatasetReleaseSummaryOut(
            release_id=release.release_id,
            dataset_name=release.dataset_name,
            source=release.source,
            version=release.version,
            schema_version=release.schema_version,
            start_date=release.start_date,
            end_date=release.end_date,
            period=release.period.value,
            adjustment=release.adjustment,
            dataset_kind=release.dataset_kind.value,
            code_version=release.code_version,
            published_at=release.published_at,
            symbol_count=release.symbol_count,
            row_count=release.row_count,
            coverage_pct=release.coverage_pct,
            capabilities=[
                DatasetReleaseCapabilityOut(
                    key=item.key,
                    status=item.status.value,
                    symbol_count=item.symbol_count,
                    ready_count=item.ready_count,
                    missing_requirements=list(item.missing_requirements),
                )
                for item in release.capabilities
            ],
            quality_status=release.quality_status.value,
            known_limitations=list(release.known_limitations),
            metadata_version=release.metadata_version,
            release_checksum=release.release_checksum,
        )
        for release in releases
    ]


@router.post(
    "/datasets/releases",
    response_model=JobOut,
    status_code=202,
)
async def create_dataset_release(
    request: ResearchDatasetReleaseCreate,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    """登记数据集冻结发布任务,立即返回 202 + job_id(issue #144)。

    实际执行(原子 rename + DB 登记 + 标的资产类型校验)由 worker 消费
    ``kind=dataset_publish`` 任务。发布成功后 ``JobOut.result_ref = release_id``;
    前端需轮询 ``/api/jobs/{job_id}`` 拿到 release_id 后再查发布详情。
    """

    from finboard_api.job_helpers import enqueue_job

    payload: dict[str, Any] = {
        "release_id": request.release_id,
        "dataset_name": request.dataset_name,
        "release_kind": request.release_kind,
        "version": request.version,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "adjustment": request.adjustment,
        "symbols": list(request.symbols),
        "required_capabilities": list(request.required_capabilities),
    }
    idempotency_key = f"publish:{request.release_id}"
    try:
        job = await enqueue_job(
            session,
            response,
            kind="dataset_publish",
            queue="data",
            idempotency_key=idempotency_key,
            payload=payload,
            requested_by="api:dataset_publish",
        )
        await session.commit()
    except BackgroundJobPersistenceConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job


@router.get(
    "/datasets/releases/{release_id}",
    response_model=ResearchDatasetReleaseOut,
)
async def get_dataset_release(
    release_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> ResearchDatasetReleaseOut:
    """读取发布清单、逐标的覆盖、资产规则及能力缺口。"""

    release = await ResearchDatasetReleaseRepository(session).get(release_id)
    if release is None:
        raise HTTPException(status_code=404, detail=f"未找到研究数据发布: {release_id}")
    return ResearchDatasetReleaseOut.model_validate(_release_detail_payload(release))


def _instrument_to_out(row: InstrumentModel) -> InstrumentOut:
    return InstrumentOut(
        code=row.code,
        name=row.name,
        market=row.market,
        instrument_type=row.instrument_type,
        exchange=row.exchange,
        list_date=row.list_date,
        delist_date=row.delist_date,
        status=row.status,
        sector=row.sector,
        industry=row.industry,
    )


def _etf_to_out(row: EtfMetadataModel) -> EtfMetadataOut:
    return EtfMetadataOut(
        code=row.code,
        fund_code=row.fund_code,
        category=row.category,
        execution_profile=row.execution_profile,
        underlying_market=row.underlying_market,
        strategy_type=row.strategy_type,
        underlying_index=row.underlying_index,
        underlying_asset_class=row.underlying_asset_class,
        management_fee_rate=row.management_fee_rate,
        custody_fee_rate=row.custody_fee_rate,
        tracking_error=row.tracking_error,
        inception_date=row.inception_date,
        listing_date=row.listing_date,
        delisting_date=row.delisting_date,
        iopv_available=row.iopv_available,
        allows_t_plus_0=row.allows_t_plus_0,
        dividend_policy=row.dividend_policy,
        source=row.source,
        rule_version=row.rule_version,
        confidence=row.confidence,
        review_status=row.review_status,
        evidence=[str(e) for e in (row.evidence or [])],
        manual_override=row.manual_override,
    )


def _bond_to_out(row: BondMetadataModel) -> BondMetadataOut:
    return BondMetadataOut(
        code=row.code,
        face_value=row.face_value,
        coupon_rate=row.coupon_rate,
        coupon_frequency=row.coupon_frequency,
        issue_date=row.issue_date,
        maturity_date=row.maturity_date,
        issuer=row.issuer,
        credit_rating=row.credit_rating,
        credit_entity_type=row.credit_entity_type,
        duration_years=row.duration_years,
        yield_to_maturity=row.yield_to_maturity,
    )


def _convertible_to_out(row: ConvertibleMetadataModel) -> ConvertibleMetadataOut:
    return ConvertibleMetadataOut(
        code=row.code,
        underlying_stock_code=row.underlying_stock_code,
        conversion_price=row.conversion_price,
        conversion_ratio=row.conversion_ratio,
        conversion_premium=row.conversion_premium,
        issue_date=row.issue_date,
        maturity_date=row.maturity_date,
        coupon_schedule=row.coupon_schedule,
        redemption_yield=row.redemption_yield,
        forced_redeem_trigger=row.forced_redeem_trigger,
        put_back_trigger=row.put_back_trigger,
        downward_revision_trigger=row.downward_revision_trigger,
    )


def _futures_to_out(row: FuturesContractModel) -> FuturesContractOut:
    return FuturesContractOut(
        contract_code=row.contract_code,
        series_id=row.series_id,
        underlying_symbol=row.underlying_symbol,
        exchange=row.exchange,
        multiplier=row.multiplier,
        margin_rate=row.margin_rate,
        price_limit_pct=row.price_limit_pct,
        price_tick=row.price_tick,
        listing_date=row.listing_date,
        last_trade_date=row.last_trade_date,
        delivery_date=row.delivery_date,
        delivery_method=row.delivery_method,
        settle_price=row.settle_price,
        open_interest=row.open_interest,
    )


def _lifecycle_to_out(row: InstrumentLifecycleEventModel) -> LifecycleEventOut:
    return LifecycleEventOut(
        id=row.id,
        symbol=row.symbol,
        event_type=row.event_type,
        effective_date=row.effective_date,
        available_at=row.available_at,
        source=row.source,
        dataset_version=row.dataset_version,
        details=row.details,
    )


def _manifest_to_out(row: DatasetManifestModel) -> DatasetManifestOut:
    return DatasetManifestOut(
        id=row.id,
        dataset_name=row.dataset_name,
        source=row.source,
        version=row.version,
        start_date=row.start_date,
        end_date=row.end_date,
        row_count=row.row_count,
        symbol_count=row.symbol_count,
        coverage_pct=row.coverage_pct,
        gaps=row.gaps,
        checksum=row.checksum,
        quality_status=row.quality_status,
        quality_report=row.quality_report,
        published_at=row.published_at,
        code_version=row.code_version,
    )
