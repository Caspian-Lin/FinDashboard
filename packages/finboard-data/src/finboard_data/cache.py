"""本地 parquet 缓存 —— 避免重复下载历史数据。

缓存文件命名:``{cache_dir}/{symbol}_{period}_{adjust}.parquet``
列:symbol, period, timestamp, open, high, low, close, volume, amount

首次拉取时全量写入;后续调用自动增量合并(按 timestamp 去重,保留最新)。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import structlog

from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

logger = structlog.get_logger(__name__)


def _initialize_pyarrow() -> None:
    """在提交线程任务前初始化 PyArrow 原生模块。

    PyArrow 仍然只在实际访问 Parquet 时加载。初始化与其他 Python 扩展模块
    同样留在调用线程,避免长生命周期进程第一次在 asyncio executor 中导入
    原生模块时发生不稳定;真正的文件 I/O 继续在线程中执行。
    """

    __import__("pyarrow")
    __import__("pyarrow.parquet")


def expected_last_bar_date(end: date, *, today: date | None = None) -> date:
    """周末请求回退到最近工作日,避免缓存永远差一天。"""
    current = today or date.today()
    expected = min(end, current)
    while expected.weekday() >= 5:
        expected -= timedelta(days=1)
    return expected


def incremental_fetch_start(
    cached: list[Bar],
    requested_start: date,
    *,
    overlap_days: int = 7,
) -> date:
    """从缓存尾部附近增量刷新,避免重复请求整个历史区间。"""
    if not cached:
        return requested_start
    return max(
        requested_start,
        cached[-1].timestamp.date() - timedelta(days=overlap_days),
    )


@dataclass(frozen=True, slots=True)
class CacheMetadata:
    """不读取行情列即可取得的 Parquet 文件概要。"""

    bar_count: int
    first_date: date | None
    last_date: date | None
    file_size: int
    source: str | None = None


@dataclass(frozen=True, slots=True)
class CacheIOStats:
    """缓存逻辑 I/O 计数,用于定位批量任务的读写放大。"""

    read_ops: int
    read_bytes: int
    write_ops: int
    write_bytes: int


class ParquetCache:
    """基于 parquet 文件的本地行情缓存。

    ``pyarrow`` 为可选依赖,仅在实际读写时 import。
    缺失时 ``read`` / ``write`` 会抛 ``ImportError``。
    """

    def __init__(
        self,
        cache_dir: str | Path = "data_cache",
        *,
        max_io_concurrency: int = 1,
    ) -> None:
        if max_io_concurrency < 1:
            raise ValueError("max_io_concurrency 必须 >= 1")
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._io_semaphore = asyncio.Semaphore(max_io_concurrency)
        self._read_ops = 0
        self._read_bytes = 0
        self._write_ops = 0
        self._write_bytes = 0

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
        _initialize_pyarrow()
        size = path.stat().st_size
        started = time.monotonic()
        async with self._io_semaphore:
            bars = await asyncio.to_thread(self._read_sync, path, symbol, period)
        self._read_ops += 1
        self._read_bytes += size
        logger.debug(
            "parquet_cache.read",
            path=str(path),
            bytes=size,
            bars=len(bars),
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return bars

    def _read_sync(self, path: Path, symbol: Symbol, period: BarPeriod) -> list[Bar]:
        import pyarrow.parquet as pq

        # 外层已经限制并发;禁止 Arrow 再启动内部 I/O 线程池放大磁盘压力。
        table = pq.read_table(path, use_threads=False, pre_buffer=False)
        col_names = set(table.column_names)
        bars: list[Bar] = []
        for row in table.to_pylist():
            ts = row["timestamp"]
            if isinstance(ts, str):
                dt = datetime.fromisoformat(ts)
            else:
                dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            elif period == BarPeriod.D1:
                dt = dt.astimezone(UTC)
            if (
                period == BarPeriod.D1
                and symbol.code.endswith((".SH", ".SZ", ".BJ"))
                and dt.hour == 16
            ):
                dt += timedelta(hours=8)
            if period == BarPeriod.D1:
                dt = datetime.combine(dt.date(), datetime.min.time(), tzinfo=UTC)
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
                    source=str(row.get("source", "")) if "source" in col_names else "",
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
        _initialize_pyarrow()
        path = self._path(symbol, period, adjust)
        started = time.monotonic()
        async with self._io_semaphore:
            await asyncio.to_thread(self._write_sync, path, bars)
        size = path.stat().st_size
        self._write_ops += 1
        self._write_bytes += size
        logger.debug(
            "parquet_cache.write",
            path=str(path),
            bytes=size,
            bars=len(bars),
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )

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
            "source": [b.source for b in bars],
        }
        table = pa.table(data)
        source_names = {bar.source for bar in bars if bar.source}
        source = (
            next(iter(source_names))
            if len(source_names) == 1
            else ("mixed" if source_names else None)
        )
        if source is not None:
            table = table.replace_schema_metadata({b"finboard.source": source.encode()})
        pq.write_table(table, path)
        self._write_metadata_sidecar(
            path,
            CacheMetadata(
                bar_count=len(bars),
                first_date=bars[0].timestamp.date(),
                last_date=bars[-1].timestamp.date(),
                file_size=path.stat().st_size,
                source=source,
            ),
        )

    async def merge(
        self,
        symbol: Symbol,
        period: BarPeriod,
        adjust: str,
        new_bars: list[Bar],
        *,
        existing_bars: list[Bar] | None = None,
    ) -> list[Bar]:
        """增量合并:读取已有缓存,合并新数据(按 timestamp 去重),写回磁盘。

        :returns: 合并后的完整 Bar 列表(升序)
        """
        existing = (
            existing_bars if existing_bars is not None else await self.read(symbol, period, adjust)
        )
        merged: dict[datetime, Bar] = {b.timestamp: b for b in existing}
        for b in new_bars:
            merged[b.timestamp] = b
        all_bars = sorted(merged.values(), key=lambda b: b.timestamp)
        await self.write(symbol, period, adjust, all_bars)
        return all_bars

    async def metadata(self, path: Path) -> CacheMetadata:
        """只读取 Parquet footer,不解码 OHLCV 行情列。"""
        _initialize_pyarrow()
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        started = time.monotonic()
        async with self._io_semaphore:
            metadata = await asyncio.to_thread(self._metadata_sync, path, size)
        self._read_ops += 1
        self._read_bytes += min(size, 64 * 1024)
        logger.debug(
            "parquet_cache.metadata",
            path=str(path),
            bytes=size,
            bars=metadata.bar_count,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return metadata

    async def metadata_for(
        self,
        symbol: Symbol,
        period: BarPeriod,
        adjust: str,
    ) -> CacheMetadata | None:
        path = self._path(symbol, period, adjust)
        if not path.exists():
            return None
        return await self.metadata(path)

    @staticmethod
    def _metadata_sync(path: Path, size: int) -> CacheMetadata:
        import pyarrow.parquet as pq

        sidecar = ParquetCache._read_metadata_sidecar(path, size)
        if sidecar is not None:
            if sidecar.source is not None:
                return sidecar
            try:
                source_column = pq.read_table(
                    path,
                    columns=["source"],
                    use_threads=False,
                    pre_buffer=False,
                ).column("source")
                source_names = {
                    str(value)
                    for value in source_column.to_pylist()
                    if value
                }
                sidecar_source = (
                    next(iter(source_names))
                    if len(source_names) == 1
                    else ("mixed" if source_names else None)
                )
                enriched = CacheMetadata(
                    bar_count=sidecar.bar_count,
                    first_date=sidecar.first_date,
                    last_date=sidecar.last_date,
                    file_size=sidecar.file_size,
                    source=sidecar_source,
                )
                ParquetCache._write_metadata_sidecar(path, enriched)
                return enriched
            except (KeyError, OSError, ValueError):
                return sidecar

        parquet = pq.ParquetFile(path)
        file_metadata = parquet.metadata
        source: str | None = None
        raw_source = (parquet.schema_arrow.metadata or {}).get(b"finboard.source")
        if raw_source:
            source = raw_source.decode("utf-8", errors="replace")
        if source is None and "source" in parquet.schema.names:
            source_column = pq.read_table(
                path,
                columns=["source"],
                use_threads=False,
                pre_buffer=False,
            ).column("source")
            source_names = {
                str(value)
                for value in source_column.to_pylist()
                if value
            }
            source = (
                next(iter(source_names))
                if len(source_names) == 1
                else ("mixed" if source_names else None)
            )
        first: datetime | None = None
        last: datetime | None = None

        try:
            timestamp_index = file_metadata.schema.names.index("timestamp")
        except ValueError:
            timestamp_index = -1

        if timestamp_index >= 0:
            for index in range(file_metadata.num_row_groups):
                statistics = file_metadata.row_group(index).column(timestamp_index).statistics
                if statistics is None or not statistics.has_min_max:
                    continue
                minimum = statistics.min
                maximum = statistics.max
                if isinstance(minimum, datetime):
                    first = minimum if first is None else min(first, minimum)
                if isinstance(maximum, datetime):
                    last = maximum if last is None else max(last, maximum)

        if "_1d_" in path.stem:
            is_a_share = any(suffix in path.stem for suffix in (".SH_", ".SZ_", ".BJ_"))
            if is_a_share and first is not None and first.hour == 16:
                first += timedelta(hours=8)
            if is_a_share and last is not None and last.hour == 16:
                last += timedelta(hours=8)

        metadata = CacheMetadata(
            bar_count=file_metadata.num_rows,
            first_date=first.date() if first is not None else None,
            last_date=last.date() if last is not None else None,
            file_size=size,
            source=source,
        )
        ParquetCache._write_metadata_sidecar(path, metadata)
        return metadata

    @staticmethod
    def _sidecar_path(path: Path) -> Path:
        return path.with_suffix(path.suffix + ".meta.json")

    @staticmethod
    def _read_metadata_sidecar(path: Path, size: int) -> CacheMetadata | None:
        sidecar = ParquetCache._sidecar_path(path)
        if not sidecar.exists():
            return None
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            stat = path.stat()
            if (
                payload.get("source_size") != size
                or payload.get("source_mtime_ns") != stat.st_mtime_ns
            ):
                return None
            return CacheMetadata(
                bar_count=int(payload["bar_count"]),
                first_date=(
                    date.fromisoformat(payload["first_date"])
                    if payload.get("first_date")
                    else None
                ),
                last_date=(
                    date.fromisoformat(payload["last_date"])
                    if payload.get("last_date")
                    else None
                ),
                file_size=size,
                source=(str(payload["source"]) if payload.get("source") else None),
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_metadata_sidecar(path: Path, metadata: CacheMetadata) -> None:
        stat = path.stat()
        sidecar = ParquetCache._sidecar_path(path)
        temporary = sidecar.with_suffix(sidecar.suffix + ".tmp")
        payload = {
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "bar_count": metadata.bar_count,
            "first_date": (
                metadata.first_date.isoformat() if metadata.first_date else None
            ),
            "last_date": metadata.last_date.isoformat() if metadata.last_date else None,
            "source": metadata.source,
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(sidecar)

    def io_stats(self) -> CacheIOStats:
        return CacheIOStats(
            read_ops=self._read_ops,
            read_bytes=self._read_bytes,
            write_ops=self._write_ops,
            write_bytes=self._write_bytes,
        )

    @staticmethod
    def filter_by_date(bars: list[Bar], start: date, end: date) -> list[Bar]:
        """按日期范围过滤(左闭右闭)。"""
        return [b for b in bars if start <= b.timestamp.date() <= end]


def make_symbol(code: str) -> Symbol:
    """从 ``510300.SH`` / ``000001.SZ`` 推断 Market 并构造 Symbol。"""
    upper = code.upper()
    if upper.endswith((".SH", ".SZ")):
        return Symbol(code=upper, market=Market.A_SHARE)
    if upper.endswith(".BJ"):
        return Symbol(code=upper, market=Market.A_SHARE)
    return Symbol(code=upper, market=Market.A_SHARE)
