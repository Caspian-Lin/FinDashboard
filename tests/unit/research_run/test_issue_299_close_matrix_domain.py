"""close 矩阵预建按 explicit_symbols 声明域收窄(issue #299)。

#287 引入的 close 矩阵把逐期重复读收敛为一次性预建,但预建范围恒为全发布
instruments——显式标的 run(#254 声明域)被迫为整只发布付一次性全量读取
成本(真实数据实测:150 只声明,发布 5534 只,加载段约 870s)。本组测试
锁定:
* 声明 ``explicit_symbols`` 时预建读取次数 = 域内标的数(经 #285 的
  ``collect_parquet_read_stats`` 断言),矩阵只含声明域;
* 未声明 ``explicit_symbols`` 时行为不变(全发布预建);
* 声明域内逐期机械字段与逐期 PIT 读取逐值相等,且逐期加载零回退读取
  (矩阵全覆盖声明域,不因收窄引爆逐期 fallback)。

真实发布用 :class:`FrozenDatasetReleaseBuilder` 在临时目录构建,不依赖
PostgreSQL(与 #287 测试同一夹具风格)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.frozen_loader import FrozenInputLoader
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_data.cache import ParquetCache, collect_parquet_read_stats
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
_RELEASE_ID = "close-domain-r1"
_ALL_CODES = (
    "600519.SH",
    "000001.SZ",
    "600036.SH",
    "000651.SZ",
    "601318.SH",
    "600276.SH",
)
_DECLARED = ("600519.SH", "600036.SH")
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
    base = {
        "600519.SH": "1500.00",
        "000001.SZ": "10.00",
        "600036.SH": "30.00",
        "000651.SZ": "40.00",
        "601318.SH": "50.00",
        "600276.SH": "60.00",
    }[code]
    return Decimal(base) * (
        Decimal("1") + Decimal(SESSIONS.index(day)) / Decimal("100")
    )


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


async def _build_release(tmp_path: Path) -> FrozenReleaseProvider:
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instruments = [_instrument(code) for code in _ALL_CODES]
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
            dataset_name="close_domain_daily_bars",
            source="fixed_sample",
            version="2024.01",
            start_date=_START,
            end_date=_END,
            code_version="deadbeef",
            required_capabilities=("stock",),
        ),
        instruments,
    )
    return FrozenReleaseProvider(release_root=release_root, release_id=_RELEASE_ID)


def _manifest(explicit: tuple[str, ...] = ()) -> ResearchRunManifest:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id="close_domain_test",
        dataset_release_ids=(_RELEASE_ID,),
    )
    if explicit:
        spec = spec.model_copy(
            update={
                "universe": spec.universe.model_copy(update={"explicit_symbols": explicit})
            }
        )
    return ResearchRunManifest(
        run_id="RR-closedomaintest01",
        idempotency_key="close-domain-test-0001",
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


def _loader(provider: FrozenReleaseProvider) -> FrozenInputLoader:
    def factory(release_id: str) -> FrozenReleaseProvider:
        assert release_id == _RELEASE_ID
        return provider

    async def snapshot_provider(snapshot_id: str) -> None:
        del snapshot_id

    return FrozenInputLoader(
        release_provider_factory=factory,
        snapshot_provider=snapshot_provider,
    )


async def _reference_close(
    provider: FrozenReleaseProvider, code: str, as_of: datetime
) -> float | None:
    """与逐期 PIT 读取等价的参照实现(绕过实例计数包装,#287 同款)。"""
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


async def _reference_execution_open(
    provider: FrozenReleaseProvider, code: str, execution_at: datetime
) -> float | None:
    """执行价参照(issue #336):日终门控下最后可见 bar 的 open。"""
    gate = execution_at.replace(hour=23, minute=59)
    bars = await FrozenReleaseProvider.fetch_point_in_time_bars(
        provider,
        Symbol(code=code, market=Market.A_SHARE),
        provider.release.period,
        provider.release.start_date,
        gate.date(),
        decision_at=gate,
        adjust=provider.release.adjustment,
    )
    return float(bars[-1].bar.open) if bars else None


@pytest.mark.asyncio
class TestCloseMatrixPrebuildDomainNarrowing:
    """AC:预建范围跟随 explicit_symbols 声明域,未声明行为不变。"""

    async def test_declared_prebuilds_domain_only(self, tmp_path: Path) -> None:
        provider = await _build_release(tmp_path)
        loader = _loader(provider)
        manifest = _manifest(_DECLARED)
        with collect_parquet_read_stats() as stats:
            await loader.ensure_close_histories(manifest)
        assert stats.read_ops == len(_DECLARED)
        assert set(loader.close_histories) == set(_DECLARED)
        assert all(item is not None for item in loader.close_histories.values())

    async def test_undeclared_prebuilds_full_release(self, tmp_path: Path) -> None:
        provider = await _build_release(tmp_path)
        loader = _loader(provider)
        manifest = _manifest()
        with collect_parquet_read_stats() as stats:
            await loader.ensure_close_histories(manifest)
        assert stats.read_ops == len(_ALL_CODES)
        assert set(loader.close_histories) == set(_ALL_CODES)

    async def test_lazy_first_build_also_narrowed(self, tmp_path: Path) -> None:
        """不经 ensure_close_histories 的惰性首建同样落在声明域(#299)。"""
        provider = await _build_release(tmp_path)
        loader = _loader(provider)
        manifest = _manifest(_DECLARED)
        decision_at = datetime.combine(SESSIONS[3], time(15, 0), tzinfo=_CST)
        execution_at = datetime.combine(SESSIONS[4], time(16, 0), tzinfo=_CST)
        context = await loader.load_context(
            manifest, decision_at=decision_at, execution_at=execution_at
        )
        assert set(loader.close_histories) == set(_DECLARED)
        assert set(context.prices) == set(_DECLARED)


@pytest.mark.asyncio
class TestDeclaredDomainPeriodLoadsEquivalent:
    """AC:收窄后逐期机械字段与逐期 PIT 读取逐值相等,且零回退读取。"""

    async def test_prices_equal_pit_without_fallback(self, tmp_path: Path) -> None:
        provider = await _build_release(tmp_path)
        loader = _loader(provider)
        manifest = _manifest(_DECLARED)
        await loader.ensure_close_histories(manifest)

        pit_calls = {"pit": 0}

        async def counting_pit(
            symbol: Symbol,
            period: BarPeriod,
            start: date,
            end: date,
            *,
            decision_at: datetime,
            adjust: str = "qfq",
        ) -> object:
            pit_calls["pit"] += 1
            return await FrozenReleaseProvider.fetch_point_in_time_bars(
                provider, symbol, period, start, end,
                decision_at=decision_at, adjust=adjust,
            )

        provider.fetch_point_in_time_bars = counting_pit  # type: ignore[assignment]

        # 发布首日 15:00 决策:尚无任何可见 bar(available_at = 15:30 CST),
        # 价格为空是既有 PIT 边界行为,不因收窄改变。
        boundary = await loader.load_context(
            manifest,
            decision_at=datetime.combine(SESSIONS[0], time(15, 0), tzinfo=_CST),
            execution_at=datetime.combine(SESSIONS[1], time(16, 0), tzinfo=_CST),
        )
        assert boundary.prices == {}
        assert set(boundary.execution_prices) == set(_DECLARED)

        for index in range(1, len(SESSIONS) - 1):
            decision_at = datetime.combine(SESSIONS[index], time(15, 0), tzinfo=_CST)
            execution_at = datetime.combine(
                SESSIONS[index + 1], time(16, 0), tzinfo=_CST
            )
            context = await loader.load_context(
                manifest, decision_at=decision_at, execution_at=execution_at
            )
            # 候选池 / 价格随声明域收窄:声明域之外不再出现在机械字段里。
            assert {c.symbol for c in context.candidates} == set(_DECLARED)
            assert set(context.prices) == set(_DECLARED)
            assert set(context.execution_prices) == set(_DECLARED)
            for code in _DECLARED:
                expected_decision = await _reference_close(provider, code, decision_at)
                # issue #336:执行价 = 执行日 bar 的 open(模板默认 next_open)。
                expected_execution = await _reference_execution_open(
                    provider, code, execution_at
                )
                assert context.prices[code] == expected_decision
                assert context.execution_prices[code] == expected_execution
        # 声明域全部由矩阵覆盖:逐期加载零回退读取(收窄不引爆 fallback)。
        assert pit_calls["pit"] == 0
