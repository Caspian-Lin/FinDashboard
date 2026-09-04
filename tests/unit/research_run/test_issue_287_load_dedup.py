"""research_run 加载路径去重的等值锁定测试(issue #287)。

锁定四组不变量:
* close 矩阵逐期切片 == 逐期 PIT 过滤读取(逐值,真实 FrozenReleaseProvider);
* 交易日历按 provider 进程内缓存(底层 fetch_bars 只发生一次);
* release provider 工厂按 run(闭包)memoize(同一 release_id 同一实例);
* 逐 symbol 串行改 ``asyncio.gather`` 后与串行实现逐值一致(含顺序)。

真实发布用 :class:`FrozenDatasetReleaseBuilder` 在临时目录构建,不依赖
PostgreSQL;`data_cache` / `data_releases` 只读约定不受影响(测试全走 tmp)。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.frozen_loader import (
    FrozenInputLoader,
    SymbolCloseHistory,
    _load_close_prices,
)
from finboard_backtest.research_run.signal_engine import (
    _load_price_series,
    _market_close_map,
    _release_trading_days,
    build_signal_engine_adapter_factory,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_data.cache import ParquetCache
from finboard_data.releases import (
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseInstrumentSpec,
    default_execution_metadata,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, Market

_CST = ZoneInfo("Asia/Shanghai")
_RELEASE_ID = "load-dedup-r1"
_SYMBOL_CODES = ("600519.SH", "000001.SZ", "600036.SH")
_SESSION_COUNT = 12


def _sessions(count: int) -> list[date]:
    """``count`` 个连续工作日(跳过周末),模拟发布交易日历。"""
    days: list[date] = []
    cursor = date(2024, 1, 2)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


SESSIONS = _sessions(_SESSION_COUNT)
_START = SESSIONS[0]
_END = SESSIONS[-1]


def _close(code: str, day: date) -> Decimal:
    """确定性价格:每标的基准价 x 逐日温和漂移。"""
    base = {"600519.SH": "1500.00", "000001.SZ": "10.00", "600036.SH": "30.00"}[code]
    return Decimal(base) * (Decimal("1") + Decimal(SESSIONS.index(day)) / Decimal("100"))


def _bars(code: str) -> list[Bar]:
    symbol = Symbol(code=code, market=Market.A_SHARE)
    return [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            open=_close(code, day) * Decimal("1.01"),
            high=_close(code, day) * Decimal("1.02"),
            low=_close(code, day) * Decimal("0.98"),
            close=_close(code, day),
            volume=Decimal(10000),
            amount=Decimal("100000"),
            source="fixed_sample",
        )
        for day in SESSIONS
    ]


def _instrument(code: str) -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=code,
        name=f"样本{code}",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2001, 8, 27, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        list_date=date(2001, 8, 27),
    )


async def _build_release(tmp_path: Path) -> tuple[FrozenReleaseProvider, Path]:
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instruments = [_instrument(code) for code in _SYMBOL_CODES]
    cache = ParquetCache(cache_dir)
    for instrument in instruments:
        await cache.write(
            Symbol(code=instrument.code, market=instrument.market),
            BarPeriod.D1,
            "qfq",
            _bars(instrument.code),
        )
    await FrozenDatasetReleaseBuilder(
        cache_dir=cache_dir,
        release_root=release_root,
    ).publish(
        DatasetReleaseSpec(
            release_id=_RELEASE_ID,
            dataset_name="load_dedup_daily_bars",
            source="fixed_sample",
            version="2024.01",
            start_date=_START,
            end_date=_END,
            code_version="deadbeef",
            required_capabilities=("stock",),
        ),
        instruments,
    )
    provider = FrozenReleaseProvider(release_root=release_root, release_id=_RELEASE_ID)
    return provider, release_root


def _decision_days() -> list[tuple[datetime, datetime]]:
    """(decision_at=当日 15:00 CST, execution_at=次日 16:00 CST)序列。

    真实 provider 的日线 available_at = 当日 15:30 CST:15:00 决策看不到当日
    bar(前缀边界),16:00 成交可以看到 —— 精确落在 PIT 边界两侧。
    """
    pairs: list[tuple[datetime, datetime]] = []
    for index, day in enumerate(SESSIONS[:-1]):
        decision_at = datetime.combine(day, time(15, 0), tzinfo=_CST)
        execution_at = datetime.combine(SESSIONS[index + 1], time(16, 0), tzinfo=_CST)
        pairs.append((decision_at, execution_at))
    return pairs


async def _reference_close(
    provider: FrozenReleaseProvider, code: str, as_of: datetime
) -> float | None:
    """与优化前 `_load_close_prices` 逐期读取等价的参照实现。

    直接调类方法(绕过实例计数包装),保证计数器只统计被测加载路径。
    """
    bars = await FrozenReleaseProvider.fetch_point_in_time_bars(
        provider,
        Symbol(code=code, market=Market.A_SHARE),
        provider.release.period,
        provider.release.start_date,
        as_of.date(),
        decision_at=as_of,
        adjust=provider.release.adjustment,
    )
    return float(bars[-1].bar.close) if bars else None


async def _reference_series(
    provider: FrozenReleaseProvider, codes: Sequence[str], as_of: datetime
) -> dict[str, list[float]]:
    """与优化前 `_load_price_series` 串行实现等价的参照(绕过计数包装)。"""
    series: dict[str, list[float]] = {}
    for code in codes:
        bars = await FrozenReleaseProvider.fetch_point_in_time_bars(
            provider,
            Symbol(code=code, market=Market.A_SHARE),
            provider.release.period,
            provider.release.start_date,
            as_of.date(),
            decision_at=as_of,
            adjust=provider.release.adjustment,
        )
        if bars:
            series[code] = [float(bar.bar.close) for bar in bars]
    return series


def _provider_stub(provider: FrozenReleaseProvider, counter: dict[str, int]) -> None:
    """给 provider 的读取入口挂调用计数。

    issue #300:close 矩阵构建读取入口为列式 ``fetch_close_history``
    (原 ``fetch_point_in_time_bars``),计数键名保持 ``pit`` 不变。
    """

    async def counting_pit(
        symbol: Symbol,
        period: BarPeriod,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> object:
        counter["pit"] += 1
        return await FrozenReleaseProvider.fetch_close_history(
            provider,
            symbol,
            period,
            start,
            end,
            decision_at=decision_at,
            adjust=adjust,
        )

    async def counting_bars(
        symbol: Symbol, period: BarPeriod, start: date, end: date, *, adjust: str = "qfq"
    ) -> object:
        counter["bars"] += 1
        return await FrozenReleaseProvider.fetch_bars(
            provider,
            symbol,
            period,
            start,
            end,
            adjust=adjust,
        )

    provider.fetch_close_history = counting_pit  # type: ignore[assignment]
    provider.fetch_bars = counting_bars  # type: ignore[assignment]


@pytest.mark.asyncio
class TestCloseMatrixEqualsPerPeriodPitReads:
    """AC:close 矩阵切片与逐期 PIT 读取逐值相等(真实 provider)。"""

    async def test_prices_and_execution_prices_equal_per_period(self, tmp_path: Path) -> None:
        provider, _ = await _build_release(tmp_path)

        def factory(release_id: str) -> FrozenReleaseProvider:
            assert release_id == _RELEASE_ID
            return provider

        async def snapshot_provider(snapshot_id: str) -> None:
            del snapshot_id

        loader = FrozenInputLoader(
            release_provider_factory=factory,
            snapshot_provider=snapshot_provider,
        )
        spec = build_strategy_template(
            "ma_cross",
            strategy_id="load_dedup_test",
            dataset_release_ids=(_RELEASE_ID,),
        )
        manifest = ResearchRunManifest(
            run_id="RR-loaddeduptest0001",
            idempotency_key="load-dedup-test-0001",
            strategy_spec=spec,
            strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
            dataset_releases=(
                FrozenArtifactRef(
                    artifact_id=_RELEASE_ID,
                    version="v1",
                    checksum="a" * 64,
                    capabilities=("stock",),
                ),
            ),
            factor_snapshots=(),
            code_version="abcdef0123456789",
            initial_capital=Decimal("100000"),
            requested_by="unit-test",
        )

        counter = {"pit": 0, "bars": 0}
        _provider_stub(provider, counter)

        for decision_at, execution_at in _decision_days():
            context = await loader.load_context(
                manifest, decision_at=decision_at, execution_at=execution_at
            )
            for code in _SYMBOL_CODES:
                assert context.prices.get(code) == (
                    await _reference_close(provider, code, decision_at)
                )
                assert context.execution_prices.get(code) == (
                    await _reference_close(provider, code, execution_at)
                )

        # 矩阵构建后,后续所有期的加载不再触发逐标的 PIT 读取:
        # 只有构建期的每标的 1 次(3 只)+ 日历/权益曲线的 fetch_bars 计数独立。
        assert counter["pit"] == len(_SYMBOL_CODES)
        # 矩阵对全部候选可切片(真实 provider、单调数据)。
        assert set(loader.close_histories) == set(_SYMBOL_CODES)
        assert all(isinstance(item, SymbolCloseHistory) for item in loader.close_histories.values())

    async def test_price_series_matrix_equals_serial_reference(self, tmp_path: Path) -> None:
        provider, _ = await _build_release(tmp_path)
        loader = FrozenInputLoader(
            release_provider_factory=lambda rid: provider,
            snapshot_provider=_noop_snapshot_provider,
        )
        # 触发矩阵构建。
        await loader._load_close_prices(
            provider,
            tuple(_candidate(code) for code in _SYMBOL_CODES),
            datetime.combine(SESSIONS[5], time(15, 0), tzinfo=_CST),
        )
        for decision_at, _ in _decision_days():
            matrix = await _load_price_series(
                provider,
                _SYMBOL_CODES,
                decision_at,
                close_histories=loader.close_histories,
            )
            reference = await _reference_series(provider, _SYMBOL_CODES, decision_at)
            assert matrix == reference
            assert list(matrix) == list(reference)


def _candidate(code: str) -> Any:
    """构造 loader 内部使用的最小候选对象(load_context 之外的直接调用)。"""
    from finboard_backtest.research_run.contracts import UniverseCandidate

    return UniverseCandidate(
        symbol=code,
        included=True,
        reasons=("test",),
        asset_class="equity",
        market="a_share",
    )


async def _noop_snapshot_provider(snapshot_id: str) -> None:
    del snapshot_id


@pytest.mark.asyncio
class TestTradingCalendarCache:
    async def test_calendar_read_once_per_provider(self, tmp_path: Path) -> None:
        provider, _ = await _build_release(tmp_path)
        counter = {"pit": 0, "bars": 0}
        _provider_stub(provider, counter)

        first = await _release_trading_days(provider)
        second = await _release_trading_days(provider)

        assert first == second == SESSIONS
        # issue #334:日历改为 ≤8 只 ready 标的的采样并集(本发布 3 只全采),
        # 缓存命中后第二次推导零新增读取。
        assert counter["bars"] == len(_SYMBOL_CODES)

    async def test_unhashable_stub_provider_still_works(self) -> None:
        """非真实 provider(非 isinstance)保持逐次推导原行为,不走缓存。"""

        class _StubInstrument:
            code = "600519.SH"
            ready = True

        class _StubRelease:
            release_id = "stub"
            instruments = (_StubInstrument(),)
            period = BarPeriod.D1
            adjustment = "qfq"
            start_date = SESSIONS[0]
            end_date = SESSIONS[-1]

        class _StubProvider:
            release = _StubRelease()
            bars_written = 0

            async def fetch_bars(self, *args: object, **kwargs: object) -> list[Bar]:
                self.bars_written += 1
                return _bars("600519.SH")

        stub = _StubProvider()
        first = await _release_trading_days(stub)  # type: ignore[arg-type]
        second = await _release_trading_days(stub)  # type: ignore[arg-type]
        assert first == second == SESSIONS
        assert stub.bars_written == 2


@pytest.mark.asyncio
class TestProviderFactoryMemoization:
    async def test_same_release_id_returns_same_instance(self, tmp_path: Path) -> None:
        provider, release_root = await _build_release(tmp_path)
        release = provider.release
        del provider
        spec = build_strategy_template(
            "multi_factor",
            strategy_id="load_dedup_factory_test",
            dataset_release_ids=(_RELEASE_ID,),
        )
        manifest = ResearchRunManifest(
            run_id="RR-loaddedupfact0001",
            idempotency_key="load-dedup-factory-0001",
            strategy_spec=spec,
            strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
            dataset_releases=(
                FrozenArtifactRef(
                    artifact_id=_RELEASE_ID,
                    version="v1",
                    checksum=release.release_checksum,
                    capabilities=("stock",),
                ),
            ),
            factor_snapshots=(),
            code_version="abcdef0123456789",
            initial_capital=Decimal("100000"),
            requested_by="unit-test",
        )
        adapter_factory = build_signal_engine_adapter_factory(
            cast(async_sessionmaker[Any], object()),
            release_root=release_root,
        )
        engine = adapter_factory(manifest)
        first = engine._release_provider_factory(_RELEASE_ID)  # type: ignore[attr-defined]
        second = engine._release_provider_factory(_RELEASE_ID)  # type: ignore[attr-defined]
        assert first is second
        assert isinstance(first, FrozenReleaseProvider)
        assert first.release.release_id == _RELEASE_ID


# ---- gather 与串行等值(stub,带交错 sleep)--------------------------------


@dataclass
class _InterleavedStub:
    """每次读取让出事件循环不同次数,强制 gather 任务交错的 stub provider。"""

    closes_by_symbol: dict[str, list[Decimal]]
    release: Any = None
    yield_rounds: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.release is None:
            from finboard_data.releases import ReleaseDatasetKind

            @dataclass
            class _Release:
                release_id: str = "stub-r1"
                instruments: tuple[Any, ...] = ()
                period: BarPeriod = BarPeriod.D1
                adjustment: str = "qfq"
                start_date: date = _START
                end_date: date = _END
                dataset_kind: Any = ReleaseDatasetKind.BARS

            self.release = _Release()
        self.yield_rounds = {code: index + 1 for index, code in enumerate(self.closes_by_symbol)}

    async def fetch_point_in_time_bars(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> list[Any]:
        del period, start, end, decision_at, adjust
        symbol_code = cast(Any, symbol).code
        for _ in range(self.yield_rounds.get(symbol_code, 1)):
            await asyncio.sleep(0)
        closes = self.closes_by_symbol.get(symbol_code, ())
        if not closes:
            return []
        return [
            _Point(bar=_BarStub(close=value, day=SESSIONS[index]), at=SESSIONS[index])
            for index, value in enumerate(closes)
        ]

    async def fetch_bars(self, *args: object, **kwargs: object) -> list[Any]:
        del args, kwargs
        return []


@dataclass(frozen=True, slots=True)
class _BarStub:
    close: Decimal
    day: date


@dataclass(frozen=True, slots=True)
class _Point:
    bar: _BarStub
    at: date


def _point_available_at(point: _Point) -> datetime:
    return datetime.combine(point.at, time(15, 30), tzinfo=_CST)


class TestGatherEqualsSerial:
    async def test_load_close_prices_fallback_matches_serial(self) -> None:
        stub = _InterleavedStub(
            closes_by_symbol={
                code: [_close(code, day) for day in SESSIONS] for code in _SYMBOL_CODES
            }
        )
        as_of = datetime.combine(SESSIONS[4], time(15, 0), tzinfo=_CST)
        candidates = [_candidate(code) for code in _SYMBOL_CODES]

        gathered = await _load_close_prices(stub, candidates, as_of)  # type: ignore[arg-type]

        serial: dict[str, float] = {}
        for candidate in candidates:
            bars = await stub.fetch_point_in_time_bars(
                Symbol(code=candidate.symbol, market=Market.A_SHARE),
                BarPeriod.D1,
                _START,
                as_of.date(),
                decision_at=as_of,
            )
            if bars:
                serial[candidate.symbol] = float(bars[-1].bar.close)
        assert gathered == serial
        assert list(gathered) == list(serial)

    async def test_load_close_prices_raises_first_in_candidate_order(self) -> None:
        """多只标的失败时按候选顺序抛第一个异常(与串行一致)。"""
        from finboard_data.releases import ReleaseCapabilityError as _Err

        class _Exploding(_InterleavedStub):
            async def fetch_point_in_time_bars(
                self,
                symbol: object,
                period: object,
                start: date,
                end: date,
                *,
                decision_at: datetime,
                adjust: str = "qfq",
            ) -> list[Any]:
                code = cast(Any, symbol).code
                await asyncio.sleep(0)
                if code in {"600519.SH", "600036.SH"}:
                    raise _Err(f"missing {code}")
                return await super().fetch_point_in_time_bars(
                    symbol,
                    period,
                    start,
                    end,
                    decision_at=decision_at,
                    adjust=adjust,
                )

        stub = _Exploding(closes_by_symbol={code: [Decimal("10")] * 4 for code in _SYMBOL_CODES})
        candidates = [_candidate(code) for code in _SYMBOL_CODES]
        as_of = datetime.combine(SESSIONS[4], time(15, 0), tzinfo=_CST)
        with pytest.raises(_Err, match=re.escape("missing 600519.SH")):
            await _load_close_prices(stub, candidates, as_of)  # type: ignore[arg-type]

    async def test_market_close_map_matches_serial(self, tmp_path: Path) -> None:
        provider, _ = await _build_release(tmp_path)
        as_of = datetime.combine(SESSIONS[-1], time(16, 0), tzinfo=_CST)
        gathered = await _market_close_map(provider, _SYMBOL_CODES)
        serial: dict[str, dict[date, Decimal]] = {}
        for code in _SYMBOL_CODES:
            bars = await provider.fetch_bars(
                Symbol(code=code, market=Market.A_SHARE),
                provider.release.period,
                provider.release.start_date,
                provider.release.end_date,
                adjust=provider.release.adjustment,
            )
            serial[code] = {bar.timestamp.date(): bar.close for bar in bars}
        assert gathered == serial
        del as_of

    async def test_load_price_series_fallback_matches_serial(self) -> None:
        stub = _InterleavedStub(
            closes_by_symbol={
                code: [_close(code, day) for day in SESSIONS] for code in _SYMBOL_CODES
            }
        )
        as_of = datetime.combine(SESSIONS[6], time(15, 0), tzinfo=_CST)
        gathered = await _load_price_series(stub, _SYMBOL_CODES, as_of)  # type: ignore[arg-type]
        reference = await _reference_series_stub(stub, _SYMBOL_CODES, as_of)
        assert gathered == reference
        assert list(gathered) == list(reference)

    async def test_symbol_close_history_visible_prefix_matches_filter(self) -> None:
        """visible_index 前缀语义与「过滤后取末根」在构造样本上逐值一致。"""
        points = [
            _Point(bar=_BarStub(close=Decimal(index), day=day), at=day)
            for index, day in enumerate(SESSIONS)
        ]
        history = SymbolCloseHistory(
            available_at=tuple(_point_available_at(point) for point in points),
            dates=tuple(point.at for point in points),
            closes=tuple(float(point.bar.close) for point in points),
        )
        for day in SESSIONS:
            for hour in (14, 15, 16):
                as_of = datetime.combine(day, time(hour, 0), tzinfo=_CST)
                visible = [
                    point
                    for point in points
                    if _point_available_at(point) <= as_of and point.at <= as_of.date()
                ]
                assert history.close_at(as_of) == (
                    float(visible[-1].bar.close) if visible else None
                )
                assert history.series_until(as_of) == [float(point.bar.close) for point in visible]


async def _reference_series_stub(
    stub: _InterleavedStub, codes: Sequence[str], as_of: datetime
) -> dict[str, list[float]]:
    series: dict[str, list[float]] = {}
    for code in codes:
        bars = await stub.fetch_point_in_time_bars(
            Symbol(code=code, market=Market.A_SHARE),
            BarPeriod.D1,
            _START,
            as_of.date(),
            decision_at=as_of,
        )
        if bars:
            series[code] = [float(bar.bar.close) for bar in bars]
    return series
