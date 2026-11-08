"""全局数据目录 REST(issue #509)。

只读:数据集列表 / 详情 / 覆盖 / 批次 / 发布头 / 逐标的 / 标的反查 /
引用 / 交易日历。目录不触发同步或发布,所有写操作仍走既有入口。
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.schemas import BaseSchema
from finboard_backtest.data_catalog import (
    CatalogError,
    CatalogQuery,
    DataCatalogService,
)
from finboard_backtest.data_catalog import contracts as catalog_contracts
from finboard_data.datasets import CatalogDataset
from finboard_persistence.catalog_repo import (
    BatchHeader,
    PresenceRow,
    ReleaseHeader,
    ReleaseInstrumentRow,
    ReleaseReferenceRow,
)

router = APIRouter(prefix="/api/data/catalog", tags=["data-catalog"])


# ------------------------------------------------------------------ 响应模型
class CatalogReleaseOut(BaseSchema):
    release_id: str
    dataset_name: str
    source: str
    version: str
    dataset_kind: str
    schema_version: str
    start_date: date
    end_date: date
    adjustment: str
    symbol_count: int
    row_count: int
    coverage_pct: float
    quality_status: str
    published_at: datetime
    release_checksum: str
    storage_uri: str
    capabilities: list[dict[str, Any]]


class CatalogBatchOut(BaseSchema):
    id: int
    dataset: str
    source: str
    dataset_version: str
    status: str
    quality_status: str | None
    expected_rows: int | None
    received_rows: int
    accepted_rows: int
    rejected_rows: int
    parameters: dict[str, Any]
    error_summary: str | None
    started_at: datetime
    completed_at: datetime | None
    published_at: datetime | None


class CatalogEntryOut(BaseSchema):
    key: str
    title: str
    title_en: str
    storage: str
    publishable: bool
    source: str | None
    status: str
    release: CatalogReleaseOut | None
    batch: CatalogBatchOut | None
    coverage_pct: float | None
    symbol_count: int | None
    row_count: int | None
    denominator: str
    local_start: date | None
    local_end: date | None
    updated_at: datetime | None
    cache_files: int
    cache_bytes: int
    warnings: list[str]


class CatalogPageOut(BaseSchema):
    items: list[CatalogEntryOut]
    total: int
    limit: int
    offset: int
    generated_at: datetime


class DatasetDetailOut(BaseSchema):
    entry: CatalogEntryOut
    fields: list[str]
    unit_notes: list[str]
    pit_rule: str
    calendar_market: str
    adjustment: str | None
    publishable: bool
    releases: list[CatalogReleaseOut]
    batches: list[CatalogBatchOut]
    failures: list[CatalogBatchOut]


class InstrumentCoverageOut(BaseSchema):
    code: str
    name: str
    market: str
    instrument_type: str
    asset_class: str
    exchange: str | None
    listing_board: str
    start_date: date
    end_date: date
    row_count: int
    expected_sessions: int
    missing_sessions: int
    suspended_sessions: int
    anomaly_count: int
    duplicate_count: int
    coverage_pct: float
    ready: bool
    issues: list[str]
    list_date: date | None
    delist_date: date | None
    status: str


class CoverageOut(BaseSchema):
    dataset: str
    denominator: str
    release: CatalogReleaseOut | None
    batch: CatalogBatchOut | None
    coverage_pct: float | None
    expected_sessions: int
    missing_sessions: int
    symbols_with_gaps: int
    missing_preview: list[InstrumentCoverageOut]
    warnings: list[str]


class InstrumentPageOut(BaseSchema):
    release_id: str
    items: list[InstrumentCoverageOut]
    total: int
    limit: int
    offset: int


class PresenceItemOut(BaseSchema):
    release_id: str
    dataset_kind: str
    version: str
    quality_status: str
    published_at: datetime
    start_date: date
    end_date: date
    coverage_pct: float
    ready: bool


class PresencePageOut(BaseSchema):
    code: str
    items: list[PresenceItemOut]
    total: int
    limit: int
    offset: int
    warnings: list[str]


class ReferenceOut(BaseSchema):
    kind: str
    ref_id: str
    status: str
    created_at: datetime | None
    deep_link: str | None


class ReferencePageOut(BaseSchema):
    release_id: str
    items: list[ReferenceOut]


class CalendarOut(BaseSchema):
    market: str
    start: date
    end: date
    days: list[date]
    source: str


# ------------------------------------------------------------------ 服务
def _service(session: AsyncSession) -> DataCatalogService:
    return DataCatalogService(
        session,
        cache_dir=Path(os.getenv("FINBOARD_DATA_CACHE_DIR", "data_cache")),
        release_root=Path(
            os.getenv("FINBOARD_DATA_RELEASE_ROOT", "data_releases")
        ),
    )


def _release_out(header: ReleaseHeader) -> CatalogReleaseOut:
    return CatalogReleaseOut(
        release_id=header.release_id,
        dataset_name=header.dataset_name,
        source=header.source,
        version=header.version,
        dataset_kind=header.dataset_kind.value,
        schema_version=header.schema_version,
        start_date=header.start_date,
        end_date=header.end_date,
        adjustment=header.adjustment,
        symbol_count=header.symbol_count,
        row_count=header.row_count,
        coverage_pct=float(header.coverage_pct),
        quality_status=header.quality_status,
        published_at=header.published_at,
        release_checksum=header.release_checksum,
        storage_uri=header.storage_uri,
        capabilities=[dict(item) for item in header.capabilities],
    )


def _batch_out(batch: BatchHeader) -> CatalogBatchOut:
    return CatalogBatchOut(
        id=batch.id,
        dataset=batch.dataset,
        source=batch.source,
        dataset_version=batch.dataset_version,
        status=batch.status,
        quality_status=batch.quality_status,
        expected_rows=batch.expected_rows,
        received_rows=batch.received_rows,
        accepted_rows=batch.accepted_rows,
        rejected_rows=batch.rejected_rows,
        parameters=dict(batch.parameters),
        error_summary=batch.error_summary,
        started_at=batch.started_at,
        completed_at=batch.completed_at,
        published_at=batch.published_at,
    )


def _entry_out(entry: catalog_contracts.CatalogEntry) -> CatalogEntryOut:
    return CatalogEntryOut(
        key=entry.key.value,
        title=entry.title,
        title_en=entry.title_en,
        storage=entry.storage.value,
        publishable=entry.publishable,
        source=entry.source,
        status=entry.status,
        release=_release_out(entry.release) if entry.release else None,
        batch=_batch_out(entry.batch) if entry.batch else None,
        coverage_pct=(
            float(entry.coverage_pct) if entry.coverage_pct is not None else None
        ),
        symbol_count=entry.symbol_count,
        row_count=entry.row_count,
        denominator=entry.denominator,
        local_start=entry.local_start,
        local_end=entry.local_end,
        updated_at=entry.updated_at,
        cache_files=entry.cache_files,
        cache_bytes=entry.cache_bytes,
        warnings=list(entry.warnings),
    )


def _instrument_out(row: ReleaseInstrumentRow) -> InstrumentCoverageOut:
    return InstrumentCoverageOut(
        code=row.code,
        name=row.name,
        market=row.market,
        instrument_type=row.instrument_type,
        asset_class=row.asset_class,
        exchange=row.exchange,
        listing_board=row.listing_board,
        start_date=row.start_date,
        end_date=row.end_date,
        row_count=row.row_count,
        expected_sessions=row.expected_sessions,
        missing_sessions=row.missing_sessions,
        suspended_sessions=row.suspended_sessions,
        anomaly_count=row.anomaly_count,
        duplicate_count=row.duplicate_count,
        coverage_pct=float(row.coverage_pct),
        ready=row.ready,
        issues=list(row.issues),
        list_date=row.list_date,
        delist_date=row.delist_date,
        status=row.status,
    )


def _presence_out(row: PresenceRow) -> PresenceItemOut:
    return PresenceItemOut(
        release_id=row.release_id,
        dataset_kind=row.dataset_kind.value,
        version=row.version,
        quality_status=row.quality_status,
        published_at=row.published_at,
        start_date=row.start_date,
        end_date=row.end_date,
        coverage_pct=float(row.coverage_pct),
        ready=row.ready,
    )


def _raise_catalog(exc: CatalogError) -> None:
    status_code = 404 if exc.code == "release_not_found" else 409
    raise HTTPException(status_code=status_code, detail=f"{exc.code}: {exc.message}")


# ------------------------------------------------------------------ 路由
@router.get("", response_model=CatalogPageOut)
async def list_catalog(
    dataset: CatalogDataset | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> CatalogPageOut:
    service = _service(session)
    items, total = await service.list_entries(
        CatalogQuery(dataset=dataset, status=status, q=q, limit=limit, offset=offset)
    )
    return CatalogPageOut(
        items=[_entry_out(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
        generated_at=datetime.now(UTC),
    )


@router.get("/datasets/{dataset}", response_model=DatasetDetailOut)
async def get_dataset(
    dataset: CatalogDataset,
    session: AsyncSession = Depends(get_db_session),
) -> DatasetDetailOut:
    detail = await _service(session).get_dataset(dataset)
    return DatasetDetailOut(
        entry=_entry_out(detail.entry),
        fields=list(detail.spec.fields),
        unit_notes=list(detail.spec.unit_notes),
        pit_rule=detail.spec.pit_rule,
        calendar_market=detail.spec.calendar_market,
        adjustment=detail.adjustment,
        publishable=detail.spec.publishable,
        releases=[_release_out(item) for item in detail.releases],
        batches=[_batch_out(item) for item in detail.batches],
        failures=[_batch_out(item) for item in detail.failures],
    )


@router.get("/datasets/{dataset}/coverage", response_model=CoverageOut)
async def get_coverage(
    dataset: CatalogDataset,
    release_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> CoverageOut:
    report = await _service(session).get_coverage(
        dataset, release_id=release_id, limit=limit
    )
    return CoverageOut(
        dataset=report.dataset.value,
        denominator=report.denominator,
        release=_release_out(report.release) if report.release else None,
        batch=_batch_out(report.batch) if report.batch else None,
        coverage_pct=(
            float(report.coverage_pct) if report.coverage_pct is not None else None
        ),
        expected_sessions=report.expected_sessions,
        missing_sessions=report.missing_sessions,
        symbols_with_gaps=report.symbols_with_gaps,
        missing_preview=[
            _instrument_out(item) for item in report.missing_preview
        ],
        warnings=list(report.warnings),
    )


@router.get("/datasets/{dataset}/batches", response_model=list[CatalogBatchOut])
async def list_batches(
    dataset: CatalogDataset,
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> list[CatalogBatchOut]:
    rows, _ = await _service(session).list_batches(
        dataset, status=status, limit=limit, offset=offset
    )
    return [_batch_out(item) for item in rows]


@router.get("/releases/{release_id}", response_model=CatalogReleaseOut)
async def get_release(
    release_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> CatalogReleaseOut:
    header = await _service(session).release_header(release_id)
    if header is None:
        raise HTTPException(status_code=404, detail=f"未找到研究数据发布: {release_id}")
    return _release_out(header)


@router.get(
    "/releases/{release_id}/instruments",
    response_model=InstrumentPageOut,
)
async def list_release_instruments(
    release_id: str,
    ready: bool | None = Query(default=None),
    missing_only: bool = Query(default=False),
    market: str | None = Query(default=None),
    instrument_type: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> InstrumentPageOut:
    try:
        page = await _service(session).list_release_instruments(
            release_id,
            ready=ready,
            missing_only=missing_only,
            market=market,
            instrument_type=instrument_type,
            q=q,
            limit=limit,
            offset=offset,
        )
    except CatalogError as exc:
        _raise_catalog(exc)
    return InstrumentPageOut(
        release_id=page.release_id,
        items=[_instrument_out(item) for item in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
    )


@router.get("/releases/{release_id}/references", response_model=ReferencePageOut)
async def list_release_references(
    release_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> ReferencePageOut:
    try:
        page = await _service(session).list_release_references(
            release_id, limit=limit
        )
    except CatalogError as exc:
        _raise_catalog(exc)
    items = [
        _reference_out(item, page.deep_links.get(f"{item.kind}:{item.ref_id}"))
        for item in page.items
    ]
    return ReferencePageOut(release_id=release_id, items=items)


@router.get("/instruments/{code}", response_model=PresencePageOut)
async def find_instrument(
    code: str,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> PresencePageOut:
    page = await _service(session).find_instrument_presence(
        code, start=start, end=end, limit=limit, offset=offset
    )
    return PresencePageOut(
        code=page.code,
        items=[_presence_out(item) for item in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
        warnings=list(page.warnings),
    )


@router.get("/calendar", response_model=CalendarOut)
async def get_calendar(
    market: str = Query(default="SSE"),
    start: date = Query(...),
    end: date = Query(...),
    session: AsyncSession = Depends(get_db_session),
) -> CalendarOut:
    view = await _service(session).get_calendar(market, start, end)
    return CalendarOut(
        market=view.market,
        start=view.start,
        end=view.end,
        days=list(view.days),
        source=view.source,
    )


def _reference_out(
    item: ReleaseReferenceRow, deep_link: str | None
) -> ReferenceOut:
    return ReferenceOut(
        kind=item.kind,
        ref_id=item.ref_id,
        status=item.status,
        created_at=item.created_at,
        deep_link=deep_link,
    )


__all__ = ["router"]
