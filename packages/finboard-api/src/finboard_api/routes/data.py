"""历史行情数据管理端点。"""

from __future__ import annotations

import asyncio
from datetime import date as parse_date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api._preview import (
    MAX_PREVIEW_LIMIT,
    read_parquet_tail,
    validate_preview_symbol,
)
from finboard_api.deps import get_db_session
from finboard_api.job_schemas import JobOut
from finboard_api.schemas import (
    BarAnomalyOut,
    BulkDownloadRequest,
    DataFetchRequest,
    DataPreviewOut,
    DataStatusListOut,
    DataStatusOut,
    DataStatusSelectionOut,
    FetchResultOut,
    InstrumentListOut,
    InstrumentOut,
    InstrumentSummaryOut,
    QualityRepairRequest,
    QualityReportOut,
    SchedulerConfigOut,
    SchedulerConfigUpdate,
    SymbolEntrySchema,
    SymbolPoolOut,
    SymbolPoolUpdate,
    TushareQuotaOut,
)
from finboard_persistence import BackgroundJobPersistenceConflictError

logger = structlog.get_logger(__name__)


if TYPE_CHECKING:
    from finboard_app.config import Settings
    from finboard_data import AkShareProvider, TushareBarProvider, YFinanceProvider

router = APIRouter(prefix="/api/data", tags=["data"])

_CACHE_DIR = "data_cache"
_SYMBOLS_FILE = "symbols.yaml"
_CONFIG_FILE = "data_config.json"
_SUPPORTED_BAR_PROVIDERS = {"akshare", "tushare", "yfinance"}

def _request_settings(request: Request) -> Settings | None:
    """完整应用有 settings;最小化路由测试允许缺省。"""
    return getattr(request.app.state, "settings", None)


def _get_provider(
    source: str | None = None,
    *,
    use_cache: bool = True,
    settings: Settings | None = None,
) -> AkShareProvider | TushareBarProvider | YFinanceProvider:
    from finboard_data import AkShareProvider, TushareBarProvider, YFinanceProvider

    provider_name = _resolve_provider_name(source, settings=settings)
    if provider_name == "akshare":
        return AkShareProvider(use_cache=use_cache)
    if provider_name == "tushare":
        return TushareBarProvider(
            token=settings.tushare_token if settings is not None else None,
            use_cache=use_cache,
            requests_per_minute=(
                settings.tushare_requests_per_minute if settings is not None else 200
            ),
            daily_request_limit=(
                settings.tushare_daily_request_limit if settings is not None else 100_000
            ),
            usage_file=(
                settings.tushare_usage_file
                if settings is not None
                else "data_cache/tushare_usage.json"
            ),
        )
    return YFinanceProvider(use_cache=use_cache)


def _resolve_provider_name(
    source: str | None = None,
    *,
    settings: Settings | None = None,
) -> str:
    """解析并校验行情源,避免未知值静默落到 yfinance。"""
    import os

    configured = settings.data_provider if settings is not None else None
    # 缺省主源 tushare(issue #393):股票 bars 主源切换,akshare 降副源。
    provider_name = (
        (source or configured or os.getenv("FINBOARD_DATA_PROVIDER") or "tushare").strip().lower()
    )
    if provider_name not in _SUPPORTED_BAR_PROVIDERS:
        supported = ", ".join(sorted(_SUPPORTED_BAR_PROVIDERS))
        raise ValueError(f"不支持的行情源: {provider_name}; 可用: {supported}")
    return provider_name


def _fallback_provider_name(
    primary: str,
    *,
    settings: Settings | None = None,
) -> str | None:
    """选择备用行情源;可用环境变量 FINBOARD_DATA_FALLBACK_PROVIDER 覆盖。"""
    import os

    configured = (
        settings.data_fallback_provider
        if settings is not None
        else os.getenv("FINBOARD_DATA_FALLBACK_PROVIDER", "").strip().lower()
    )
    fallback = configured or ("yfinance" if primary == "akshare" else "akshare")
    if fallback not in _SUPPORTED_BAR_PROVIDERS or fallback == primary:
        return None
    return fallback




