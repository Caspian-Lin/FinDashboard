"""历史行情数据管理端点。"""

from __future__ import annotations

import asyncio
from datetime import date as parse_date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
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
    QualityReportOut,
    SchedulerConfigOut,
    SchedulerConfigUpdate,
    SymbolEntrySchema,
    SymbolPoolOut,
    SymbolPoolUpdate,
    SyncResultOut,
)

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from finboard_data import AkShareProvider, YFinanceProvider

router = APIRouter(prefix="/api/data", tags=["data"])

_CACHE_DIR = "data_cache"
_SYMBOLS_FILE = "symbols.yaml"
_CONFIG_FILE = "data_config.json"

_PROVIDER: str | None = None


def _get_provider() -> AkShareProvider | YFinanceProvider:
    import os

    from finboard_data import AkShareProvider, YFinanceProvider

    global _PROVIDER
    provider_name = _PROVIDER or os.getenv("FINBOARD_DATA_PROVIDER", "akshare")
    _PROVIDER = provider_name
    if provider_name == "akshare":
        return AkShareProvider()
    return YFinanceProvider()


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
) -> DataStatusListOut:
    """分页列出缓存状态,避免一次扫描并返回全市场缓存。"""
    from finboard_data.cache import ParquetCache

    safe_limit = min(max(limit, 1), 500)
    safe_offset = max(offset, 0)
    cache = ParquetCache(_CACHE_DIR, max_io_concurrency=8)
    cache_path = Path(_CACHE_DIR)
    parquet_files = sorted(
        await asyncio.to_thread(lambda: list(cache_path.glob("*.parquet")))
    )
    query = q.strip().upper() if q else None

    def matches(path: Path) -> bool:
        parts = path.stem.rsplit("_", 2)
        if len(parts) != 3:
            return False
        code, period_str, adjustment = parts
        return (
            (query is None or query in code.upper())
            and (period is None or period_str == period)
            and (adjust is None or adjustment == adjust)
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
                period=period_str,
                adjust=adjust,
                bar_count=metadata.bar_count,
                first_date=str(metadata.first_date) if metadata.first_date else None,
                last_date=str(metadata.last_date) if metadata.last_date else None,
                last_close=None,
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
) -> DataStatusSelectionOut:
    """返回匹配筛选条件的全部缓存项,供发布页一键全选。

    该端点冻结一次筛选结果及其整体日期范围,避免前端逐页请求后漏选。
    """
    from finboard_data.cache import ParquetCache

    cache = ParquetCache(_CACHE_DIR, max_io_concurrency=8)
    cache_path = Path(_CACHE_DIR)
    parquet_files = sorted(
        await asyncio.to_thread(lambda: list(cache_path.glob("*.parquet")))
    )
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
        ):
            continue
        selected_paths.append((path, code, period_str, adjustment))

    items: list[DataStatusOut] = []
    first_dates: list[str] = []
    last_dates: list[str] = []
    for offset in range(0, len(selected_paths), 256):
        batch = selected_paths[offset : offset + 256]
        metadata_batch = await asyncio.gather(
            *(cache.metadata(path) for path, *_ in batch)
        )
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
                    period=period_str,
                    adjust=adjustment,
                    bar_count=metadata.bar_count,
                    first_date=first_date,
                    last_date=last_date,
                    last_close=None,
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
    )


@router.post("/fetch", response_model=FetchResultOut)
async def fetch_data(req: DataFetchRequest) -> FetchResultOut:
    """触发单个标的的数据拉取。"""
    from finboard_data.cache import make_symbol
    from finboard_shared.types import BarPeriod

    provider = _get_provider()
    sym = make_symbol(req.symbol)
    try:
        bars = await provider.fetch_bars(
            sym,
            BarPeriod.D1,
            parse_date.fromisoformat(req.start),
            parse_date.fromisoformat(req.end),
            adjust=req.adjust,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"数据拉取失败(网络/数据源错误): {exc}",
        ) from exc
    return FetchResultOut(
        symbol=req.symbol,
        bar_count=len(bars),
        first_date=str(bars[0].timestamp.date()) if bars else None,
        last_date=str(bars[-1].timestamp.date()) if bars else None,
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
                    passed=qr.passed,
                    primary_source=qr.sources[0] if qr.sources else "",
                )
            )
        except Exception:
            logger.exception("quality_check_failed", symbol=code)

    return results


