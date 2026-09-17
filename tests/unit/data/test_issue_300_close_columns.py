"""列式 close 直出(issue #300):与逐行对象路径逐值等值。

#300 P0 把研究域 close 消费端从「parquet → Python 逐行对象(Decimal /
tuple)→ 再转 numpy」改为「parquet → float64 / date 列式直出」。本组测试
锁定两条直出路径的等值不变量:

* timestamp 批量归一化(``_d1_midnight_us``,Arrow/numpy C 层)与逐行
  ``_normalise_timestamp`` 的 D1 分支在 naive / aware / A 股 hour==16 等
  边界网格上逐值相等;
* ``read_close_columns_sync`` 与 ``read_close_points_sync`` 在同一文件上
  (含乱序、null close、区间裁剪)产出逐值相等的日期与收盘价序列;
* 异步入口的进程内读缓存命中语义与 #285 job 级读取聚合口径
  (``read_close_columns`` 独立条目计数)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from finboard_data.cache import (
    CloseColumns,
    ParquetCache,
    _d1_midnight_us,
    _normalise_timestamp,
    collect_parquet_read_stats,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

_CST = ZoneInfo("Asia/Shanghai")


def _write_parquet(path: Path, timestamps: list[datetime], closes: list[object]) -> None:
    """直接写 timestamp/close 两列的 parquet(绕过 Bar 写路径以覆盖边界形态)。"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "timestamp": pa.array(timestamps),
            "close": pa.array(closes, type=pa.float64()),
        }
    )
    pq.write_table(table, path)


# ---- timestamp 批量归一化等值 -----------------------------------------------


class TestD1MidnightUsEquivalence:
    """向量化归一化与逐行 _normalise_timestamp 的 D1 分支逐值等值。"""

    @staticmethod
    def _check(symbol_code: str, raw_values: list[datetime], tmp_path: Path) -> None:
        symbol = Symbol(code=symbol_code, market=Market.A_SHARE)
        path = tmp_path / f"{symbol_code.replace('.', '_')}.parquet"
        _write_parquet(
            path, raw_values, [float(index) for index in range(len(raw_values))]
        )
        import pyarrow.parquet as pq

        table = pq.read_table(path, columns=["timestamp"])
        midnight_us = _d1_midnight_us(table.column("timestamp"), symbol)
        expected = [
            int(_normalise_timestamp(value, symbol, BarPeriod.D1).timestamp() * 1_000_000)
            for value in raw_values
        ]
        assert midnight_us.tolist() == expected

    def test_naive_utc_grid_all_hours(self, tmp_path: Path) -> None:
        """naive(视为 UTC)与全小时网格,含 A 股 hour==16 平移分支。"""
        base = datetime(2024, 5, 6)
        values = [base + timedelta(hours=hour, minutes=30) for hour in range(24)]
        self._check("600519.SH", values, tmp_path)
        self._check("AAPL", values, tmp_path)

    def test_aware_utc_grid_all_hours(self, tmp_path: Path) -> None:
        """aware UTC 与全小时网格。"""
        base = datetime(2024, 5, 6, tzinfo=UTC)
        values = [base + timedelta(hours=hour) for hour in range(24)]
        self._check("000001.SZ", values, tmp_path)
        self._check("AAPL", values, tmp_path)

    def test_aware_non_utc_tz(self, tmp_path: Path) -> None:
        """aware 非 UTC 时区(如 CST 15:00 = UTC 07:00)正确转 UTC 后归一化。"""
        values = [
            datetime(2024, 5, 6, hour, tzinfo=_CST).astimezone(_CST)
            for hour in range(24)
        ]
        self._check("600036.SH", values, tmp_path)

    def test_a_share_hour16_crosses_day(self, tmp_path: Path) -> None:
        """A 股 hour==16 的 +8h 平移把业务日期推进到次日(16:30 同样命中)。"""
        symbol = Symbol(code="600519.SH", market=Market.A_SHARE)
        values = [
            datetime(2024, 5, 6, 16, 0, tzinfo=UTC),
            datetime(2024, 5, 6, 16, 30, tzinfo=UTC),
        ]
        path = tmp_path / "h16.parquet"
        _write_parquet(path, values, [1.0, 2.0])
        import pyarrow.parquet as pq

        table = pq.read_table(path, columns=["timestamp"])
        midnight_us = _d1_midnight_us(table.column("timestamp"), symbol)
        normalized = [_normalise_timestamp(v, symbol, BarPeriod.D1) for v in values]
        assert all(ts.date() == date(2024, 5, 7) for ts in normalized)
        assert midnight_us.tolist() == [
            int(ts.timestamp() * 1_000_000) for ts in normalized
        ]


# ---- 列式读取与 close_points 对象路径等值 ------------------------------------