async def _store_fetched_bars(
    cache: Any,
    symbol: Any,
    period: Any,
    adjust: str,
    bars: list[Any],
    source: str,
) -> None:
    """写入行情并避免把不同数据源静默混进同一个可发布缓存。"""
    if not bars:
        return
    existing = await cache.read(symbol, period, adjust)
    known_sources = {bar.source for bar in existing if bar.source}
    if known_sources and known_sources != {source}:
        await cache.write(symbol, period, adjust, bars)
        return
    await cache.merge(
        symbol,
        period,
        adjust,
        bars,
        existing_bars=existing,
    )


async def _persist_tushare_lifecycle_events(
    session: AsyncSession,
    events: list[Any],
) -> int:
    """幂等写入 Tushare 停复牌事件,返回本次新增数量。

    实现收敛到 finboard_persistence 单一事实源(#393):与 bulk_download
    执行器 / MCP fetch 共用同一行形状与幂等键。
    """
    from finboard_persistence import persist_tushare_lifecycle_events

    return await persist_tushare_lifecycle_events(session, events)


@router.get("/status", response_model=list[DataStatusOut])
async def list_cache_status() -> list[DataStatusOut]:
    """列出缓存状态;只读 Parquet footer,不扫描行情列。"""
    from finboard_data.cache import ParquetCache

    cache = ParquetCache(_CACHE_DIR, max_io_concurrency=8)
    cache_path = Path(_CACHE_DIR)
    parquet_files = await asyncio.to_thread(lambda: list(cache_path.glob("*.parquet")))

    result: list[DataStatusOut] = []
    for f in parquet_files:
        parts = f.stem.rsplit("_", 2)
        if len(parts) != 3:
            continue
        code, period_str, adjust = parts
        metadata = await cache.metadata(f)
        result.append(
            DataStatusOut(
                symbol=code,
                period=period_str,
                adjust=adjust,
                bar_count=metadata.bar_count,
                first_date=(str(metadata.first_date) if metadata.first_date else None),
                last_date=str(metadata.last_date) if metadata.last_date else None,
                last_close=None,
                source=metadata.source,
            )
        )
    return result


@router.get("/status-page", response_model=DataStatusListOut)
async def list_cache_status_page(
    limit: int = 200,
    offset: int = 0,
    q: str | None = None,
    period: str | None = None,
    adjust: str | None = None,
    listing_board: list[str] | None = Query(default=None),
) -> DataStatusListOut:
    """分页列出缓存状态,避免一次扫描并返回全市场缓存。"""
    from finboard_data.cache import ParquetCache
    from finboard_data.discovery import infer_a_share_listing_board

    safe_limit = min(max(limit, 1), 500)
    safe_offset = max(offset, 0)
    cache = ParquetCache(_CACHE_DIR, max_io_concurrency=8)
    cache_path = Path(_CACHE_DIR)
    parquet_files = sorted(await asyncio.to_thread(lambda: list(cache_path.glob("*.parquet"))))
    query = q.strip().upper() if q else None

    def matches(path: Path) -> bool:
        parts = path.stem.rsplit("_", 2)
        if len(parts) != 3:
            return False
        code, period_str, adjustment = parts
        board = infer_a_share_listing_board(code).value
        return (
            (query is None or query in code.upper())
            and (period is None or period_str == period)
            and (adjust is None or adjustment == adjust)
            and (not listing_board or board in listing_board)
        )

    matching_files = [path for path in parquet_files if matches(path)]

    items: list[DataStatusOut] = []
    for path in matching_files[safe_offset : safe_offset + safe_limit]:
        parts = path.stem.rsplit("_", 2)
        if len(parts) != 3:
            continue
        code, period_str, adjust = parts
        metadata = await cache.metadata(path)
        items.append(
            DataStatusOut(
                symbol=code,
                listing_board=infer_a_share_listing_board(code).value,
                period=period_str,
                adjust=adjust,
                bar_count=metadata.bar_count,
                first_date=str(metadata.first_date) if metadata.first_date else None,
                last_date=str(metadata.last_date) if metadata.last_date else None,
                last_close=None,
                source=metadata.source,
            )
        )
    return DataStatusListOut(
        items=items,
        total=len(matching_files),
        limit=safe_limit,
        offset=safe_offset,
    )


