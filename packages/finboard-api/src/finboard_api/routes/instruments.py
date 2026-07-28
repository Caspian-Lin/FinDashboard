"""多资产元数据端点(issue #58)。

提供 ETF / 可转债 / 国债 / 期货合约的元数据查询,以及数据集覆盖率审计。
本端点只读,不触及交易红线。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_session
from finboard_api.schemas import (
    BondMetadataOut,
    ConvertibleMetadataOut,
    DatasetManifestOut,
    EtfMetadataOut,
    FuturesContractOut,
    InstrumentOut,
    LifecycleEventOut,
)
from finboard_persistence import (
    BondMetadataModel,
    ConvertibleMetadataModel,
    DatasetManifestModel,
    EtfMetadataModel,
    FuturesContractModel,
    InstrumentLifecycleEventModel,
    InstrumentModel,
)

router = APIRouter(prefix="/api/instruments", tags=["instruments"])


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
