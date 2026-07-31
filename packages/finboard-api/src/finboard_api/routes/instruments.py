"""多资产元数据端点(issue #58)。

提供 ETF / 可转债 / 国债 / 期货合约的元数据查询,以及数据集覆盖率审计。
本端点只读,不触及交易红线。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_session
from finboard_api.schemas import (
    BondMetadataOut,
    ConvertibleMetadataOut,
    DatasetManifestOut,
    DatasetReleaseCapabilityOut,
    EtfClassificationUpdate,
    EtfMetadataOut,
    FuturesContractOut,
    InstrumentOut,
    LifecycleEventOut,
    ResearchDatasetReleaseCreate,
    ResearchDatasetReleaseOut,
    ResearchDatasetReleaseSummaryOut,
)
from finboard_persistence import (
    BondMetadataModel,
    ConvertibleMetadataModel,
    DatasetManifestModel,
    EtfMetadataModel,
    FuturesContractModel,
    InstrumentLifecycleEventModel,
    InstrumentModel,
    ResearchDatasetReleaseRepository,
)

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


@router.get("", response_model=list[InstrumentOut])
async def list_instruments(
    market: str | None = Query(default=None),
    instrument_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
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


@router.get("/{code}", response_model=InstrumentOut)
async def get_instrument(
    code: str,
    session: AsyncSession = Depends(get_session),
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
    session: AsyncSession = Depends(get_session),
) -> EtfMetadataOut | None:
    stmt = select(EtfMetadataModel).where(EtfMetadataModel.fund_code == fund_code)
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _etf_to_out(row)


_ETF_CLASSIFICATION_DEFAULTS: dict[str, tuple[str, bool]] = {
    "equity": ("equity", False),
    "index": ("equity", False),
    "cross_border": ("equity", True),
    "bond": ("fixed_income", False),
    "money_market": ("cash", True),
    "commodity": ("commodity", True),
}


@router.put("/etf/{code}", response_model=EtfMetadataOut)
async def update_etf_classification(
    code: str,
    request: EtfClassificationUpdate,
    session: AsyncSession = Depends(get_session),
) -> EtfMetadataOut:
    """补齐或修正研究用 ETF 分类,不修改实盘交易配置。"""

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

    row = (
        await session.execute(
            select(EtfMetadataModel).where(EtfMetadataModel.code == normalized)
        )
    ).scalar_one_or_none()
    asset_class, allows_t_plus_0 = _ETF_CLASSIFICATION_DEFAULTS[request.category]
    if row is None:
        row = EtfMetadataModel(
            code=normalized,
            fund_code=normalized.split(".", 1)[0],
            category=request.category,
            underlying_index=request.underlying_index,
            underlying_asset_class=asset_class,
            iopv_available=False,
            allows_t_plus_0=allows_t_plus_0,
            dividend_policy="cash",
            source="manual",
            dataset_version=f"manual-{date.today().isoformat()}",
        )
        session.add(row)
    else:
        row.category = request.category
        row.underlying_index = request.underlying_index
        row.underlying_asset_class = asset_class
        row.allows_t_plus_0 = allows_t_plus_0
        row.source = "manual"
        row.dataset_version = f"manual-{date.today().isoformat()}"

    await session.flush()
    await session.commit()
    return _etf_to_out(row)


@router.get("/bond/{code}", response_model=BondMetadataOut | None)
async def get_bond_metadata(
    code: str,
    session: AsyncSession = Depends(get_session),
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
    session: AsyncSession = Depends(get_session),
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
    session: AsyncSession = Depends(get_session),
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
    session: AsyncSession = Depends(get_session),
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
    session: AsyncSession = Depends(get_session),
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
    session: AsyncSession = Depends(get_session),
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
    response_model=ResearchDatasetReleaseOut,
    status_code=201,
)
async def create_dataset_release(
    request: ResearchDatasetReleaseCreate,
    session: AsyncSession = Depends(get_session),
) -> ResearchDatasetReleaseOut:
    """把选定范围的本地 Parquet 缓存冻结为不可变研究数据版本。"""

    from finboard_data import (
        DatasetReleaseError,
        DatasetReleaseSpec,
        ImmutableReleaseError,
    )
    from finboard_persistence import ResearchDatasetReleaseService

    cache_dir = Path(os.getenv("FINBOARD_DATA_CACHE_DIR", _DEFAULT_CACHE_DIR))
    release_root = Path(
        os.getenv("FINBOARD_DATA_RELEASE_ROOT", _DEFAULT_RELEASE_ROOT)
    )
    source = request.source or os.getenv("FINBOARD_DATA_PROVIDER", "akshare")
    if source not in {"akshare", "yfinance", "tushare", "manual"}:
        raise HTTPException(
            status_code=422,
            detail=f"不支持的数据来源: {source}",
        )

    service = ResearchDatasetReleaseService(
        session,
        cache_dir=cache_dir,
        release_root=release_root,
    )
    try:
        release = await service.publish(
            DatasetReleaseSpec(
                release_id=request.release_id,
                dataset_name=request.dataset_name,
                source=source,
                version=request.version,
                start_date=request.start_date,
                end_date=request.end_date,
                code_version=_current_code_version(),
                adjustment=request.adjustment,
                required_capabilities=tuple(request.required_capabilities),
                known_limitations=(
                    "交易日覆盖使用工作日近似;节假日缺口作为 warning 报告",
                    "停牌优先使用停复牌生命周期事件;缺少事件时仅能由零成交且 OHLC 不变的日线代理识别",
                    "只冻结本地缓存已有字段,不会回退到联网数据源",
                ),
            ),
            request.symbols,
        )
        await session.commit()
    except ImmutableReleaseError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"发布身份冲突: {exc}") from exc
    except (DatasetReleaseError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=f"数据质量门未通过: {exc}") from exc

    return ResearchDatasetReleaseOut.model_validate(release.as_dict())


@router.get(
    "/datasets/releases/{release_id}",
    response_model=ResearchDatasetReleaseOut,
)
async def get_dataset_release(
    release_id: str,
    session: AsyncSession = Depends(get_session),
) -> ResearchDatasetReleaseOut:
    """读取发布清单、逐标的覆盖、资产规则及能力缺口。"""

    release = await ResearchDatasetReleaseRepository(session).get(release_id)
    if release is None:
        raise HTTPException(status_code=404, detail=f"未找到研究数据发布: {release_id}")
    return ResearchDatasetReleaseOut.model_validate(release.as_dict())


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
