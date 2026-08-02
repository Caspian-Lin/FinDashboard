"""历史行情数据管理端点。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from datetime import date as parse_date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.schemas import (
    BarAnomalyOut,
    BatchFetchResultOut,
    BulkDownloadRequest,
    BulkDownloadStatusOut,
    DataFetchRequest,
    DataStatusListOut,
    DataStatusOut,
    DataStatusSelectionOut,
    FetchResultOut,
    InstrumentListOut,
    InstrumentOut,
    InstrumentSummaryOut,
    LLMConfigOut,
    LLMConfigUpdate,
    QualityRepairRequest,
    QualityRepairResultOut,
    QualityReportOut,
    SchedulerConfigOut,
    SchedulerConfigUpdate,
    SymbolEntrySchema,
    SymbolPoolOut,
    SymbolPoolUpdate,
    SyncResultOut,
    TushareQuotaOut,
)

logger = structlog.get_logger(__name__)

_BULK_LOG_LIMIT = 1_000


def _append_bulk_download_log(
    state: dict[str, Any],
    *,
    event: str,
    code: str,
    reason: str | None = None,
) -> int:
    """追加有限长度的批量拉取事件,返回可用于补充原因的序号。"""
    logs = state.setdefault("logs", [])
    seq = logs[-1]["seq"] + 1 if logs else 1
    logs.append(
        {
            "seq": seq,
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "code": code,
            "reason": reason,
        }
    )
    if len(logs) > _BULK_LOG_LIMIT:
        del logs[: len(logs) - _BULK_LOG_LIMIT]
    return seq


def _set_bulk_download_log_reason(
    state: dict[str, Any],
    *,
    seq: int,
    reason: str,
) -> None:
    """异步缓存检查完成后,补充对应“正在拉取”事件的未命中原因。"""
    for entry in reversed(state.get("logs", [])):
        if entry["seq"] == seq:
            entry["reason"] = reason
            return

if TYPE_CHECKING:
    from finboard_app.config import Settings
    from finboard_data import AkShareProvider, TushareBarProvider, YFinanceProvider

router = APIRouter(prefix="/api/data", tags=["data"])

_CACHE_DIR = "data_cache"
_SYMBOLS_FILE = "symbols.yaml"
_CONFIG_FILE = "data_config.json"
_SUPPORTED_BAR_PROVIDERS = {"akshare", "tushare", "yfinance"}

# .env 作为 LLM provider 配置的真相源(由设置页 GET/PUT 维护)。
_ENV_FILE = ".env"
_API_KEY_MASK = "********"
# .env 变量名 ↔ schema 字段名。
_LLM_KEYS: dict[str, str] = {
    "FINBOARD_LLM_PROVIDER": "provider",
    "FINBOARD_LLM_BASE_URL": "base_url",
    "FINBOARD_LLM_API_KEY": "api_key",
    "FINBOARD_LLM_MODEL": "model",
    "FINBOARD_LLM_TIMEOUT_SECONDS": "timeout_seconds",
    "FINBOARD_LLM_MAX_RETRIES": "max_retries",
}


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
    provider_name = (
        (source or configured or os.getenv("FINBOARD_DATA_PROVIDER") or "akshare").strip().lower()
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


def _validate_bulk_provider_scope(provider_name: str, instruments: list[Any]) -> None:
    """批量任务按资产类型隔离, 避免 Tushare 股票接口误吞 ETF。"""
    if provider_name != "tushare":
        return

    incompatible = [
        instrument.code
        for instrument in instruments
        if getattr(instrument.market, "value", instrument.market) != "a_share"
        or getattr(instrument.instrument_type, "value", instrument.instrument_type) != "stock"
    ]
    if incompatible:
        raise ValueError(
            "Tushare 批量任务仅支持 A 股股票; 请将类型设为“股票”。"
            "ETF 请另建任务并选择 akshare 或 yfinance, 之后可发布多资产混合来源数据集。"
        )


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
    """幂等写入 Tushare 停复牌事件,返回本次新增数量。"""
    if not events:
        return 0

    from sqlalchemy.dialects.postgresql import insert

    from finboard_persistence import InstrumentLifecycleEventModel

    observed_at = datetime.now(UTC)
    values = [
        {
            "symbol": event.symbol,
            "event_type": event.event_type,
            "effective_date": event.effective_date,
            # 历史事件是现在从 API 观测到的,不能倒填成当时已知。
            "available_at": observed_at,
            "source": "tushare",
            "dataset_version": "suspend_d-v1",
            "details": {
                "suspend_type": "R" if event.event_type == "resumption" else "S",
                "suspend_timing": event.suspend_timing,
            },
            "observed_at": observed_at,
        }
        for event in events
    ]
    statement = (
        insert(InstrumentLifecycleEventModel)
        .values(values)
        .on_conflict_do_nothing(constraint="uq_instrument_lifecycle_event")
        .returning(InstrumentLifecycleEventModel.id)
    )
    result = await session.execute(statement)
    return len(result.scalars().all())


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


@router.post("/quality/repair", response_model=QualityRepairResultOut)
async def repair_cache_quality(
    req: QualityRepairRequest,
    request: Request,
) -> QualityRepairResultOut:
    """批量使用指定备用源修复存量缓存中的异常 bar。"""
    from dataclasses import replace

    from finboard_data.cache import ParquetCache, make_symbol
    from finboard_data.quality import BarQualityChecker
    from finboard_shared.types import BarPeriod

    cache = ParquetCache(_CACHE_DIR)
    checker = BarQualityChecker()
    # 强制绕过共享缓存,确保备用源真正重新请求远端数据。
    provider = _get_provider(
        req.source,
        use_cache=False,
        settings=_request_settings(request),
    )
    semaphore = asyncio.Semaphore(3)

    async def _repair(code: str) -> tuple[QualityReportOut, int, bool]:
        sym = make_symbol(code)
        try:
            bars = await cache.read(sym, BarPeriod.D1, req.adjust)
            before = checker.check(bars, symbol=code)
            if not bars:
                return (
                    QualityReportOut(
                        symbol=code,
                        total_bars=0,
                        anomaly_count=0,
                        passed=False,
                        fallback_used=True,
                        fallback_source=req.source,
                        error="缓存为空",
                    ),
                    0,
                    False,
                )

            corrected_dates: list[str] = []
            by_date = {bar.timestamp.date(): bar for bar in bars}
            anomaly_dates = set(before.anomaly_dates)
            # 重新拉取整个已有日期窗口,这样不仅能修 OHLC 异常,也能补上主源漏掉的日期。
            async with semaphore:
                alternatives = await provider.fetch_bars(
                    sym,
                    BarPeriod.D1,
                    min(bar.timestamp.date() for bar in bars),
                    max(bar.timestamp.date() for bar in bars),
                    adjust=req.adjust,
                )
            for alternative in alternatives:
                bar_date = alternative.timestamp.date()
                current = by_date.get(bar_date)
                if current is not None and bar_date not in anomaly_dates:
                    continue
                if checker.check([alternative], symbol=code).passed:
                    by_date[bar_date] = replace(alternative, source=req.source)
                    if current is None or bar_date in anomaly_dates:
                        corrected_dates.append(str(bar_date))

            repaired_bars = sorted(by_date.values(), key=lambda bar: bar.timestamp)
            if corrected_dates or before.duplicate_count:
                await cache.write(sym, BarPeriod.D1, req.adjust, repaired_bars)
            after = checker.check(repaired_bars, symbol=code)
            passed = bool(repaired_bars) and after.passed
            return (
                QualityReportOut(
                    symbol=code,
                    total_bars=after.total_bars,
                    anomaly_count=after.anomaly_count,
                    duplicate_count=after.duplicate_count,
                    sources=list(after.sources),
                    anomalies=[
                        BarAnomalyOut(
                            date=str(anomaly.date),
                            source=anomaly.source,
                            reasons=list(anomaly.reasons),
                        )
                        for anomaly in after.anomalies[:20]
                    ],
                    passed=passed,
                    primary_source=before.sources[0] if before.sources else "",
                    fallback_used=True,
                    fallback_source=req.source,
                    corrected_dates=corrected_dates,
                    error=None if passed else "备用源未覆盖全部异常或缺失日期",
                ),
                len(corrected_dates),
                passed,
            )
        except Exception as exc:
            logger.exception("quality_repair_failed", symbol=code, source=req.source)
            return (
                QualityReportOut(
                    symbol=code,
                    total_bars=0,
                    anomaly_count=0,
                    passed=False,
                    fallback_used=True,
                    fallback_source=req.source,
                    error=str(exc),
                ),
                0,
                False,
            )

    unique_codes = list(dict.fromkeys(req.symbols))
    repaired_results = await asyncio.gather(*[_repair(code) for code in unique_codes])
    reports = [item[0] for item in repaired_results]
    repaired = sum(1 for _, _, passed in repaired_results if passed)
    return QualityRepairResultOut(
        total=len(unique_codes),
        repaired=repaired,
        failed=len(unique_codes) - repaired,
        corrected_bars=sum(item[1] for item in repaired_results),
        reports=reports,
    )


@router.post("/fetch-all", response_model=BatchFetchResultOut)
async def fetch_all_data(request: Request) -> BatchFetchResultOut:
    """批量更新标的池缓存,不在内存中保留所有历史 Bars。"""
    from datetime import timedelta

    from finboard_data import load_symbol_pool
    from finboard_data.cache import ParquetCache, make_symbol
    from finboard_shared.types import BarPeriod

    config = load_symbol_pool(_SYMBOLS_FILE)
    if not config.symbols:
        return BatchFetchResultOut(total=0, success=0, failed=0, details=[])

    end = parse_date.today()
    start = end - timedelta(days=config.fetch_lookback_days)
    period = (
        BarPeriod[config.fetch_period]
        if config.fetch_period in BarPeriod.__members__
        else BarPeriod(config.fetch_period)
    )
    provider = _get_provider(settings=_request_settings(request))
    sym_objs = [make_symbol(s.code) for s in config.symbols]
    results = await provider.update_cache_batch(
        sym_objs,
        period,
        start,
        end,
        adjust=config.fetch_adjust,
    )

    details: list[FetchResultOut] = []
    cache = ParquetCache(_CACHE_DIR)
    for entry in config.symbols:
        metadata = await cache.metadata_for(
            make_symbol(entry.code),
            period,
            config.fetch_adjust,
        )
        details.append(
            FetchResultOut(
                symbol=entry.code,
                bar_count=metadata.bar_count if metadata else 0,
                first_date=(str(metadata.first_date) if metadata and metadata.first_date else None),
                last_date=str(metadata.last_date) if metadata and metadata.last_date else None,
                source=metadata.source if metadata else None,
            )
        )

    success = sum(results.values())
    return BatchFetchResultOut(
        total=len(config.symbols),
        success=success,
        failed=len(config.symbols) - success,
        details=details,
    )


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
def _get_bulk_state(request: Request) -> dict[str, Any]:
    if not hasattr(request.app.state, "_bulk_download"):
        request.app.state._bulk_download = {
            "status": "idle",
            "done": 0,
            "total": 0,
            "success": 0,
            "failed": 0,
            "current_symbol": None,
            "phase": None,
            "error": None,
            "cache_hits": 0,
            "cache_misses": 0,
            "started_at": None,
            "active_symbols": [],
            "logs": [],
        }
    return request.app.state._bulk_download  # type: ignore[no-any-return]


@router.post("/sync", response_model=SyncResultOut)
async def sync_universe(
    session: AsyncSession = Depends(get_db_session),
) -> SyncResultOut:
    """从 akshare 发现全市场标的,写入 instruments 表(带生命周期 diff)。"""
    from datetime import date

    from finboard_data.discovery import UniverseDiscovery
    from finboard_persistence import InstrumentRepository

    discovery = UniverseDiscovery()
    try:
        instruments = await discovery.discover_all()
    except ModuleNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail="标的池同步依赖 akshare,请执行 uv sync --all-packages 后重启 API",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"标的池同步失败(上游数据源暂不可用): {exc}",
        ) from exc

    dicts: list[dict[str, object]] = [
        {
            "code": ins.code,
            "name": ins.name,
            "market": ins.market.value,
            "instrument_type": ins.instrument_type.value,
            "exchange": ins.exchange,
            "listing_board": ins.listing_board.value,
        }
        for ins in instruments
    ]

    repo = InstrumentRepository(session)
    result = await repo.sync_with_diff(dicts, as_of=date.today())
    await session.commit()

    return SyncResultOut(
        total=result.total,
        new=result.new,
        updated=result.updated,
        renamed=len(result.renamed),
        pending_delist=len(result.pending_delist),
        delisted=len(result.delisted),
        reactivated=len(result.reactivated),
    )


@router.post("/bulk-download", response_model=BulkDownloadStatusOut)
async def start_bulk_download(
    request: Request,
    req: BulkDownloadRequest,
    session: AsyncSession = Depends(get_db_session),
) -> BulkDownloadStatusOut:
    """启动批量历史数据拉取(后台异步任务)。"""
    import asyncio
    from datetime import date as parse_d
    from datetime import datetime as parse_dt

    state = _get_bulk_state(request)
    if state["status"] == "running":
        raise HTTPException(status_code=409, detail="批量拉取正在运行中")
    from finboard_data import AkShareProvider, TushareBarProvider, YFinanceProvider
    from finboard_data.cache import make_symbol
    from finboard_persistence import InstrumentRepository
    from finboard_shared.types import BarPeriod

    repo = InstrumentRepository(session)
    instruments, _total = await repo.list_active(
        market=req.market,
        instrument_type=req.instrument_type,
        exchange=req.exchange,
        listing_boards=req.listing_boards or None,
        limit=999999,
    )
    await session.commit()

    if not instruments:
        raise HTTPException(status_code=400, detail="未找到匹配的标的(请先同步)")

    try:
        settings = _request_settings(request)
        provider_name = _resolve_provider_name(req.source, settings=settings)
        _validate_bulk_provider_scope(provider_name, instruments)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if provider_name == "akshare":
        primary: AkShareProvider | TushareBarProvider | YFinanceProvider = AkShareProvider(
            max_concurrency=2, request_interval=0.5
        )
    elif provider_name == "tushare":
        primary = TushareBarProvider(
            token=settings.tushare_token if settings is not None else None,
            max_concurrency=16,
            requests_per_minute=(settings.tushare_requests_per_minute if settings else 200),
            daily_request_limit=(settings.tushare_daily_request_limit if settings else 100_000),
            usage_file=(
                settings.tushare_usage_file if settings else "data_cache/tushare_usage.json"
            ),
        )
    else:
        primary = YFinanceProvider(max_concurrency=3, request_interval=0.3)

    sym_objs = [make_symbol(ins.code) for ins in instruments]
    start_date = parse_d.fromisoformat(req.start)
    end_date = parse_d.today()

    state.update(
        status="running",
        done=0,
        total=len(sym_objs),
        success=0,
        failed=0,
        current_symbol=None,
        phase="starting",
        error=None,
        quality_passed=0,
        quality_failed=0,
        fallback_used=0,
        lifecycle_events=0,
        lifecycle_sync_failed=0,
        cache_hits=0,
        cache_misses=0,
        started_at=parse_dt.now().isoformat(),
        active_symbols=[],
        logs=[],
        quality_reports=[],
    )

    async def _run_download() -> None:
        _bg_tasks: set[asyncio.Task[None]] = set()
        try:
            from finboard_data.cache import ParquetCache, expected_last_bar_date

            _reason_cache = ParquetCache(_CACHE_DIR)
            _reason_effective_end = expected_last_bar_date(end_date)
            active_reasons: dict[str, str] = {}

            def _sync_active() -> None:
                state["active_symbols"] = [
                    {"code": c, "reason": r} for c, r in active_reasons.items()
                ]

            async def _cache_miss_reason(code: str) -> str:
                sym = make_symbol(code)
                metadata = await _reason_cache.metadata_for(sym, BarPeriod.D1, "qfq")
                if metadata is None:
                    return "无缓存"
                if metadata.source != provider_name:
                    return (
                        f"缓存来源 {metadata.source or '未知'}, 请求源为 {provider_name}"
                    )
                if provider_name == "tushare":
                    ranges = TushareBarProvider._cache_fetch_ranges(
                        metadata,
                        start_date,
                        _reason_effective_end,
                    )
                    if ranges:
                        missing = "、".join(f"{left}~{right}" for left, right in ranges)
                        return f"缺少日期段 {missing}"
                    return "缓存覆盖信息需要刷新"
                if metadata.last_date is None:
                    return "缓存没有有效日线"
                return f"缓存仅到 {metadata.last_date}, 需更新至 {_reason_effective_end}"

            async def _fill_cache_reason(code: str, log_seq: int) -> None:
                try:
                    reason = await _cache_miss_reason(code)
                except Exception:
                    reason = "缓存检查失败, 按未命中处理"
                _set_bulk_download_log_reason(state, seq=log_seq, reason=reason)
                if code in active_reasons:
                    active_reasons[code] = reason
                    _sync_active()

            def on_progress(code: str, done: int, total: int) -> None:
                active_reasons.pop(code, None)
                state["done"] = done
                state["total"] = total
                _sync_active()

            cache_hit_symbols: set[str] = set()
            cache_miss_symbols: set[str] = set()

            def on_status(code: str, phase: str) -> None:
                state["current_symbol"] = code
                state["phase"] = phase
                if phase == "cache_hit":
                    cache_hit_symbols.add(code)
                    active_reasons.pop(code, None)
                    _append_bulk_download_log(
                        state,
                        event="cache_hit",
                        code=code,
                        reason="请求日期范围已覆盖",
                    )
                elif phase == "fetching":
                    cache_miss_symbols.add(code)
                    active_reasons[code] = ""
                    log_seq = _append_bulk_download_log(
                        state,
                        event="fetching",
                        code=code,
                    )
                    _t = asyncio.create_task(_fill_cache_reason(code, log_seq))
                    _bg_tasks.add(_t)
                    _t.add_done_callback(_bg_tasks.discard)
                elif phase == "completed":
                    active_reasons.pop(code, None)
                    if code not in cache_hit_symbols:
                        _append_bulk_download_log(
                            state,
                            event="completed",
                            code=code,
                        )
                elif phase == "failed":
                    active_reasons.pop(code, None)
                    _append_bulk_download_log(
                        state,
                        event="failed",
                        code=code,
                        reason="行情更新失败, 可重新运行以重试",
                    )
                state["cache_hits"] = len(cache_hit_symbols)
                state["cache_misses"] = len(cache_miss_symbols)
                _sync_active()

            results = await primary.update_cache_batch(
                sym_objs,
                BarPeriod.D1,
                start_date,
                end_date,
                on_progress=on_progress,
                on_status=on_status,
            )
            state["success"] = sum(results.values())
            state["failed"] = len(sym_objs) - state["success"]

            # 批量日线任务只负责缓存更新。停复牌事件和质量检查是独立数据产品,
            # 不应在日线进度达到 100% 后继续串行占用数千次请求并阻塞终态。
            # 质量检查仍可通过 /api/data/quality 显式触发;生命周期同步将由
            # 独立任务按日期增量维护。
            state["status"] = "done"
            state["current_symbol"] = None
            state["phase"] = None
            state["active_symbols"] = []
            return
        except asyncio.CancelledError:
            state.update(
                status="cancelled",
                current_symbol=None,
                phase=None,
                active_symbols=[],
            )
            raise
        except Exception as exc:
            state["status"] = "error"
            state["phase"] = None
            state["error"] = str(exc)
        finally:
            for background_task in _bg_tasks:
                background_task.cancel()
            if _bg_tasks:
                await asyncio.gather(*_bg_tasks, return_exceptions=True)

    request.app.state._bulk_task = asyncio.create_task(_run_download())
    return BulkDownloadStatusOut(**state)


@router.get("/bulk-download/status", response_model=BulkDownloadStatusOut)
async def get_bulk_download_status(request: Request) -> BulkDownloadStatusOut:
    """查询批量拉取进度。"""
    state = _get_bulk_state(request)
    return BulkDownloadStatusOut(**state)


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
        data_provider=settings.data_provider if settings is not None else "akshare",
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
        data_provider=settings.data_provider if settings is not None else "akshare",
    )


# ------------------------------------------------------------------ LLM Provider Config
def _read_env_llm() -> dict[str, str]:
    """从 .env 文件读取 LLM 字段(裸值);文件不存在则返回空值集合。"""
    result: dict[str, str] = dict.fromkeys(_LLM_KEYS.values(), "")
    path = Path(_ENV_FILE)
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key in _LLM_KEYS:
            result[_LLM_KEYS[key]] = val
    return result


def _quote_env_value(val: str) -> str:
    """值含空格 / # / 引号 / 反斜杠时用双引号包裹并转义。"""
    if val == "":
        return ""
    if any(c in val for c in (" ", "#", '"', "'", "\\")):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return val