@router.get("/status-selection", response_model=DataStatusSelectionOut)
async def select_cache_status(
    q: str | None = None,
    period: str | None = None,
    adjust: str | None = None,
    listing_board: list[str] | None = Query(default=None),
) -> DataStatusSelectionOut:
    """返回匹配筛选条件的全部缓存项,供发布页一键全选。

    该端点冻结一次筛选结果及其整体日期范围,避免前端逐页请求后漏选。
    """
    from finboard_data.cache import ParquetCache
    from finboard_data.discovery import infer_a_share_listing_board

    cache = ParquetCache(_CACHE_DIR, max_io_concurrency=8)
    cache_path = Path(_CACHE_DIR)
    parquet_files = sorted(await asyncio.to_thread(lambda: list(cache_path.glob("*.parquet"))))
    query = q.strip().upper() if q else None
    selected_paths: list[tuple[Path, str, str, str]] = []
    for path in parquet_files:
        parts = path.stem.rsplit("_", 2)
        if len(parts) != 3:
            continue
        code, period_str, adjustment = parts
        if (
            (query is not None and query not in code.upper())
            or (period is not None and period_str != period)
            or (adjust is not None and adjustment != adjust)
            or (
                listing_board
                and infer_a_share_listing_board(code).value not in listing_board
            )
        ):
            continue
        selected_paths.append((path, code, period_str, adjustment))

    items: list[DataStatusOut] = []
    first_dates: list[str] = []
    last_dates: list[str] = []
    for offset in range(0, len(selected_paths), 256):
        batch = selected_paths[offset : offset + 256]
        metadata_batch = await asyncio.gather(*(cache.metadata(path) for path, *_ in batch))
        for (_, code, period_str, adjustment), metadata in zip(
            batch,
            metadata_batch,
            strict=True,
        ):
            first_date = str(metadata.first_date) if metadata.first_date else None
            last_date = str(metadata.last_date) if metadata.last_date else None
            if first_date is not None:
                first_dates.append(first_date)
            if last_date is not None:
                last_dates.append(last_date)
            items.append(
                DataStatusOut(
                    symbol=code,
                    listing_board=infer_a_share_listing_board(code).value,
                    period=period_str,
                    adjust=adjustment,
                    bar_count=metadata.bar_count,
                    first_date=first_date,
                    last_date=last_date,
                    last_close=None,
                    source=metadata.source,
                )
            )
    return DataStatusSelectionOut(
        items=items,
        total=len(items),
        first_date=min(first_dates, default=None),
        last_date=max(last_dates, default=None),
    )


@router.get("/status/{symbol}", response_model=DataStatusOut)
async def get_cache_status(symbol: str) -> DataStatusOut:
    """查看单标的缓存详情;只读 Parquet footer。"""
    from finboard_data.cache import ParquetCache, make_symbol
    from finboard_shared.types import BarPeriod

    cache = ParquetCache(_CACHE_DIR)
    sym = make_symbol(symbol)
    metadata = await cache.metadata_for(sym, BarPeriod.D1, "qfq")
    return DataStatusOut(
        symbol=symbol,
        period="D1",
        adjust="qfq",
        bar_count=metadata.bar_count if metadata else 0,
        first_date=(str(metadata.first_date) if metadata and metadata.first_date else None),
        last_date=str(metadata.last_date) if metadata and metadata.last_date else None,
        last_close=None,
        source=metadata.source if metadata else None,
    )


