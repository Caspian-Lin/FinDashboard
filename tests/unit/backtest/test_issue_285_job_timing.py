"""回测 job 级分段耗时打点单测(issue #285)。

断言:
* ``BacktestEngine.run()`` 的结果携带 ``timing``(总耗时 / 数据加载耗时 /
  parquet 读取聚合),含无数据早退路径;
* ``ParquetCache`` 的 job 级聚合计时在读取入口包裹/计数,不改动读取内部
  逻辑;contextvar 未激活时零行为变化;嵌套激活以内层为准。

纯可观测性:timing 不参与任何 checksum,不改变回测语义。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from finboard_backtest import BacktestConfig, BacktestEngine
from finboard_core import Strategy
from finboard_data.cache import (
    ParquetCache,
    ParquetReadJobStats,
    collect_parquet_read_stats,
)
from finboard_shared.identifiers import StrategyId
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

SYMBOL = Symbol(code="510300.SH", market=Market.A_SHARE)


def _bar(day: int, close: str = "10") -> Bar:
    return Bar(
        symbol=SYMBOL,
        period=BarPeriod.D1,
        timestamp=datetime(2024, 1, day, tzinfo=UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("1000"),
    )


class NopStrategy(Strategy):
    """最小策略桩:只满足引擎回调契约,不做任何交易。"""

    @property
    def strategy_id(self) -> StrategyId:
        return StrategyId("nop")

    async def on_market_data(self, event: object, ctx: object) -> None:
        del event, ctx


class MemoryProvider:
    """每个标的返回 3 根固定日线的内存行情源。"""

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        del period, start, end, adjust
        return [_bar(day) for day in (1, 2, 3)]


class EmptyProvider:
    """恒返回空列表,触发引擎 no_data 早退。"""

    async def fetch_bars(
        self,
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[Bar]:
        del symbol, period, start, end, adjust
        return []


def _assert_timing_shape(timing: dict[str, object]) -> None:
    assert set(timing) == {
        "total_elapsed_seconds",
        "data_load_elapsed_seconds",
        "parquet_reads",
    }
    total = cast(float, timing["total_elapsed_seconds"])
    load = cast(float, timing["data_load_elapsed_seconds"])
    assert total >= 0
    assert load >= 0
    assert total >= load
    reads = cast(dict[str, object], timing["parquet_reads"])
    assert set(reads) == {"read_ops", "read_elapsed_ms", "read_bytes", "ops_by_entry"}


class TestEngineTiming:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_result_carries_timing(self) -> None:
        result = await BacktestEngine(
            strategy=NopStrategy(),
            data_provider=MemoryProvider(),
            config=BacktestConfig(
                symbols=["510300.SH"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 3),
            ),
        ).run()

        assert result.timing is not None
        _assert_timing_shape(result.timing)
        # 内存 provider 不经 ParquetCache,读取计数为 0。
        reads = cast(dict[str, object], result.timing["parquet_reads"])
        assert reads["read_ops"] == 0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_data_early_return_still_records_timing(self) -> None:
        result = await BacktestEngine(
            strategy=NopStrategy(),
            data_provider=EmptyProvider(),
            config=BacktestConfig(
                symbols=["510300.SH"],
                start=date(2024, 1, 1),
                end=date(2024, 1, 3),
            ),
        ).run()

        assert result.total_return == 0.0
        assert result.timing is not None
        _assert_timing_shape(result.timing)


class TestParquetJobStats:
    @pytest.mark.unit
    def test_stats_record_and_as_dict(self) -> None:
        stats = ParquetReadJobStats()
        stats.record("read", elapsed_ms=1.0, size_bytes=100)
        stats.record("read", elapsed_ms=2.5, size_bytes=200)
        stats.record("metadata", elapsed_ms=0.5, size_bytes=64)

        payload = stats.as_dict()
        assert payload["read_ops"] == 3
        assert payload["read_elapsed_ms"] == pytest.approx(4.0)
        assert payload["read_bytes"] == 364
        assert payload["ops_by_entry"] == {"metadata": 1, "read": 2}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_collect_wraps_cache_read_entries(self, tmp_path: Path) -> None:
        cache = ParquetCache(tmp_path)
        await cache.write(SYMBOL, BarPeriod.D1, "qfq", [_bar(2), _bar(3)])

        with collect_parquet_read_stats() as stats:
            assert len(await cache.read(SYMBOL, BarPeriod.D1, "qfq")) == 2
            points = await cache.read_close_points(
                SYMBOL,
                BarPeriod.D1,
                "qfq",
                start=date(2024, 1, 2),
                end=date(2024, 1, 3),
            )
            assert len(points) == 2
            metadata = await cache.metadata_for(SYMBOL, BarPeriod.D1, "qfq")
            assert metadata is not None

        payload = stats.as_dict()
        assert payload["read_ops"] == 3
        assert cast(int, payload["read_bytes"]) > 0
        assert cast(float, payload["read_elapsed_ms"]) >= 0
        assert payload["ops_by_entry"] == {
            "metadata": 1,
            "read": 1,
            "read_close_points": 1,
        }

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_inactive_collection_is_no_op(self, tmp_path: Path) -> None:
        cache = ParquetCache(tmp_path)
        # 未激活聚合:读取照常工作,不抛错、不计数(句柄不存在即跳过)。
        assert await cache.read(SYMBOL, BarPeriod.D1, "qfq") == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_nested_collection_accumulates_all_handles(
        self, tmp_path: Path
    ) -> None:
        cache = ParquetCache(tmp_path)
        await cache.write(SYMBOL, BarPeriod.D1, "qfq", [_bar(2)])

        with collect_parquet_read_stats() as outer:
            assert len(await cache.read(SYMBOL, BarPeriod.D1, "qfq")) == 1
            with collect_parquet_read_stats() as inner:
                assert len(await cache.read(SYMBOL, BarPeriod.D1, "qfq")) == 1
            # issue #383:读取向全部活跃句柄累加 —— 内层只记自身段,
            # 外层记全程(不再「以内层为准」遮蔽外层聚合)。
            assert inner.as_dict()["read_ops"] == 1
        assert outer.as_dict()["read_ops"] == 2
