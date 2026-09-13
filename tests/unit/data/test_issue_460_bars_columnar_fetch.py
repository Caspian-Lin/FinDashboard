"""issue #460:``FrozenReleaseProvider.fetch_bars_columns`` 列式直通与对象路径逐值等值。

挂载 bars 采集此前走 ``fetch_point_in_time_bars`` 全对象路径:全市场窗口
~1000 万行 x 每行 5-6 个 Python 对象(Bar/PointInTimeBar/dict/available_at)
把挂载段钉在 GIL 单核上(~28 分钟,#371 daily 同病灶)。列式直通在 Arrow
内完成 available_at 派生(市场收盘规则)与 PIT 门控/日期过滤。本文件锁定:

* 列式 == 对象路径:date/OHLCV/amount/available_at 逐值相等;
* PIT 门控与日期区间过滤语义一致;
* available_at = 业务日 15:30 上海(07:30 UTC)收盘规则;
* naive ``decision_at`` 具名拒绝。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, Market

_RELEASE_ID = "bars-columnar-r1"
_CODES = ("600001.SH", "000002.SZ")
_START = date(2024, 1, 2)
_END = date(2024, 2, 13)


def _trade_days() -> list[date]:
    result: list[date] = []
    current = _START
    while current <= _END:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _closes(code_index: int, count: int) -> list[Decimal]:
    return [
        Decimal("10") + Decimal((code_index * 7 + j * 13) % 31) * Decimal("0.1")
        for j in range(count)
    ]


def _stock(code: str):
    from finboard_data.releases import (
        ReleaseInstrumentSpec,
        default_execution_metadata,
    )

    return ReleaseInstrumentSpec(
        code=code,
        name=f"样本{code}",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2023, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        list_date=date(2020, 1, 1),
    )


async def _publish_bars_release(tmp_path: Path) -> FrozenReleaseProvider:
    """真实发布构建器冻结一份多标的 bars 发布(夹具同款)。"""
    from finboard_data.cache import ParquetCache

    days = _trade_days()
    cache = ParquetCache(tmp_path / "cache")
    for i, code in enumerate(_CODES):
        symbol = Symbol(code, Market.A_SHARE)
        closes = _closes(i, len(days))
        bars = [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(day, time(0, 0), tzinfo=UTC),
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("10000"),
                amount=Decimal("100000"),
                source="fixed_sample",
            )
            for day, close in zip(days, closes, strict=True)
        ]
        await cache.write(symbol, BarPeriod.D1, "qfq", bars)
    release_root = tmp_path / "releases"
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=release_root,
    )
    spec = DatasetReleaseSpec(
        release_id=_RELEASE_ID,
        dataset_name="bars_columnar_equivalence",
        source="fixed_sample",
        version="v1",
        start_date=_START,
        end_date=_END,
        code_version="test",
        required_capabilities=("stock",),
    )
    release = await builder.publish(spec, [_stock(code) for code in _CODES])
    assert release.is_usable
    return FrozenReleaseProvider(release_root=release_root, release_id=_RELEASE_ID)


def _symbol(provider: FrozenReleaseProvider, code: str) -> Symbol:
    return Symbol(code, Market.A_SHARE)


@pytest.mark.asyncio
async def test_columnar_matches_object_path_full_range(tmp_path: Path) -> None:
    """全区间 + 宽松 decision_at:列式与对象路径逐行逐值相等。"""
    provider = await _publish_bars_release(tmp_path)
    days = _trade_days()
    decision_at = datetime.combine(days[-1] + timedelta(days=1), time(0, 0), tzinfo=UTC)
    for code in _CODES:
        points = await provider.fetch_point_in_time_bars(
            _symbol(provider, code),
            BarPeriod.D1,
            _START,
            _END,
            decision_at=decision_at,
        )
        table = await provider.fetch_bars_columns(
            _symbol(provider, code),
            BarPeriod.D1,
            _START,
            _END,
            decision_at=decision_at,
        )
        assert table.num_rows == len(points) == len(days)
        rows = table.to_pylist()
        for row, point in zip(rows, points, strict=True):
            assert row["symbol"] == code
            assert row["date"] == point.bar.timestamp.date()
            assert row["open"] == float(point.bar.open)
            assert row["high"] == float(point.bar.high)
            assert row["low"] == float(point.bar.low)
            assert row["close"] == float(point.bar.close)
            assert row["volume"] == float(point.bar.volume)
            assert row["amount"] == float(point.bar.amount)
            assert row["available_at"] == point.available_at


@pytest.mark.asyncio
async def test_columnar_pit_gate_matches_object_path(tmp_path: Path) -> None:
    """decision_at 收紧到窗口中部:可见行集合与对象路径一致。"""
    provider = await _publish_bars_release(tmp_path)
    days = _trade_days()
    cut = days[len(days) // 2]
    decision_at = datetime.combine(cut, time(15, 30), tzinfo=UTC)
    code = _CODES[0]
    points = await provider.fetch_point_in_time_bars(
        _symbol(provider, code),
        BarPeriod.D1,
        _START,
        _END,
        decision_at=decision_at,
    )
    table = await provider.fetch_bars_columns(
        _symbol(provider, code),
        BarPeriod.D1,
        _START,
        _END,
        decision_at=decision_at,
    )
    assert [point.bar.timestamp.date() for point in points] == [
        row["date"] for row in table.to_pylist()
    ]
    assert all(row["date"] <= cut for row in table.to_pylist())


@pytest.mark.asyncio
async def test_columnar_date_range_filter_matches_object_path(tmp_path: Path) -> None:
    """区间起点收紧:列式与对象路径保留同一日期子集。"""
    provider = await _publish_bars_release(tmp_path)
    days = _trade_days()
    start = days[len(days) // 3]
    decision_at = datetime.combine(days[-1] + timedelta(days=1), time(0, 0), tzinfo=UTC)
    code = _CODES[1]
    points = await provider.fetch_point_in_time_bars(
        _symbol(provider, code),
        BarPeriod.D1,
        start,
        _END,
        decision_at=decision_at,
    )
    table = await provider.fetch_bars_columns(
        _symbol(provider, code),
        BarPeriod.D1,
        start,
        _END,
        decision_at=decision_at,
    )
    assert [point.bar.timestamp.date() for point in points] == [
        row["date"] for row in table.to_pylist()
    ]


@pytest.mark.asyncio
async def test_columnar_available_at_is_shanghai_close(tmp_path: Path) -> None:
    """available_at = 业务日 15:30 上海(= 07:30 UTC),与对象路径同规则。"""
    from datetime import timezone

    provider = await _publish_bars_release(tmp_path)
    days = _trade_days()
    decision_at = datetime.combine(days[-1] + timedelta(days=1), time(0, 0), tzinfo=UTC)
    table = await provider.fetch_bars_columns(
        _symbol(provider, _CODES[0]),
        BarPeriod.D1,
        _START,
        _END,
        decision_at=decision_at,
    )
    shanghai = timezone(timedelta(hours=8))
    for row in table.to_pylist():
        expected = datetime.combine(
            row["date"], time(15, 30), tzinfo=shanghai
        ).astimezone(UTC)
        assert row["available_at"] == expected


@pytest.mark.asyncio
async def test_columnar_naive_decision_at_rejected(tmp_path: Path) -> None:
    """naive decision_at 与对象路径同口径具名拒绝。"""
    provider = await _publish_bars_release(tmp_path)
    with pytest.raises(ValueError, match="decision_at 必须带时区"):
        await provider.fetch_bars_columns(
            _symbol(provider, _CODES[0]),
            BarPeriod.D1,
            _START,
            _END,
            decision_at=datetime(2024, 2, 13, 0, 0),
        )