def _write_env_llm(merged: dict[str, str]) -> None:
    """把 LLM 字段写回 .env(临时文件 + 原子替换,保留其余行与注释)。

    已存在的 FINBOARD_LLM_* 行就地替换;不存在的在文件末尾追加一段。
    """
    path = Path(_ENV_FILE)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(merged)
    new_lines: list[str] = []
    for raw in lines:
        line = raw.strip()
        replaced = False
        if line and not line.startswith("#") and "=" in line:
            key = line.partition("=")[0].strip()
            py_key = _LLM_KEYS.get(key)
            if py_key is not None and py_key in remaining:
                new_lines.append(f"{key}={_quote_env_value(remaining[py_key])}")
                remaining.pop(py_key)
                replaced = True
        if not replaced:
            new_lines.append(raw)
    if remaining:
        new_lines.append("")
        new_lines.append("# ====== AI 研究助手 LLM Provider(由设置页维护)======")
        for env_key, py_key in _LLM_KEYS.items():
            if py_key in remaining:
                new_lines.append(f"{env_key}={_quote_env_value(remaining[py_key])}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def _llm_out_from_env(values: dict[str, str]) -> LLMConfigOut:
    """把 .env 字符串值转换为对外 schema(api_key 掩码)。"""
    provider = values["provider"] or "fake"
    if provider not in ("fake", "openai_compatible"):
        provider = "fake"
    try:
        timeout_f = float(values["timeout_seconds"]) if values["timeout_seconds"] else 30.0
    except ValueError:
        timeout_f = 30.0
    try:
        retries_i = int(values["max_retries"]) if values["max_retries"] else 3
    except ValueError:
        retries_i = 3
    api_key_raw = values["api_key"]
    return LLMConfigOut(
        provider=provider,  # type: ignore[arg-type]
        base_url=values["base_url"],
        api_key=_API_KEY_MASK if api_key_raw else "",
        api_key_set=bool(api_key_raw),
        model=values["model"] or "gpt-4o-mini",
        timeout_seconds=timeout_f,
        max_retries=retries_i,
    )


def _rebuild_llm_provider(request: Request, merged: dict[str, str]) -> None:
    """写完 .env 后热重建 app.state 的 provider / research_assistant。

    不抛错:配置不全时降级 fake(与 lifespan 行为一致)。
    """
    settings = _request_settings(request)
    if settings is None:
        return
    out = _llm_out_from_env(merged)
    new_settings = settings.model_copy(
        update={
            "llm_provider": out.provider,
            "llm_base_url": out.base_url,
            "llm_api_key": merged["api_key"],
            "llm_model": out.model,
            "llm_timeout_seconds": out.timeout_seconds,
            "llm_max_retries": out.max_retries,
        }
    )
    from finboard_app.llm_factory import build_llm_provider
    from finboard_backtest.factor_research import FakeLLMProvider, ResearchAssistant

    try:
        new_provider = build_llm_provider(new_settings)
    except ValueError as exc:
        logger.warning("api.llm_provider_fallback", error=str(exc))
        new_provider = FakeLLMProvider()
    old = getattr(request.app.state, "llm_provider", None)
    if old is not None and hasattr(old, "close"):
        try:
            old.close()
        except Exception as exc:
            logger.warning("api.llm_provider_close_failed", error=str(exc))
    request.app.state.settings = new_settings
    request.app.state.llm_provider = new_provider
    request.app.state.research_assistant = ResearchAssistant(new_provider)


@router.get("/llm-config", response_model=LLMConfigOut)
async def get_llm_config() -> LLMConfigOut:
    """获取 LLM provider 配置(读 .env,api_key 掩码)。"""
    return _llm_out_from_env(_read_env_llm())


@router.put("/llm-config", response_model=LLMConfigOut)
async def update_llm_config(
    req: LLMConfigUpdate,
    request: Request,
) -> LLMConfigOut:
    """更新 LLM provider 配置并持久化到 .env,热重建 provider。

    api_key 哨兵:传入 "********" 或 None 表示保留原值;传其它值(含空串)则覆盖。
    """
    cur = _read_env_llm()
    api_key = cur["api_key"]
    if req.api_key is not None and req.api_key != _API_KEY_MASK:
        api_key = req.api_key
    merged: dict[str, str] = {
        "provider": req.provider if req.provider is not None else cur["provider"],
        "base_url": req.base_url if req.base_url is not None else cur["base_url"],
        "api_key": api_key,
        "model": req.model if req.model is not None else cur["model"],
        "timeout_seconds": (
            str(req.timeout_seconds) if req.timeout_seconds is not None else cur["timeout_seconds"]
        ),
        "max_retries": (
            str(req.max_retries) if req.max_retries is not None else cur["max_retries"]
        ),
    }
    _write_env_llm(merged)
    _rebuild_llm_provider(request, merged)
    return _llm_out_from_env(merged)
