"""本地 parquet 缓存 —— 避免重复下载历史数据。

缓存文件命名:``{cache_dir}/{symbol}_{period}_{adjust}.parquet``
列:symbol, period, timestamp, open, high, low, close, volume, amount

首次拉取时全量写入;后续调用自动增量合并(按 timestamp 去重,保留最新)。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import structlog

from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

logger = structlog.get_logger(__name__)


class ParquetCache:
    """基于 parquet 文件的本地行情缓存。

    ``pyarrow`` 为可选依赖,仅在实际读写时 import。
    缺失时 ``read`` / ``write`` 会抛 ``ImportError``。
    """

    def __init__(self, cache_dir: str | Path = "data_cache") -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: Symbol, period: BarPeriod, adjust: str) -> Path:
        return self._dir / f"{symbol.code}_{period.value}_{adjust}.parquet"

    async def read(
        self,
        symbol: Symbol,
        period: BarPeriod,
        adjust: str,
    ) -> list[Bar]:
        """读取缓存;文件不存在时返回空列表。"""
        path = self._path(symbol, period, adjust)
        if not path.exists():
            return []
        return await asyncio.to_thread(self._read_sync, path, symbol, period)

    def _read_sync(
        self, path: Path, symbol: Symbol, period: BarPeriod
    ) -> list[Bar]:
        import pyarrow.parquet as pq

        table = pq.read_table(path)
        bars: list[Bar] = []
        for row in table.to_pylist():
            ts = row["timestamp"]
            if isinstance(ts, str):
                dt = datetime.fromisoformat(ts)
            else:
                dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            bars.append(
                Bar(
                    symbol=symbol,
                    period=period,
                    timestamp=dt,
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=Decimal(str(row.get("volume", 0))),
                    amount=Decimal(str(row.get("amount", 0))),
                )
            )
        bars.sort(key=lambda b: b.timestamp)
        return bars

    async def write(
        self,
        symbol: Symbol,
        period: BarPeriod,
        adjust: str,
        bars: list[Bar],
    ) -> None:
        """全量写入(覆盖已有文件)。"""
        if not bars:
            return
        path = self._path(symbol, period, adjust)
        await asyncio.to_thread(self._write_sync, path, bars)

    def _write_sync(self, path: Path, bars: list[Bar]) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        data = {
            "timestamp": [b.timestamp for b in bars],
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
            "amount": [float(b.amount) for b in bars],
        }
        table = pa.table(data)
        pq.write_table(table, path)

    async def merge(
        self,
        symbol: Symbol,
        period: BarPeriod,
        adjust: str,
        new_bars: list[Bar],
    ) -> list[Bar]:
        """增量合并:读取已有缓存,合并新数据(按 timestamp 去重),写回磁盘。

        :returns: 合并后的完整 Bar 列表(升序)
        """
        existing = await self.read(symbol, period, adjust)
        merged: dict[datetime, Bar] = {b.timestamp: b for b in existing}
        for b in new_bars:
            merged[b.timestamp] = b
        all_bars = sorted(merged.values(), key=lambda b: b.timestamp)
        await self.write(symbol, period, adjust, all_bars)
        return all_bars

    @staticmethod
    def filter_by_date(
        bars: list[Bar], start: date, end: date
    ) -> list[Bar]:
        """按日期范围过滤(左闭右闭)。"""
        return [
            b
            for b in bars
            if start <= b.timestamp.date() <= end
        ]


def make_symbol(code: str) -> Symbol:
    """从 ``510300.SH`` / ``000001.SZ`` 推断 Market 并构造 Symbol。"""
    upper = code.upper()
    if upper.endswith((".SH", ".SZ")):
        return Symbol(code=upper, market=Market.A_SHARE)
    if upper.endswith(".BJ"):
        return Symbol(code=upper, market=Market.A_SHARE)
    return Symbol(code=upper, market=Market.A_SHARE)