def _sample_bars(
    code: str, days: list[date], *, shift: timedelta = timedelta()
) -> list[Bar]:
    symbol = Symbol(code=code, market=Market.A_SHARE)
    return [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC) + shift,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10") + Decimal(index),
            volume=Decimal(1000),
            amount=Decimal("10000"),
            source="fixed_sample",
        )
        for index, day in enumerate(days)
    ]


class TestReadCloseColumnsEquivalence:
    """AC:列式直出与 read_close_points 对象路径逐值相等。"""

    async def test_equals_close_points_sorted_and_windowed(
        self, tmp_path: Path
    ) -> None:
        cache = ParquetCache(tmp_path / "cache")
        code = "600519.SH"
        days = [date(2024, 1, 2) + timedelta(days=index) for index in range(10)]
        await cache.write(
            Symbol(code=code, market=Market.A_SHARE), BarPeriod.D1, "qfq",
            _sample_bars(code, days),
        )
        symbol = Symbol(code=code, market=Market.A_SHARE)

        windows: list[tuple[date | None, date | None]] = [
            (None, None),
            (days[2], days[5]),
            (days[0], days[-1]),
            (date(2023, 12, 1), date(2024, 12, 31)),
            (days[4], days[4]),
            (date(2025, 1, 1), None),
        ]
        for start, end in windows:
            columns = await cache.read_close_columns(
                symbol, BarPeriod.D1, "qfq", start=start, end=end
            )
            points = await cache.read_close_points(
                symbol, BarPeriod.D1, "qfq", start=start, end=end
            )
            assert columns.dates == tuple(item[0].date() for item in points)
            assert columns.closes.tolist() == [float(item[1]) for item in points]

    def test_sync_path_unsorted_and_null_close(self, tmp_path: Path) -> None:
        """乱序文件稳定排序后与 close_points 同序;null close 行跳过。"""
        code = "000001.SZ"
        symbol = Symbol(code=code, market=Market.A_SHARE)
        path = tmp_path / "unsorted.parquet"
        values = [
            datetime(2024, 1, 4, tzinfo=UTC),
            datetime(2024, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 3, tzinfo=UTC),
            datetime(2024, 1, 5, tzinfo=UTC),
        ]
        _write_parquet(path, values, [4.0, 2.0, float("nan"), None])
        columns = ParquetCache.read_close_columns_sync(
            path, symbol, BarPeriod.D1, None, None
        )
        points = ParquetCache.read_close_points_sync(
            path, symbol, BarPeriod.D1, None, None
        )
        # close_points 跳过 null close;列式路径同语义(同日稳定排序)。
        assert columns.dates == tuple(item[0].date() for item in points)
        expected = [float(item[1]) for item in points]
        actual = columns.closes.tolist()
        assert len(actual) == len(expected)
        assert all(
            (value != value and other != other) or value == other
            for value, other in zip(actual, expected, strict=True)
        )
        assert columns.dates == (
            date(2024, 1, 2),
            date(2024, 1, 3),
            date(2024, 1, 4),
        )

    def test_non_d1_rejected(self, tmp_path: Path) -> None:
        symbol = Symbol(code="600519.SH", market=Market.A_SHARE)
        with pytest.raises(ValueError, match="日线"):
            ParquetCache.read_close_columns_sync(
                tmp_path / "na.parquet", symbol, BarPeriod.M5, None, None
            )

    async def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        cache = ParquetCache(tmp_path / "cache")
        columns = await cache.read_close_columns(
            Symbol(code="600519.SH", market=Market.A_SHARE),
            BarPeriod.D1,
            "qfq",
        )
        assert isinstance(columns, CloseColumns)
        assert columns.dates == ()
        assert columns.closes.size == 0

    async def test_read_cache_hit_and_io_stats_entry(self, tmp_path: Path) -> None:
        """第二次读取命中进程内缓存;job 级聚合按 ``read_close_columns`` 记账。"""
        cache = ParquetCache(tmp_path / "cache")
        code = "600036.SH"
        days = [date(2024, 2, 1) + timedelta(days=index) for index in range(6)]
        await cache.write(
            Symbol(code=code, market=Market.A_SHARE), BarPeriod.D1, "qfq",
            _sample_bars(code, days),
        )
        symbol = Symbol(code=code, market=Market.A_SHARE)
        with collect_parquet_read_stats() as stats:
            first = await cache.read_close_columns(symbol, BarPeriod.D1, "qfq")
            second = await cache.read_close_columns(symbol, BarPeriod.D1, "qfq")
        assert stats.read_ops == 2
        assert stats.ops_by_entry.get("read_close_columns") == 2
        assert cache.read_cache_info()["hits"] >= 1
        assert first.dates == second.dates
        assert first.closes.tolist() == second.closes.tolist()
