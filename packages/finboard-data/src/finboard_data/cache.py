"""本地 parquet 缓存 —— 避免重复下载历史数据。

缓存文件命名:``{cache_dir}/{symbol}_{period}_{adjust}.parquet``
列:symbol, period, timestamp, open, high, low, close, volume, amount

首次拉取时全量写入;后续调用自动增量合并(按 timestamp 去重,保留最新)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
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


def _normalise_timestamp(
    value: object,
    symbol: Symbol,
    period: BarPeriod,
) -> datetime:
    """把 Arrow/Pandas/字符串时间统一为领域层 timestamp。"""

    if isinstance(value, str):
        timestamp = datetime.fromisoformat(value)
    elif isinstance(value, datetime):
        timestamp = value
    elif hasattr(value, "to_pydatetime"):
        timestamp = value.to_pydatetime()
    else:
        raise TypeError(f"Parquet timestamp 类型无效: {type(value).__name__}")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    elif period == BarPeriod.D1:
        timestamp = timestamp.astimezone(UTC)
    if (
        period == BarPeriod.D1
        and symbol.code.endswith((".SH", ".SZ", ".BJ"))
        and timestamp.hour == 16
    ):
        timestamp += timedelta(hours=8)
    if period == BarPeriod.D1:
        timestamp = datetime.combine(
            timestamp.date(), datetime.min.time(), tzinfo=UTC
        )
    return timestamp


def expected_last_bar_date(
    end: date, *, today: date | None = None, now: datetime | None = None
) -> date:
    """计算应已落盘的最后一个交易日。

    周末自动回退到最近工作日。若 ``end`` 落在今天、且当前本地时间早于
    21:00(A 股 15:00 收盘 + 6 小时数据发布窗口),则连今天也回退到前一个
    工作日,避免盘前/盘中批量拉取时缓存永远差"今天"一天从而反复请求。
    """
    current = today or date.today()
    expected = min(end, current)
    while expected.weekday() >= 5:
        expected -= timedelta(days=1)
    if expected == current and end >= current:
        now_dt = now or datetime.now()
        if now_dt.hour < 21:
            expected -= timedelta(days=1)
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
    covered_ranges: tuple[tuple[date, date], ...] = ()

    def covers(self, start: date, end: date) -> bool:
        """缓存是否已成功查询过整个请求区间。

        Bar 的首尾区间也视为天然覆盖;显式覆盖区间用于记录停牌、上市前等
        合法无 Bar 日期,避免这些日期在下一次批量同步时被重复请求。
        """
        if start > end:
            return True
        ranges = list(self.covered_ranges)
        if self.first_date is not None and self.last_date is not None:
            ranges.append((self.first_date, self.last_date))
        return any(
            range_start <= start and range_end >= end
            for range_start, range_end in _coalesce_date_ranges(ranges)
        )


@dataclass(frozen=True, slots=True)
class CacheIOStats:
    """缓存逻辑 I/O 计数,用于定位批量任务的读写放大。"""

    read_ops: int
    read_bytes: int
    write_ops: int
    write_bytes: int


@dataclass(slots=True)
class ParquetReadJobStats:
    """job 级 parquet 读取聚合计时(issue #285)。

    由 :func:`collect_parquet_read_stats` 激活,``ParquetCache`` 的读取入口
    (``read`` / ``read_close_points`` / ``metadata``)在入口处累加,聚合出
    单个 job 内的读取次数 / 累计耗时 / 累计字节,回答「慢在 IO 还是计算」。
    不改动任何读取内部逻辑;未激活时零开销。
    """

    read_ops: int = 0
    read_elapsed_ms: float = 0.0
    read_bytes: int = 0
    #: 按读取入口细分的次数(read / read_close_points / metadata)。
    ops_by_entry: dict[str, int] = field(default_factory=dict)

    def record(self, entry: str, *, elapsed_ms: float, size_bytes: int) -> None:
        self.read_ops += 1
        self.read_elapsed_ms += elapsed_ms
        self.read_bytes += size_bytes
        self.ops_by_entry[entry] = self.ops_by_entry.get(entry, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "read_ops": self.read_ops,
            "read_elapsed_ms": round(self.read_elapsed_ms, 2),
            "read_bytes": self.read_bytes,
            "ops_by_entry": dict(sorted(self.ops_by_entry.items())),
        }


_PARQUET_READ_STATS: ContextVar[ParquetReadJobStats | None] = ContextVar(
    "finboard_parquet_read_stats", default=None
)


@contextlib.contextmanager
def collect_parquet_read_stats() -> Iterator[ParquetReadJobStats]:
    """在当前上下文(asyncio task)内聚合 parquet 读取耗时(issue #285)。

    用法::

        with collect_parquet_read_stats() as stats:
            ...  # 数据加载 / 回测运行
        print(stats.as_dict())

    asyncio task 创建时复制 contextvar,worker 每 job 一个 task,天然按 job
    隔离;嵌套激活以内层为准(当前无嵌套使用方)。
    """

    stats = ParquetReadJobStats()
    token = _PARQUET_READ_STATS.set(stats)
    try:
        yield stats
    finally:
        _PARQUET_READ_STATS.reset(token)


def _record_job_read(entry: str, *, elapsed_ms: float, size_bytes: int) -> None:
    """读取入口处向已激活的 job 级聚合句柄累加(未激活时静默跳过)。"""

    stats = _PARQUET_READ_STATS.get()
    if stats is not None:
        stats.record(entry, elapsed_ms=elapsed_ms, size_bytes=size_bytes)


#: 进程内读缓存的默认元素上限(bars 与 close 点位合并计数,LRU 驱逐)。
#: 内存护栏:约 2e6 根日线 Bar 最坏情形数百 MB;典型日频文件 ~250 根,
#: 足以容纳数千只标的的全区间工作集(multi_period 回放逐期扫描全部标的,
#: 缓存容量须 ≥ 工作集才不抖动)。置 0 关闭缓存。
DEFAULT_READ_CACHE_MAX_ELEMENTS = 2_000_000

#: 读缓存条目种类(bars / close 点位),参与缓存键。
_READ_CACHE_KIND_BARS = "bars"
_READ_CACHE_KIND_CLOSE_POINTS = "close_points"


@dataclass(frozen=True, slots=True)
class _ReadCacheEntry:
    """进程内读缓存条目:反序列化结果 + 元素计数(bars / 点位二选一)。"""

    bars: list[Bar] | None
    points: list[tuple[datetime, Decimal]] | None
    elements: int


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
        read_cache_max_elements: int | None = None,
    ) -> None:
        if max_io_concurrency < 1:
            raise ValueError("max_io_concurrency 必须 >= 1")
        if read_cache_max_elements is None:
            read_cache_max_elements = DEFAULT_READ_CACHE_MAX_ELEMENTS
        if read_cache_max_elements < 0:
            raise ValueError("read_cache_max_elements 必须 >= 0")
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._io_semaphore = asyncio.Semaphore(max_io_concurrency)
        self._read_ops = 0
        self._read_bytes = 0
        self._write_ops = 0
        self._write_bytes = 0
        # issue #287:进程内读缓存(实例作用域)。键含文件 size + mtime_ns,
        # 任何写入(本实例 write / 外部进程改写)都会因键变化自然失效;
        # write 后再显式丢弃同路径条目,避免陈旧条目占用内存等 LRU 驱逐。
        self._read_cache_max_elements = read_cache_max_elements
        self._read_cache: OrderedDict[
            tuple[str, int, int, str], _ReadCacheEntry
        ] = OrderedDict()
        self._read_cache_elements = 0
        self._read_cache_hits = 0

    # ---- 进程内读缓存(LRU)------------------------------------------------

    @staticmethod
    def _read_cache_key(
        path: Path, stat: os.stat_result, kind: str
    ) -> tuple[str, int, int, str]:
        return (str(path), stat.st_size, stat.st_mtime_ns, kind)

    def _read_cache_get(self, key: tuple[str, int, int, str]) -> _ReadCacheEntry | None:
        entry = self._read_cache.get(key)
        if entry is None:
            return None
        self._read_cache.move_to_end(key)
        self._read_cache_hits += 1
        return entry

    def _read_cache_put(
        self, key: tuple[str, int, int, str], entry: _ReadCacheEntry
    ) -> None:
        if entry.elements > self._read_cache_max_elements:
            # 单文件超过整个缓存预算:不缓存(避免把其它条目全部挤掉)。
            return
        while (
            self._read_cache
            and self._read_cache_elements + entry.elements > self._read_cache_max_elements
        ):
            _, evicted = self._read_cache.popitem(last=False)
            self._read_cache_elements -= evicted.elements
        self._read_cache[key] = entry
        self._read_cache_elements += entry.elements

    def _read_cache_drop_path(self, path: Path) -> None:
        """丢弃某路径的全部缓存条目(write 后调用,防陈旧条目滞留)。"""
        prefix = str(path)
        for key in [item for item in self._read_cache if item[0] == prefix]:
            entry = self._read_cache.pop(key)
            self._read_cache_elements -= entry.elements

    def read_cache_info(self) -> dict[str, int]:
        """进程内读缓存概况(观测 / 测试用):条目数、元素数、命中次数。"""
        return {
            "entries": len(self._read_cache),
            "elements": self._read_cache_elements,
            "hits": self._read_cache_hits,
        }

    def _path(self, symbol: Symbol, period: BarPeriod, adjust: str) -> Path:
        return self._dir / f"{symbol.code}_{period.value}_{adjust}.parquet"

    async def read(
        self,
        symbol: Symbol,
        period: BarPeriod,
        adjust: str,
    ) -> list[Bar]:
        """读取缓存;文件不存在时返回空列表。

        进程内读缓存(issue #287):同一文件(键 = 路径 + size + mtime_ns)
        的重复读取直接返回上次反序列化结果,不再重读磁盘。命中与否通过
        debug 日志的 ``cache_hit`` 字段可见。返回值为缓存列表的浅拷贝,
        调用方的就地修改不影响缓存。
        """
        path = self._path(symbol, period, adjust)
        if not path.exists():
            return []
        stat = path.stat()
        key = self._read_cache_key(path, stat, _READ_CACHE_KIND_BARS)
        started = time.monotonic()
        entry = self._read_cache_get(key)
        if entry is not None and entry.bars is not None:
            # 命中同样计入 job 级读取聚合(issue #285 语义:入口处计数,
            # 命中耗时 ~0 如实反映缓存收益);io_stats 的磁盘 I/O 口径不计命中。
            _record_job_read(
                "read",
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                size_bytes=stat.st_size,
            )
            logger.debug(
                "parquet_cache.read",
                path=str(path),
                bytes=stat.st_size,
                bars=len(entry.bars),
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                cache_hit=True,
            )
            return list(entry.bars)
        _initialize_pyarrow()
        size = stat.st_size
        async with self._io_semaphore:
            bars = await asyncio.to_thread(self._read_sync, path, symbol, period)
        self._read_cache_put(
            key, _ReadCacheEntry(bars=bars, points=None, elements=len(bars))
        )
        self._read_ops += 1
        self._read_bytes += size
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        _record_job_read("read", elapsed_ms=elapsed_ms, size_bytes=size)
        logger.debug(
            "parquet_cache.read",
            path=str(path),
            bytes=size,
            bars=len(bars),
            elapsed_ms=elapsed_ms,
            cache_hit=False,
        )
        return list(bars)

    async def read_close_points(
        self,
        symbol: Symbol,
        period: BarPeriod,
        adjust: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[tuple[datetime, Decimal]]:
        """只读取 timestamp/close 列,供价格特征计算使用。

        特征快照不需要 OHLCV 的其余字段。保留独立读取入口可以避免为每条
        历史行情创建完整 ``Bar``/``Decimal`` 对象,同时不改变通用 ``read``
        的返回契约。

        进程内读缓存(issue #287):缓存整文件的 timestamp/close 点位,请求
        区间在缓存之后内存裁剪(与逐次读取的日期过滤同口径),multi_period
        回放逐期重复读取同一文件时命中缓存,``cache_hit`` debug 日志可见。
        """

        path = self._path(symbol, period, adjust)
        if not path.exists():
            return []
        stat = path.stat()
        key = self._read_cache_key(path, stat, _READ_CACHE_KIND_CLOSE_POINTS)
        started = time.monotonic()
        entry = self._read_cache_get(key)
        if entry is not None and entry.points is not None:
            points = [
                item
                for item in entry.points
                if (start is None or item[0].date() >= start)
                and (end is None or item[0].date() <= end)
            ]
            # 命中同样计入 job 级读取聚合(issue #285 语义:入口处计数,
            # 命中耗时 ~0 如实反映缓存收益);io_stats 的磁盘 I/O 口径不计命中。
            _record_job_read(
                "read_close_points",
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                size_bytes=stat.st_size,
            )
            logger.debug(
                "parquet_cache.read_close_points",
                path=str(path),
                bytes=stat.st_size,
                points=len(points),
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                cache_hit=True,
            )
            return points
        _initialize_pyarrow()
        size = stat.st_size
        async with self._io_semaphore:
            # 未命中时一次读取整文件点位并整份进缓存(区间裁剪在内存做),
            # 后续任意区间 / 下一期的读取不再触盘。
            full_points = await asyncio.to_thread(
                self._read_close_points_sync,
                path,
                symbol,
                period,
                None,
                None,
            )
        self._read_cache_put(
            key,
            _ReadCacheEntry(bars=None, points=full_points, elements=len(full_points)),
        )
        points = [
            item
            for item in full_points
            if (start is None or item[0].date() >= start)
            and (end is None or item[0].date() <= end)
        ]
        self._read_ops += 1
        self._read_bytes += size
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        _record_job_read("read_close_points", elapsed_ms=elapsed_ms, size_bytes=size)
        logger.debug(
            "parquet_cache.read_close_points",
            path=str(path),
            bytes=size,
            points=len(points),
            elapsed_ms=elapsed_ms,
            cache_hit=False,
        )
        return points

    def _read_sync(self, path: Path, symbol: Symbol, period: BarPeriod) -> list[Bar]:
        """兼容旧的线程读取入口。"""

        return self.read_bars_sync(path, symbol, period)

    @staticmethod
    def read_bars_sync(path: Path, symbol: Symbol, period: BarPeriod) -> list[Bar]:
        """同步读取完整 Bar,供线程/进程 worker 复用。"""

        import pyarrow.parquet as pq

        # 外层已经限制并发;禁止 Arrow 再启动内部 I/O 线程池放大磁盘压力。
        table = pq.read_table(path, use_threads=False, pre_buffer=False)
        col_names = set(table.column_names)
        # issue #287:按列提取(每列一次 to_pylist),避免逐行 dict 构造的
        # 开销;缺列 / 空值语义与逐行读取保持一致(volume/amount 缺列按 0,
        # source 缺列按空串,列存在但值为 None 时 str(None) 报错行为不变)。
        timestamps = table.column("timestamp").to_pylist()
        opens = table.column("open").to_pylist()
        highs = table.column("high").to_pylist()
        lows = table.column("low").to_pylist()
        closes = table.column("close").to_pylist()
        volumes = table.column("volume").to_pylist() if "volume" in col_names else None
        amounts = table.column("amount").to_pylist() if "amount" in col_names else None
        sources = table.column("source").to_pylist() if "source" in col_names else None
        bars: list[Bar] = []
        for index, raw_timestamp in enumerate(timestamps):
            dt = _normalise_timestamp(raw_timestamp, symbol, period)
            bars.append(
                Bar(
                    symbol=symbol,
                    period=period,
                    timestamp=dt,
                    open=Decimal(str(opens[index])),
                    high=Decimal(str(highs[index])),
                    low=Decimal(str(lows[index])),
                    close=Decimal(str(closes[index])),
                    volume=Decimal(str(0 if volumes is None else volumes[index])),
                    amount=Decimal(str(0 if amounts is None else amounts[index])),
                    source="" if sources is None else str(sources[index]),
                )
            )
        bars.sort(key=lambda b: b.timestamp)
        return bars

    @staticmethod
    def _read_close_points_sync(
        path: Path,
        symbol: Symbol,
        period: BarPeriod,
        start: date | None,
        end: date | None,
    ) -> list[tuple[datetime, Decimal]]:
        """兼容旧的线程读取入口。"""

        return ParquetCache.read_close_points_sync(
            path,
            symbol,
            period,
            start,
            end,
        )

    @staticmethod
    def read_close_points_sync(
        path: Path,
        symbol: Symbol,
        period: BarPeriod,
        start: date | None,
        end: date | None,
    ) -> list[tuple[datetime, Decimal]]:
        """同步读取 timestamp/close,供独立进程 worker 复用。"""

        import pyarrow.parquet as pq

        table = pq.read_table(
            path,
            columns=["timestamp", "close"],
            use_threads=False,
            pre_buffer=False,
        )
        points: list[tuple[datetime, Decimal]] = []
        for row in table.to_pylist():
            timestamp = _normalise_timestamp(row["timestamp"], symbol, period)
            business_date = timestamp.date()
            if start is not None and business_date < start:
                continue
            if end is not None and business_date > end:
                continue
            close = row.get("close")
            if close is None:
                continue
            points.append((timestamp, Decimal(str(close))))
        points.sort(key=lambda item: item[0])
        return points

    async def write(
        self,
        symbol: Symbol,
        period: BarPeriod,
        adjust: str,
        bars: list[Bar],
        *,
        covered_ranges: tuple[tuple[date, date], ...] = (),
    ) -> None:
        """全量写入(覆盖已有文件)。"""
        if not bars:
            return
        _initialize_pyarrow()
        path = self._path(symbol, period, adjust)
        started = time.monotonic()
        async with self._io_semaphore:
            await asyncio.to_thread(self._write_sync, path, bars, covered_ranges)
        # write 覆盖文件:立即丢弃该路径的进程内读缓存条目(防陈旧滞留)。
        self._read_cache_drop_path(path)
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

    def _write_sync(
        self,
        path: Path,
        bars: list[Bar],
        covered_ranges: tuple[tuple[date, date], ...] = (),
    ) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        previous: CacheMetadata | None = None
        if path.exists():
            previous = self._read_metadata_sidecar(path, path.stat().st_size)

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
        retained_ranges: tuple[tuple[date, date], ...] = ()
        if previous is not None and previous.source == source:
            retained_ranges = previous.covered_ranges
        self._write_metadata_sidecar(
            path,
            CacheMetadata(
                bar_count=len(bars),
                first_date=bars[0].timestamp.date(),
                last_date=bars[-1].timestamp.date(),
                file_size=path.stat().st_size,
                source=source,
                covered_ranges=_coalesce_date_ranges([*retained_ranges, *covered_ranges]),
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
        covered_ranges: tuple[tuple[date, date], ...] = (),
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
        await self.write(
            symbol,
            period,
            adjust,
            all_bars,
            covered_ranges=covered_ranges,
        )
        return all_bars

    async def mark_covered(
        self,
        symbol: Symbol,
        period: BarPeriod,
        adjust: str,
        start: date,
        end: date,
        *,
        source: str,
    ) -> bool:
        """记录一次成功的远端查询区间,不重写 Parquet 行情数据。"""
        if start > end:
            return True
        path = self._path(symbol, period, adjust)
        if not path.exists():
            return False
        _initialize_pyarrow()
        async with self._io_semaphore:
            return await asyncio.to_thread(
                self._mark_covered_sync,
                path,
                start,
                end,
                source,
            )

    @staticmethod
    def _mark_covered_sync(
        path: Path,
        start: date,
        end: date,
        source: str,
    ) -> bool:
        size = path.stat().st_size
        metadata = ParquetCache._metadata_sync(path, size)
        if metadata.source != source:
            return False
        ParquetCache._write_metadata_sidecar(
            path,
            CacheMetadata(
                bar_count=metadata.bar_count,
                first_date=metadata.first_date,
                last_date=metadata.last_date,
                file_size=size,
                source=metadata.source,
                covered_ranges=_coalesce_date_ranges([*metadata.covered_ranges, (start, end)]),
            ),
        )
        return True

    async def metadata(self, path: Path) -> CacheMetadata:
        """只读取 Parquet footer,不解码 OHLCV 行情列。"""
        _initialize_pyarrow()
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        started = time.monotonic()
        async with self._io_semaphore:
            metadata = await asyncio.to_thread(self._metadata_sync, path, size)
        self._read_ops += 1
        # footer-only 读取按 io_stats 同口径计字节(上限 64KB),避免 job 级
        # bytes 聚合被整文件大小虚增。
        counted_bytes = min(size, 64 * 1024)
        self._read_bytes += counted_bytes
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        _record_job_read("metadata", elapsed_ms=elapsed_ms, size_bytes=counted_bytes)
        logger.debug(
            "parquet_cache.metadata",
            path=str(path),
            bytes=size,
            bars=metadata.bar_count,
            elapsed_ms=elapsed_ms,
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
                source_names = {str(value) for value in source_column.to_pylist() if value}
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
                    covered_ranges=sidecar.covered_ranges,
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
            source_names = {str(value) for value in source_column.to_pylist() if value}
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
            covered_ranges=(),
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
                    date.fromisoformat(payload["first_date"]) if payload.get("first_date") else None
                ),
                last_date=(
                    date.fromisoformat(payload["last_date"]) if payload.get("last_date") else None
                ),
                file_size=size,
                source=(str(payload["source"]) if payload.get("source") else None),
                covered_ranges=_parse_covered_ranges(payload.get("covered_ranges")),
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
            "first_date": (metadata.first_date.isoformat() if metadata.first_date else None),
            "last_date": metadata.last_date.isoformat() if metadata.last_date else None,
            "source": metadata.source,
            "covered_ranges": [
                [range_start.isoformat(), range_end.isoformat()]
                for range_start, range_end in metadata.covered_ranges
            ],
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


def _coalesce_date_ranges(
    ranges: list[tuple[date, date]],
) -> tuple[tuple[date, date], ...]:
    """合并重叠或相邻的闭区间。"""
    valid = sorted(item for item in ranges if item[0] <= item[1])
    if not valid:
        return ()
    merged: list[tuple[date, date]] = [valid[0]]
    for range_start, range_end in valid[1:]:
        previous_start, previous_end = merged[-1]
        if range_start <= previous_end + timedelta(days=1):
            merged[-1] = (previous_start, max(previous_end, range_end))
        else:
            merged.append((range_start, range_end))
    return tuple(merged)


def _parse_covered_ranges(value: object) -> tuple[tuple[date, date], ...]:
    """容错读取 sidecar 中的已查询区间。"""
    if not isinstance(value, list):
        return ()
    ranges: list[tuple[date, date]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            continue
        try:
            ranges.append((date.fromisoformat(str(item[0])), date.fromisoformat(str(item[1]))))
        except ValueError:
            continue
    return _coalesce_date_ranges(ranges)


#: 期货交易所后缀(issue #267)。与 ``akshare_provider.FUTURES_EXCHANGES``
#: 同口径 —— 不直接 import 是因为 akshare_provider 反向依赖本模块
#: (缓存原语),常量一致性由 tests/unit/data/test_akshare_futures.py 锁定。
_FUTURE_SUFFIXES = (".CFFEX", ".CZCE", ".DCE", ".GFEX", ".INE", ".SHFE")


def make_symbol(code: str) -> Symbol:
    """从 ``510300.SH`` / ``000001.SZ`` / ``IF0.CFFEX`` 推断 Market 并构造 Symbol。

    期货交易所后缀(issue #267)→ ``Market.FUTURE``:此前未知后缀一律兜底
    ``A_SHARE``,会让冻结发布的逐标的 market 校验把期货代码误判成
    ``a_share`` 而拒绝读取;与 ``finboard_data.assets.registry`` 的
    ``_PREFIX_TABLE``(CFFEX 等 → FUTURE)对齐。
    """
    upper = code.upper()
    if upper.endswith(_FUTURE_SUFFIXES):
        return Symbol(code=upper, market=Market.FUTURE)
    if upper.endswith((".SH", ".SZ")):
        return Symbol(code=upper, market=Market.A_SHARE)
    if upper.endswith(".BJ"):
        return Symbol(code=upper, market=Market.A_SHARE)
    return Symbol(code=upper, market=Market.A_SHARE)