@router.get("/cache/preview", response_model=DataPreviewOut)
async def preview_cache_bars(
    symbol: str = Query(..., description="含交易所后缀的标的代码,如 510300.SH"),
    limit: int = Query(default=20, ge=1, le=MAX_PREVIEW_LIMIT, description="尾部 bar 数"),
    adjust: str = Query(default="qfq", description="复权键(qfq/none)"),
) -> DataPreviewOut:
    """只读预览单标的本地缓存 parquet 的尾部 bar(数据页可观测性)。

    直接按缓存文件名定位(与 /status 系列同一 D1/qfq 口径),pyarrow 读
    尾部行,不走 Bar 对象构造;纯读,无写路径。
    """
    from finboard_data.cache import make_symbol

    try:
        normalized = validate_preview_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    sym = make_symbol(normalized)
    artifact = Path(_CACHE_DIR) / f"{sym.code}_1d_{adjust}.parquet"
    if not artifact.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"缓存文件不存在: {artifact}(请先在行情拉取页同步该标的)",
        )
    columns, rows, total = await asyncio.to_thread(read_parquet_tail, artifact, limit)
    return DataPreviewOut(
        label=f"{normalized} · 本地缓存 (1d/{adjust})",
        columns=columns,
        rows=rows,
        total_rows=total,
        truncated=total > len(rows),
        artifact=artifact.name,
    )


@router.get("/tushare-quota", response_model=TushareQuotaOut)
async def get_tushare_quota(request: Request) -> TushareQuotaOut:
    """返回本应用对 Tushare 的 RPM 与每日预占预算。"""
    from finboard_data.tushare_budget import shared_tushare_budget

    settings = _request_settings(request)
    budget = shared_tushare_budget(
        requests_per_minute=(settings.tushare_requests_per_minute if settings else 200),
        daily_request_limit=(settings.tushare_daily_request_limit if settings else 100_000),
        usage_file=(settings.tushare_usage_file if settings else "data_cache/tushare_usage.json"),
    )
    snapshot = await budget.snapshot()
    return TushareQuotaOut(
        date=snapshot.date,
        requests_per_minute=snapshot.requests_per_minute,
        daily_limit=snapshot.daily_limit,
        used=snapshot.used,
        remaining=snapshot.remaining,
    )


