"""历史行情数据管理端点。"""

from __future__ import annotations

import asyncio
from datetime import date as parse_date
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter

from finboard_api.schemas import (
    BatchFetchResultOut,
    DataFetchRequest,
    DataStatusOut,
    FetchResultOut,
    SymbolEntrySchema,
    SymbolPoolOut,
    SymbolPoolUpdate,
)

if TYPE_CHECKING:
    from finboard_data import AkShareProvider

router = APIRouter(prefix="/api/data", tags=["data"])

_CACHE_DIR = "data_cache"
_SYMBOLS_FILE = "symbols.yaml"


def _get_provider() -> AkShareProvider:
    from finboard_data import AkShareProvider

    return AkShareProvider()


@router.get("/status", response_model=list[DataStatusOut])
async def list_cache_status() -> list[DataStatusOut]:
    """列出所有已缓存标的的状态。"""
    from finboard_data.cache import ParquetCache
    from finboard_shared.models import Symbol as Sym
    from finboard_shared.types import BarPeriod, Market

    cache = ParquetCache(_CACHE_DIR)
    cache_path = Path(_CACHE_DIR)
    parquet_files = await asyncio.to_thread(lambda: list(cache_path.glob("*.parquet")))

    result: list[DataStatusOut] = []
    for f in parquet_files:
        parts = f.stem.rsplit("_", 2)
        if len(parts) != 3:
            continue
        code, period_str, adjust = parts
        bars = await cache.read(
            Sym(code=code, market=Market.A_SHARE),
            BarPeriod(period_str),
            adjust,
        )
        result.append(
            DataStatusOut(
                symbol=code,
                period=period_str,
                adjust=adjust,
                bar_count=len(bars),
                first_date=str(bars[0].timestamp.date()) if bars else None,
                last_date=str(bars[-1].timestamp.date()) if bars else None,
                last_close=bars[-1].close if bars else None,
            )
        )
    return result


@router.get("/status/{symbol}", response_model=DataStatusOut)
async def get_cache_status(symbol: str) -> DataStatusOut:
    """查看单标的缓存详情。"""
    from finboard_data.cache import ParquetCache, make_symbol
    from finboard_shared.types import BarPeriod

    cache = ParquetCache(_CACHE_DIR)
    sym = make_symbol(symbol)
    bars = await cache.read(sym, BarPeriod.D1, "qfq")
    return DataStatusOut(
        symbol=symbol,
        period="D1",
        adjust="qfq",
        bar_count=len(bars),
        first_date=str(bars[0].timestamp.date()) if bars else None,
        last_date=str(bars[-1].timestamp.date()) if bars else None,
        last_close=bars[-1].close if bars else None,
    )


@router.post("/fetch", response_model=FetchResultOut)
async def fetch_data(req: DataFetchRequest) -> FetchResultOut:
    """触发单个标的的数据拉取。"""
    from finboard_data.cache import make_symbol
    from finboard_shared.types import BarPeriod

    provider = _get_provider()
    sym = make_symbol(req.symbol)
    bars = await provider.fetch_bars(
        sym,
        BarPeriod.D1,
        parse_date.fromisoformat(req.start),
        parse_date.fromisoformat(req.end),
        adjust=req.adjust,
    )
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
        symbols=[
            SymbolEntrySchema(code=s.code, name=s.name) for s in config.symbols
        ],
        fetch_period=config.fetch_period,
        fetch_lookback_days=config.fetch_lookback_days,
        fetch_adjust=config.fetch_adjust,
    )


@router.put("/symbols", response_model=SymbolPoolOut)
async def update_symbol_pool(req: SymbolPoolUpdate) -> SymbolPoolOut:
    """更新标的池配置。"""
    from finboard_data import SymbolEntry, SymbolPoolConfig, save_symbol_pool

    config = SymbolPoolConfig(
        symbols=[
            SymbolEntry(code=s.code, name=s.name) for s in req.symbols
        ],
        fetch_period=req.fetch_period,
        fetch_lookback_days=req.fetch_lookback_days,
        fetch_adjust=req.fetch_adjust,
    )
    save_symbol_pool(config, _SYMBOLS_FILE)
    return SymbolPoolOut(
        symbols=[
            SymbolEntrySchema(code=s.code, name=s.name) for s in config.symbols
        ],
        fetch_period=config.fetch_period,
        fetch_lookback_days=config.fetch_lookback_days,
        fetch_adjust=config.fetch_adjust,
    )
