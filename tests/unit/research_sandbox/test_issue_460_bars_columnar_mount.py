"""issue #460:窗口/单日挂载的 bars 列式直通 —— 与对象路径逐值等值。

``_iter_bars_batches`` 经 ``fetch_bars_columns`` hasattr 探针列式优先,
批次形态(Arrow 表 | dict 列表)在两个消费方
(:func:`build_data_mount` / :func:`build_window_data_mount`)统一经
``_bars_batch_table`` 归一。本文件锁定:

* 真实 FrozenReleaseProvider(列式)与遮蔽列式方法的对象路径 shim,
  窗口/单日挂载的 bars.parquet 逐值一致、清单 row_count/universe 一致;
* 列式 stub 批次(带多余列)被 schema select 正确裁剪,与对象 stub
  (point 形态)挂载逐值一致;
* 单日挂载(v2 策略形态)无 available_at 列的既有契约保持。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finboard_backtest.research_sandbox.data_mount import (
    build_data_mount,
    build_window_data_mount,
)
from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, Market

_RELEASE_ID = "bars-columnar-mount-r1"
_CODES = ("600001.SH", "000002.SZ")
_START = date(2024, 1, 2)
_END = date(2024, 1, 31)
_WINDOW_DAYS = (date(2024, 1, 10), date(2024, 1, 15), date(2024, 1, 31))


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
        dataset_name="bars_columnar_mount",
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


class _ObjectPathOnlyProvider:
    """遮蔽 ``fetch_bars_columns`` 的 shim:强制走既有对象路径。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def release(self) -> Any:
        return self._inner.release

    async def fetch_point_in_time_bars(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.fetch_point_in_time_bars(*args, **kwargs)


def _read_mount_bars(out_root: Path) -> tuple[dict[str, list[dict[str, object]]], int]:
    table = pq.read_table(out_root / "bars.parquet")
    rows = table.to_pylist()
    by_symbol: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_symbol.setdefault(str(row["symbol"]), []).append(row)
    return by_symbol, table.num_rows


@pytest.mark.asyncio
async def test_window_mount_columnar_matches_object_path(tmp_path: Path) -> None:
    """窗口挂载:列式与对象路径的 bars.parquet 逐值一致。"""
    provider = await _publish_bars_release(tmp_path / "col")
    object_provider = _ObjectPathOnlyProvider(provider)
    dates = tuple(_WINDOW_DAYS)

    col_mount = await build_window_data_mount(
        providers=[provider],
        window_start=min(dates),
        window_end=max(dates),
        dates=dates,
        out_root=tmp_path / "col-mount",
        code_artifact="mom20",
        code_commit="c" * 40,
        release_id=_RELEASE_ID,
        dataset_release_ids=[],
    )
    obj_mount = await build_window_data_mount(
        providers=[object_provider],
        window_start=min(dates),
        window_end=max(dates),
        dates=dates,
        out_root=tmp_path / "obj-mount",
        code_artifact="mom20",
        code_commit="c" * 40,
        release_id=_RELEASE_ID,
        dataset_release_ids=[],
    )
    col_rows, col_total = _read_mount_bars(col_mount.root)
    obj_rows, obj_total = _read_mount_bars(obj_mount.root)
    assert col_total == obj_total > 0
    assert sorted(col_rows) == sorted(obj_rows) == sorted(_CODES)
    for code in _CODES:
        assert col_rows[code] == obj_rows[code]
    assert [item.row_count for item in col_mount.datasets] == [
        item.row_count for item in obj_mount.datasets
    ]
    assert col_mount.symbols == obj_mount.symbols


@pytest.mark.asyncio
async def test_single_day_mount_columnar_matches_object_path(tmp_path: Path) -> None:
    """单日挂载(v2 策略形态):列式与对象路径逐值一致且无 available_at 列。"""
    provider = await _publish_bars_release(tmp_path / "col")
    object_provider = _ObjectPathOnlyProvider(provider)
    decision_at = datetime.combine(_WINDOW_DAYS[0], time(23, 59), tzinfo=UTC)

    col_mount = await build_data_mount(
        providers=[provider],
        decision_at=decision_at,
        out_root=tmp_path / "col-mount",
    )
    obj_mount = await build_data_mount(
        providers=[object_provider],
        decision_at=decision_at,
        out_root=tmp_path / "obj-mount",
    )
    col_rows, col_total = _read_mount_bars(col_mount.root)
    obj_rows, obj_total = _read_mount_bars(obj_mount.root)
    assert col_total == obj_total > 0
    for code in _CODES:
        assert col_rows[code] == obj_rows[code]
    schema = pq.read_schema(col_mount.root / "bars.parquet")
    assert "available_at" not in schema.names


# ------------------------------------------------------------- stub 形态


class _ColumnarStubProvider:
    """只带 fetch_bars_columns 的最小 stub:锁定 schema select 裁剪多余列。"""

    def __init__(self, release: Any, table_factory: Any) -> None:
        self.release = release
        self._table_factory = table_factory

    async def fetch_bars_columns(
        self, symbol: Any, period: Any, start: date, end: date, *,
        decision_at: datetime, adjust: str = "qfq",
    ) -> Any:
        return self._table_factory(symbol.code, start, end)


class _ObjectStubProvider:
    """只带 fetch_point_in_time_bars 的最小 stub(point 形态,既有路径)。"""

    def __init__(self, release: Any, rows_factory: Any) -> None:
        self.release = release
        self._rows_factory = rows_factory

    async def fetch_point_in_time_bars(
        self, symbol: Any, period: Any, start: date, end: date, *,
        decision_at: datetime, adjust: str = "qfq",
    ) -> list[Any]:
        rows: list[Any] = self._rows_factory(symbol.code, start, end)
        return rows


class _Release:
    def __init__(self) -> None:
        self.release_id = "stub"
        self.dataset_kind = SimpleNamespace(value="bars")
        self.period = BarPeriod.D1
        self.adjustment = "qfq"
        self.start_date = _START
        self.end_date = _END
        self.instruments = [
            SimpleNamespace(code=code, market=Market.A_SHARE) for code in _CODES
        ]


def _stub_payload(code: str, start: date, end: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    day = start
    while day <= end:
        rows.append(
            {
                "symbol": code,
                "date": day,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
                "amount": 1.0,
                "available_at": datetime.combine(day, time(7, 30), tzinfo=UTC),
            }
        )
        day += timedelta(days=1)
    return rows


def _point_rows(code: str, start: date, end: date) -> list[Any]:
    """对象路径的 point 形态:``_collect_bars`` 消费 .bar / .available_at。"""
    return [
        SimpleNamespace(
            bar=SimpleNamespace(
                symbol=SimpleNamespace(code=row["symbol"]),
                timestamp=datetime.combine(row["date"], time(0, 0), tzinfo=UTC),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                amount=row["amount"],
            ),
            available_at=row["available_at"],
        )
        for row in _stub_payload(code, start, end)
    ]


@pytest.mark.asyncio
async def test_columnar_stub_table_selects_schema_columns(tmp_path: Path) -> None:
    """列式 stub 多带一列,归一时被 schema select 裁掉,挂载与对象 stub 一致。"""

    def table_factory(code: str, start: date, end: date) -> pa.Table:
        rows = _stub_payload(code, start, end)
        for row in rows:
            row["extra_column"] = "ignore-me"
        return pa.Table.from_pylist(rows)

    release = _Release()
    col_mount = await build_window_data_mount(
        providers=[_ColumnarStubProvider(release, table_factory)],
        window_start=_START,
        window_end=_END,
        dates=(date(2024, 1, 5), date(2024, 1, 31)),
        out_root=tmp_path / "col-mount",
        code_artifact="mom20",
        code_commit="c" * 40,
        release_id="stub",
        dataset_release_ids=[],
    )
    obj_mount = await build_window_data_mount(
        providers=[_ObjectStubProvider(release, _point_rows)],
        window_start=_START,
        window_end=_END,
        dates=(date(2024, 1, 5), date(2024, 1, 31)),
        out_root=tmp_path / "obj-mount",
        code_artifact="mom20",
        code_commit="c" * 40,
        release_id="stub",
        dataset_release_ids=[],
    )
    col_rows, col_total = _read_mount_bars(col_mount.root)
    obj_rows, obj_total = _read_mount_bars(obj_mount.root)
    assert col_total == obj_total > 0
    for code in _CODES:
        assert col_rows[code] == obj_rows[code]
    schema = pq.read_schema(col_mount.root / "bars.parquet")
    assert "extra_column" not in schema.names