@router.post("/fetch", response_model=FetchResultOut)
async def fetch_data(
    req: DataFetchRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> FetchResultOut:
    """触发单个标的的数据拉取,主源失败时自动用备用源重试。"""
    from finboard_data.cache import ParquetCache, make_symbol
    from finboard_shared.types import BarPeriod

    try:
        settings = _request_settings(request)
        primary_name = _resolve_provider_name(req.source, settings=settings)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    fallback_name = _fallback_provider_name(primary_name, settings=settings)
    sym = make_symbol(req.symbol)
    start_date = parse_date.fromisoformat(req.start)
    end_date = parse_date.fromisoformat(req.end)
    cache = ParquetCache(_CACHE_DIR)
    errors: list[str] = []
    actual_source: str | None = None
    actual_provider: Any | None = None
    bars: list[Any] = []

    for source_name in (primary_name, fallback_name):
        if source_name is None:
            continue
        provider = _get_provider(source_name, use_cache=False, settings=settings)
        try:
            candidate = await provider.fetch_bars(
                sym,
                BarPeriod.D1,
                start_date,
                end_date,
                adjust=req.adjust,
            )
            if candidate:
                bars = candidate
                actual_source = source_name
                actual_provider = provider
                break
            errors.append(f"{source_name}: 返回空数据")
        except Exception as exc:
            errors.append(f"{source_name}: {exc}")
            logger.warning(
                "data.fetch_source_failed",
                symbol=req.symbol,
                source=source_name,
                error=str(exc),
            )

    if not bars or actual_source is None:
        detail = "; ".join(errors) or "所有行情源均未返回数据"
        raise HTTPException(
            status_code=502,
            detail=f"数据拉取失败(主源及备用源均不可用): {detail}",
        )

    await _store_fetched_bars(
        cache,
        sym,
        BarPeriod.D1,
        req.adjust,
        bars,
        actual_source,
    )
    lifecycle_events = 0
    lifecycle_sync_failed = False
    lifecycle_sync_error: str | None = None
    if actual_source == "tushare" and actual_provider is not None:
        try:
            events = await actual_provider.fetch_suspension_events(
                sym,
                start_date,
                end_date,
            )
            lifecycle_events = await _persist_tushare_lifecycle_events(session, events)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            lifecycle_sync_failed = True
            lifecycle_sync_error = type(exc).__name__
            logger.warning(
                "data.lifecycle_sync_failed",
                symbol=req.symbol,
                source=actual_source,
                error_type=type(exc).__name__,
            )
    used_fallback = actual_source != primary_name
    return FetchResultOut(
        symbol=req.symbol,
        bar_count=len(bars),
        first_date=str(bars[0].timestamp.date()) if bars else None,
        last_date=str(bars[-1].timestamp.date()) if bars else None,
        source=actual_source,
        fallback_used=used_fallback,
        fallback_source=actual_source if used_fallback else None,
        lifecycle_events=lifecycle_events,
        lifecycle_sync_failed=lifecycle_sync_failed,
        lifecycle_sync_error=lifecycle_sync_error,
    )


@router.get("/quality", response_model=list[QualityReportOut])
async def check_cache_quality(
    symbols: str | None = None,
    adjust: str = "qfq",
) -> list[QualityReportOut]:
    """检查已缓存行情数据的质量,无需重新拉取。

    :param symbols: 逗号分隔的标的代码;不传则检查所有缓存文件
    :param adjust:   复权方式
    """
    from finboard_data.cache import ParquetCache, make_symbol
    from finboard_data.quality import BarQualityChecker
    from finboard_shared.types import BarPeriod

    cache = ParquetCache(_CACHE_DIR)
    checker = BarQualityChecker()

    if symbols:
        codes = [c.strip() for c in symbols.split(",") if c.strip()]
    else:
        import os

        cache_dir = _CACHE_DIR
        all_files = os.listdir(cache_dir)
        codes = sorted(
            f.rsplit("_", 2)[0]
            for f in all_files
            if f.endswith(f"_{BarPeriod.D1.value}_{adjust}.parquet")
        )

    results: list[QualityReportOut] = []
    for code in codes:
        try:
            sym = make_symbol(code)
            bars = await cache.read(sym, BarPeriod.D1, adjust)
            qr = checker.check(bars, symbol=code)
            results.append(
                QualityReportOut(
                    symbol=code,
                    total_bars=qr.total_bars,
                    anomaly_count=qr.anomaly_count,
                    duplicate_count=qr.duplicate_count,
                    sources=list(qr.sources),
                    anomalies=[
                        BarAnomalyOut(
                            date=str(a.date),
                            source=a.source,
                            reasons=list(a.reasons),
                        )
                        for a in qr.anomalies[:20]
                    ],
                    passed=bool(bars) and qr.passed,
                    primary_source=qr.sources[0] if qr.sources else "",
                )
            )
        except Exception as exc:
            logger.exception("quality_check_failed", symbol=code)
            results.append(
                QualityReportOut(
                    symbol=code,
                    total_bars=0,
                    anomaly_count=0,
                    duplicate_count=0,
                    passed=False,
                    error=str(exc),
                )
            )

    return results


@router.post("/quality/repair", response_model=JobOut, status_code=202)
async def repair_cache_quality(
    req: QualityRepairRequest,
    response: Response,
    request: Request,
) -> JobOut:
    """登记批量缓存异常 bar 修复任务,立即返回 202 + job_id(issue #144)。

    实际执行由 worker 消费 ``kind=quality_repair`` 任务(保留 Semaphore(3) 并发);
    精细的逐标的修复报告在迁移后降级为 ``JobOut`` 进度,不再返回。进度 / 状态 /
    取消统一通过 ``/api/jobs/{job_id}`` 轮询。
    """
    import hashlib

    from finboard_api.job_helpers import enqueue_job

    payload: dict[str, Any] = {
        "symbols": list(dict.fromkeys(req.symbols)),
        "source": req.source,
        "adjust": req.adjust,
    }
    symbols_digest = hashlib.sha256(
        ",".join(payload["symbols"]).encode("utf-8")
    ).hexdigest()[:16]
    idempotency_key = f"quality_repair:{symbols_digest}:{req.source}:{req.adjust}"
    session_maker = getattr(request.app.state, "session_maker", None)
    if session_maker is None:
        raise HTTPException(
            status_code=503,
            detail="数据库会话未初始化,无法登记任务",
        )
    try:
        async with session_maker() as session:
            job = await enqueue_job(
                session,
                response,
                kind="quality_repair",
                queue="data",
                idempotency_key=idempotency_key,
                payload=payload,
                requested_by="api:quality_repair",
            )
            await session.commit()
    except BackgroundJobPersistenceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job


@router.get("/symbols", response_model=SymbolPoolOut)
async def get_symbol_pool() -> SymbolPoolOut:
    """获取标的池配置。"""
    from finboard_data import load_symbol_pool

    config = load_symbol_pool(_SYMBOLS_FILE)
    return SymbolPoolOut(
        symbols=[SymbolEntrySchema(code=s.code, name=s.name) for s in config.symbols],
        fetch_period=config.fetch_period,
        fetch_lookback_days=config.fetch_lookback_days,
        fetch_adjust=config.fetch_adjust,
    )


@router.put("/symbols", response_model=SymbolPoolOut)
async def update_symbol_pool(req: SymbolPoolUpdate) -> SymbolPoolOut:
    """更新标的池配置。"""
    from finboard_data import SymbolEntry, SymbolPoolConfig, save_symbol_pool

    config = SymbolPoolConfig(
        symbols=[SymbolEntry(code=s.code, name=s.name) for s in req.symbols],
        fetch_period=req.fetch_period,
        fetch_lookback_days=req.fetch_lookback_days,
        fetch_adjust=req.fetch_adjust,
    )
    save_symbol_pool(config, _SYMBOLS_FILE)
    return SymbolPoolOut(
        symbols=[SymbolEntrySchema(code=s.code, name=s.name) for s in config.symbols],
        fetch_period=config.fetch_period,
        fetch_lookback_days=config.fetch_lookback_days,
        fetch_adjust=config.fetch_adjust,
    )


# ------------------------------------------------------------------ Instruments (DB)
@router.get("/instruments/summary", response_model=InstrumentSummaryOut)
async def summarize_instruments(
    session: AsyncSession = Depends(get_db_session),
) -> InstrumentSummaryOut:
    """返回标的字典的状态、市场和类型分布。"""
    from finboard_persistence import InstrumentModel

    async def grouped_counts(column: Any) -> dict[str, int]:
        result = await session.execute(
            select(column, func.count(InstrumentModel.id)).group_by(column).order_by(column)
        )
        return {str(key or "unknown"): int(count) for key, count in result.all()}

    by_status = await grouped_counts(InstrumentModel.status)
    by_market = await grouped_counts(InstrumentModel.market)
    by_instrument_type = await grouped_counts(InstrumentModel.instrument_type)
    by_listing_board = await grouped_counts(InstrumentModel.listing_board)
    active_etf_result = await session.execute(
        select(func.count(InstrumentModel.id)).where(
            InstrumentModel.status == "active",
            InstrumentModel.instrument_type == "etf",
        )
    )
    return InstrumentSummaryOut(
        total=sum(by_status.values()),
        active_total=by_status.get("active", 0),
        active_etf_total=int(active_etf_result.scalar_one() or 0),
        by_status=by_status,
        by_market=by_market,
        by_instrument_type=by_instrument_type,
        by_listing_board=by_listing_board,
    )


@router.get("/instruments", response_model=InstrumentListOut)
async def list_instruments(
    market: str | None = None,
    instrument_type: str | None = None,
    exchange: str | None = None,
    listing_board: list[str] | None = Query(default=None),
    status: str | None = "active",
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
    session: AsyncSession = Depends(get_db_session),
) -> InstrumentListOut:
    """列出数据库中的标的(分页,可选模糊搜索)。"""
    from finboard_persistence import InstrumentRepository

    repo = InstrumentRepository(session)
    status_filter = None if status in (None, "", "all") else status
    rows, total = await repo.list_page(
        market=market,
        instrument_type=instrument_type,
        exchange=exchange,
        listing_boards=listing_board,
        status=status_filter,
        q=q,
        limit=limit,
        offset=offset,
    )
    await session.commit()
    return InstrumentListOut(
        items=[
            InstrumentOut(
                code=r.code,
                name=r.name,
                market=r.market,
                instrument_type=r.instrument_type,
                exchange=r.exchange,
                listing_board=r.listing_board,
                list_date=r.list_date,
                delist_date=r.delist_date,
                status=r.status,
                sector=r.sector,
                industry=r.industry,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/instruments/codes", response_model=list[str])
async def list_instrument_codes(
    market: str | None = None,
    instrument_type: str | None = None,
    exchange: str | None = None,
    listing_board: list[str] | None = Query(default=None),
    q: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> list[str]:
    """返回匹配条件的全部标的代码(不分页,供前端"全选"使用)。"""
    from finboard_persistence import InstrumentRepository

    repo = InstrumentRepository(session)
    codes = await repo.list_codes(
        market=market,
        instrument_type=instrument_type,
        exchange=exchange,
        listing_boards=listing_board,
        q=q,
    )
    await session.commit()
    return codes


@router.get("/instruments/search", response_model=list[InstrumentOut])
async def search_instruments(
    q: str,
    limit: int = 50,
    session: AsyncSession = Depends(get_db_session),
) -> list[InstrumentOut]:
    """按代码或名称模糊搜索标的。"""
    from finboard_persistence import InstrumentRepository

    repo = InstrumentRepository(session)
    rows = await repo.search(q, limit=limit)
    await session.commit()
    return [
        InstrumentOut(
            code=r.code,
            name=r.name,
            market=r.market,
            instrument_type=r.instrument_type,
            exchange=r.exchange,
            listing_board=r.listing_board,
            status=r.status,
        )
        for r in rows
    ]


# ------------------------------------------------------------------ Sync (DB)

@router.post("/sync", response_model=JobOut, status_code=202)
async def sync_universe(
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    """登记全市场标的同步任务,立即返回 202 + job_id(issue #144)。

    实际执行由 worker 消费 ``kind=data_sync`` 任务;上游 akshare 不可用会在 worker
    端映射为 ``failed(data_source_unavailable)``。进度 / 状态 / 取消统一通过
    ``/api/jobs/{job_id}`` 轮询。同步范围含基准指数登记(issue #256):
    ``discover_indices`` 受控登记表自动写入 ``instrument_type=index`` 行。
    """
    from datetime import date

    from finboard_api.job_helpers import enqueue_job

    payload: dict[str, Any] = {"as_of": date.today().isoformat()}
    idempotency_key = f"data_sync:{date.today().isoformat()}"
    try:
        job = await enqueue_job(
            session,
            response,
            kind="data_sync",
            queue="data",
            idempotency_key=idempotency_key,
            payload=payload,
            requested_by="api:sync",
        )
        await session.commit()
    except BackgroundJobPersistenceConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job


@router.post("/bulk-download", response_model=JobOut, status_code=202)
async def start_bulk_download(
    request: Request,
    response: Response,
    req: BulkDownloadRequest,
    session: AsyncSession = Depends(get_db_session),
) -> JobOut:
    """登记批量历史数据拉取任务,立即返回 202 + job_id(issue #144)。

    实际执行由独立 worker 进程(``finboard worker run``)消费 ``kind=bulk_download``
    任务。进度 / 状态 / 取消统一通过 ``/api/jobs/{job_id}`` 轮询。
    ``symbols``(#347)可选 —— 失败标的子集重跑,与 market / instrument_type /
    exchange / listing_boards 过滤叠加,交集为空执行器按 no_instruments 拒。
    入队期 payload 契约校验(#347,#260 风格):非法 source / 坏日期 /
    tushare x etf|futures 秒级 422,不再等 worker 执行期才失败。
    """
    import hashlib

    from finboard_api.job_helpers import enqueue_job
    from finboard_backtest.background_jobs.payload_contracts import (
        PayloadContractError,
        validate_job_payload,
    )

    payload: dict[str, Any] = {
        "market": req.market,
        "source": req.source or "",
        "start": req.start,
        "instrument_type": req.instrument_type,
        "exchange": req.exchange,
        "listing_boards": list(req.listing_boards),
    }
    # symbols 只在显式提供时进入 payload:缺省 payload 与 #347 之前逐字节
    # 一致(payload_checksum 稳定,同 idempotency_key 重提交不因新增键冲突)。
    symbols_digest = ""
    if req.symbols:
        deduped = list(dict.fromkeys(req.symbols))
        payload["symbols"] = deduped
        symbols_digest = hashlib.sha256(
            ",".join(deduped).encode("utf-8")
        ).hexdigest()[:16]
    # 入队期契约(#347):REST 与 MCP 语义化端点共用同一校验函数。
    try:
        validate_job_payload("bulk_download", payload)
    except PayloadContractError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"payload 契约校验失败[{exc.code}]: {exc.summary}",
        ) from exc
    idempotency_key = (
        f"bulk_download:{req.market}:{req.source or 'auto'}:{req.start}:"
        f"{req.instrument_type or 'all'}"
    )
    if symbols_digest:
        # 子集重跑的幂等键带 symbols 摘要:不同子集不互相命中旧任务。
        idempotency_key += f":sub:{symbols_digest}"
    try:
        job = await enqueue_job(
            session,
            response,
            kind="bulk_download",
            queue="data",
            idempotency_key=idempotency_key,
            payload=payload,
            requested_by="api:bulk_download",
        )
        await session.commit()
    except BackgroundJobPersistenceConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job


# ------------------------------------------------------------------ Scheduler Config
def _load_config() -> dict[str, Any]:
    import json
    from pathlib import Path

    p = Path(_CONFIG_FILE)
    if p.exists():
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data
    return {}


def _save_config(cfg: dict[str, Any]) -> None:
    import json
    from pathlib import Path

    Path(_CONFIG_FILE).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("/config", response_model=SchedulerConfigOut)
async def get_scheduler_config(request: Request) -> SchedulerConfigOut:
    """获取定时任务配置。"""
    cfg = _load_config()
    settings = _request_settings(request)
    return SchedulerConfigOut(
        sync_enabled=cfg.get("sync_enabled", True),
        sync_time=cfg.get("sync_time", "15:35"),
        download_enabled=cfg.get("download_enabled", True),
        download_time=cfg.get("download_time", "15:45"),
        download_lookback_days=cfg.get("download_lookback_days", 5),
        download_markets=cfg.get("download_markets", ["a_share"]),
        download_types=cfg.get("download_types", ["stock", "etf"]),
        data_provider=settings.data_provider if settings is not None else "tushare",
    )


@router.put("/config", response_model=SchedulerConfigOut)
async def update_scheduler_config(
    req: SchedulerConfigUpdate,
    request: Request,
) -> SchedulerConfigOut:
    """更新定时任务配置。"""
    cfg = _load_config()
    updates = req.model_dump(exclude_none=True)
    cfg.update(updates)
    _save_config(cfg)
    settings = _request_settings(request)

    return SchedulerConfigOut(
        sync_enabled=cfg.get("sync_enabled", True),
        sync_time=cfg.get("sync_time", "15:35"),
        download_enabled=cfg.get("download_enabled", True),
        download_time=cfg.get("download_time", "15:45"),
        download_lookback_days=cfg.get("download_lookback_days", 5),
        download_markets=cfg.get("download_markets", ["a_share"]),
        download_types=cfg.get("download_types", ["stock", "etf"]),
        data_provider=settings.data_provider if settings is not None else "tushare",
    )