@router.post("/fetch-all", response_model=BatchFetchResultOut)
async def fetch_all_data() -> BatchFetchResultOut:
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
    provider = _get_provider()
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
@router.get("/instruments", response_model=InstrumentListOut)
async def list_instruments(
    market: str | None = None,
    instrument_type: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
    session: AsyncSession = Depends(get_db_session),
) -> InstrumentListOut:
    """列出数据库中的标的(分页,可选模糊搜索)。"""
    from finboard_persistence import InstrumentRepository

    repo = InstrumentRepository(session)
    rows, total = await repo.list_active(
        market=market,
        instrument_type=instrument_type,
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
                status=r.status,
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
    q: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> list[str]:
    """返回匹配条件的全部标的代码(不分页,供前端"全选"使用)。"""
    from finboard_persistence import InstrumentRepository

    repo = InstrumentRepository(session)
    codes = await repo.list_codes(market=market, instrument_type=instrument_type, q=q)
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
    import os
    from datetime import date as parse_d

    state = _get_bulk_state(request)
    if state["status"] == "running":
        raise HTTPException(status_code=409, detail="批量拉取正在运行中")

    from finboard_data import AkShareProvider, YFinanceProvider
    from finboard_data.cache import make_symbol
    from finboard_persistence import InstrumentRepository
    from finboard_shared.types import BarPeriod

    repo = InstrumentRepository(session)
    instruments, _total = await repo.list_active(
        market=req.market,
        instrument_type=req.instrument_type,
        limit=999999,
    )
    await session.commit()

    if not instruments:
        raise HTTPException(status_code=400, detail="未找到匹配的标的(请先同步)")

    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "akshare")
    if provider_name == "akshare":
        primary: AkShareProvider | YFinanceProvider = AkShareProvider(
            max_concurrency=2, request_interval=0.5
        )
        fallback: AkShareProvider | YFinanceProvider | None = YFinanceProvider(
            max_concurrency=3, request_interval=0.3
        )
    else:
        primary = YFinanceProvider(max_concurrency=3, request_interval=0.3)
        fallback = AkShareProvider(max_concurrency=2, request_interval=0.5)

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
        quality_reports=[],
    )

    async def _run_download() -> None:
        try:

            def on_progress(code: str, done: int, total: int) -> None:
                state["done"] = done
                state["total"] = total

            def on_status(code: str, phase: str) -> None:
                state["current_symbol"] = code
                state["phase"] = phase

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

            # Phase 2: quality check + fallback repair
            from finboard_data.cache import ParquetCache
            from finboard_data.quality import BarQualityChecker
            from finboard_shared.models import Bar

            cache = ParquetCache(_CACHE_DIR)
            checker = BarQualityChecker()
            quality_reports: list[QualityReportOut] = []
            q_passed = 0
            q_failed = 0
            fb_used = 0

            for sym in sym_objs:
                try:
                    bars = await cache.read(sym, BarPeriod.D1, "qfq")
                    qr = checker.check(bars, symbol=sym.code)

                    corrected_dates: list[str] = []
                    fallback_source: str | None = None

                    if not qr.passed and qr.anomaly_count > 0 and fallback:
                        state["phase"] = f"quality_repair:{sym.code}"
                        try:
                            alt_bars = await fallback.fetch_bars(
                                sym, BarPeriod.D1,
                                start_date, end_date,
                                adjust="qfq",
                            )
                            alt_map = {b.timestamp.date(): b for b in alt_bars}
                            merged = list(bars)
                            for anomaly in qr.anomalies:
                                alt = alt_map.get(anomaly.date)
                                if alt and not checker._check_bar(alt):
                                    idx = next(
                                        (i for i, b in enumerate(merged)
                                         if b.timestamp.date() == anomaly.date),
                                        None,
                                    )
                                    if idx is not None:
                                        merged[idx] = Bar(
                                            symbol=alt.symbol,
                                            period=alt.period,
                                            timestamp=alt.timestamp,
                                            open=alt.open,
                                            high=alt.high,
                                            low=alt.low,
                                            close=alt.close,
                                            volume=alt.volume,
                                            amount=alt.amount,
                                            source="fallback",
                                        )
                                        corrected_dates.append(str(anomaly.date))

                            if corrected_dates:
                                await cache.write(sym, BarPeriod.D1, "qfq", merged)
                                qr = checker.check(merged, symbol=sym.code)
                                fb_used += 1
                                fallback_source = "yfinance" if provider_name == "akshare" else "akshare"
                        except Exception:
                            logger.warning("bulk_download.fallback_failed", symbol=sym.code)

                    if qr.passed:
                        q_passed += 1
                    else:
                        q_failed += 1

                    quality_reports.append(
                        QualityReportOut(
                            symbol=sym.code,
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
                            passed=qr.passed,
                            primary_source=provider_name,
                            fallback_used=bool(corrected_dates),
                            fallback_source=fallback_source,
                            corrected_dates=corrected_dates,
                        )
                    )
                except Exception:
                    logger.exception("bulk_download.quality_check_failed", symbol=sym.code)

            state["quality_passed"] = q_passed
            state["quality_failed"] = q_failed
            state["fallback_used"] = fb_used
            state["quality_reports"] = quality_reports
            state["status"] = "done"
            state["current_symbol"] = None
            state["phase"] = None
        except Exception as exc:
            state["status"] = "error"
            state["phase"] = None
            state["error"] = str(exc)

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
async def get_scheduler_config() -> SchedulerConfigOut:
    """获取定时任务配置。"""
    import os

    cfg = _load_config()
    return SchedulerConfigOut(
        sync_enabled=cfg.get("sync_enabled", True),
        sync_time=cfg.get("sync_time", "15:35"),
        download_enabled=cfg.get("download_enabled", True),
        download_time=cfg.get("download_time", "15:45"),
        download_lookback_days=cfg.get("download_lookback_days", 5),
        download_markets=cfg.get("download_markets", ["a_share"]),
        download_types=cfg.get("download_types", ["stock", "etf"]),
        data_provider=os.getenv("FINBOARD_DATA_PROVIDER", "akshare"),
    )


@router.put("/config", response_model=SchedulerConfigOut)
async def update_scheduler_config(req: SchedulerConfigUpdate) -> SchedulerConfigOut:
    """更新定时任务配置。"""
    import os

    cfg = _load_config()
    updates = req.model_dump(exclude_none=True)
    cfg.update(updates)
    _save_config(cfg)

    return SchedulerConfigOut(
        sync_enabled=cfg.get("sync_enabled", True),
        sync_time=cfg.get("sync_time", "15:35"),
        download_enabled=cfg.get("download_enabled", True),
        download_time=cfg.get("download_time", "15:45"),
        download_lookback_days=cfg.get("download_lookback_days", 5),
        download_markets=cfg.get("download_markets", ["a_share"]),
        download_types=cfg.get("download_types", ["stock", "etf"]),
        data_provider=os.getenv("FINBOARD_DATA_PROVIDER", "akshare"),
    )
