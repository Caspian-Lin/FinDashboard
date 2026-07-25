"""历史行情数据管理端点。"""

from __future__ import annotations

import asyncio
from datetime import date as parse_date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_session
from finboard_api.schemas import (
    BatchFetchResultOut,
    BulkDownloadRequest,
    BulkDownloadStatusOut,
    DataFetchRequest,
    DataStatusOut,
    FetchResultOut,
    InstrumentListOut,
    InstrumentOut,
    SchedulerConfigOut,
    SchedulerConfigUpdate,
    SymbolEntrySchema,
    SymbolPoolOut,
    SymbolPoolUpdate,
    SyncResultOut,
)

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
    provider_name = _PROVIDER or os.getenv("FINBOARD_DATA_PROVIDER", "yfinance")
    _PROVIDER = provider_name
    if provider_name == "akshare":
        return AkShareProvider()
    return YFinanceProvider()


@router.get("/status", response_model=list[DataStatusOut])
async def list_cache_status() -> list[DataStatusOut]:
    """列出缓存状态;只读 Parquet footer,不扫描行情列。"""
    from finboard_data.cache import ParquetCache

    cache = ParquetCache(_CACHE_DIR)
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


@router.post("/fetch-all", response_model=BatchFetchResultOut)
async def fetch_all_data() -> BatchFetchResultOut:
    """批量拉取标的池中所有标的的行情数据。"""
    from datetime import timedelta

    from finboard_data import load_symbol_pool
    from finboard_data.cache import make_symbol
    from finboard_shared.types import BarPeriod

    config = load_symbol_pool(_SYMBOLS_FILE)
    if not config.symbols:
        return BatchFetchResultOut(total=0, success=0, failed=0, details=[])

    end = parse_date.today()
    start = end - timedelta(days=config.fetch_lookback_days)
    period = BarPeriod(config.fetch_period)
    provider = _get_provider()
    sym_objs = [make_symbol(s.code) for s in config.symbols]

    results = await provider.fetch_bars_batch(
        sym_objs,
        period,
        start,
        end,
        adjust=config.fetch_adjust,
    )

    details: list[FetchResultOut] = []
    success = 0
    for entry in config.symbols:
        bars = results.get(entry.code, [])
        if bars:
            success += 1
        details.append(
            FetchResultOut(
                symbol=entry.code,
                bar_count=len(bars),
                first_date=str(bars[0].timestamp.date()) if bars else None,
                last_date=str(bars[-1].timestamp.date()) if bars else None,
            )
        )

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
    limit: int = 200,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> InstrumentListOut:
    """列出数据库中的标的(分页)。"""
    from finboard_persistence import InstrumentRepository

    repo = InstrumentRepository(session)
    rows, total = await repo.list_active(
        market=market,
        instrument_type=instrument_type,
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


@router.get("/instruments/search", response_model=list[InstrumentOut])
async def search_instruments(
    q: str,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
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
            "error": None,
        }
    return request.app.state._bulk_download  # type: ignore[no-any-return]


@router.post("/sync", response_model=SyncResultOut)
async def sync_universe(
    session: AsyncSession = Depends(get_session),
) -> SyncResultOut:
    """从 akshare 发现全市场标的,写入 instruments 表。"""
    from finboard_data.discovery import UniverseDiscovery
    from finboard_persistence import InstrumentRepository

    discovery = UniverseDiscovery()
    instruments = await discovery.discover_all()

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
    count = await repo.upsert_many(dicts)
    await session.commit()

    return SyncResultOut(total=count, new=len(dicts), updated=0)


@router.post("/bulk-download", response_model=BulkDownloadStatusOut)
async def start_bulk_download(
    request: Request,
    req: BulkDownloadRequest,
    session: AsyncSession = Depends(get_session),
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

    provider_name = os.getenv("FINBOARD_DATA_PROVIDER", "yfinance")
    if provider_name == "akshare":
        provider: AkShareProvider | YFinanceProvider = AkShareProvider(
            max_concurrency=2, request_interval=0.5
        )
    else:
        provider = YFinanceProvider(max_concurrency=3, request_interval=0.3)

    sym_objs = [make_symbol(ins.code) for ins in instruments]
    start_date = parse_d.fromisoformat(req.start)
    end_date = parse_d.today()

    state.update(status="running", done=0, total=len(sym_objs), success=0, failed=0, error=None)

    async def _run_download() -> None:
        try:

            def on_progress(code: str, done: int, total: int) -> None:
                state["done"] = done
                state["total"] = total

            results = await provider.update_cache_batch(
                sym_objs, BarPeriod.D1, start_date, end_date, on_progress=on_progress
            )
            state["success"] = sum(results.values())
            state["failed"] = len(sym_objs) - state["success"]
            state["status"] = "done"
        except Exception as exc:
            state["status"] = "error"
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
        data_provider=os.getenv("FINBOARD_DATA_PROVIDER", "yfinance"),
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
        data_provider=os.getenv("FINBOARD_DATA_PROVIDER", "yfinance"),
    )
