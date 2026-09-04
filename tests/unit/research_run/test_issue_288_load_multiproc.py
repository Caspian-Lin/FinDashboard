"""issue #288:research 特征多进程接通与加载期分块并行的等值锁定测试。

锁定五组不变量:
* 价格特征:常驻 spawn 进程池(真实多进程,Windows 无 fork)与进程内
  协程路径的观测逐值相等;池跨期次复用;
* 加载期分块 ``gather`` 与逐期串行的 contexts 逐字段相等(顺序 / 内容);
* #263 逐期失败标记在分块并行下仍精确定位失败期次(多个失败取原始
  期序第一个,与串行首个失败一致);
* 进程池异常不炸 run:启动失败 / 中途 BrokenProcessPool 均具名降级为
  进程内协程路径,结果不变;
* settings ``research_price_feature_process_workers`` 默认值与工厂解析。

#188 进度计数不受影响:进度上报发生在 runner 逐决策持久化阶段,加载期
并行不触碰(test_runner_progress.py 全绿即锁定)。

真实发布用 :class:`FrozenDatasetReleaseBuilder` 在临时目录构建,不依赖
PostgreSQL;spawn 测试真实创建子进程(本机 Windows,无 fork 可走)。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.failure_context import read_decision_load_context
from finboard_backtest.research_run.frozen_loader import (
    FrozenInputLoader,
    LoadedDecisionContext,
)
from finboard_backtest.research_run.signal_engine import (
    _DECISION_LOAD_CHUNK,
    DecisionLoadContext,
    _compute_period_features,
    build_decision_load_contexts,
    build_signal_engine_adapter_factory,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_backtest.strategy_spec.contracts import (
    FeatureGraph,
    FeatureKind,
    FeatureNode,
    FeatureOperator,
    ResearchStrategySpec,
    SignalAction,
    SignalComparator,
    SignalRule,
    SignalRules,
)
from finboard_data.cache import ParquetCache
from finboard_data.releases import (
    CloseHistoryColumns,
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseDatasetKind,
    ReleaseInstrumentSpec,
    default_execution_metadata,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, InstrumentType, Market

# ---- 真实发布构造(多期:2024-01 ~ 2024-06,共 6 个决策期)--------------------

_MONTHS_SESSIONS_COUNT = 4  # 每月 4 个交易日,足够 momentum(20)在后期成立
_SYMBOLS = ("600519.SH", "000001.SZ", "600036.SH")
_RELEASE_ID = "load-multiproc-r1"


def _sessions() -> list[date]:
    """2024-01-02 起每周一~周五的交易日序列,跨 6 个半月(覆盖月末推导)。"""
    days: list[date] = []
    cursor = date(2024, 1, 2)
    while len(days) < 22 * 6:  # ~6.5 个月的工作日
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


SESSIONS = _sessions()
_START = SESSIONS[0]
_END = SESSIONS[-1]


def _close(code: str, day: date) -> Decimal:
    """确定性价格:每标的基准价 x 逐日温和漂移 x 交替抖动(非零波动率)。"""
    base = {"600519.SH": "1500.00", "000001.SZ": "10.00", "600036.SH": "30.00"}[code]
    step = SESSIONS.index(day)
    wiggle = Decimal("1.01") if step % 2 == 0 else Decimal("0.99")
    return Decimal(base) * (Decimal("1") + Decimal(step) / Decimal("200")) * wiggle


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


async def _build_release(
    tmp_path: Path,
) -> tuple[FrozenReleaseProvider, Path]:
    cache_dir = tmp_path / "cache"
    release_root = tmp_path / "releases"
    instruments = [_instrument(code) for code in _SYMBOLS]
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
            dataset_name="load_multiproc_daily_bars",
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


def _price_only_spec(
    release_ids: tuple[str, ...] = (_RELEASE_ID,),
) -> ResearchStrategySpec:
    """价格因子专用规格(仅 momentum/volatility,无需冻结基本面快照)。"""
    spec = build_strategy_template(
        "multi_factor",
        strategy_id="multiproc_test",
        dataset_release_ids=release_ids,
    )
    nodes = (
        FeatureNode(
            node_id="momentum",
            label="动量",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source="momentum",
        ),
        FeatureNode(
            node_id="volatility",
            label="波动率",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source="volatility_20d",
        ),
        FeatureNode(
            node_id="composite",
            label="复合得分",
            kind=FeatureKind.COMPOSITE,
            operator=FeatureOperator.WEIGHTED_SUM,
            inputs=("momentum", "volatility"),
            weights=(0.6, 0.4),
        ),
    )
    return spec.model_copy(
        update={
            "feature_graph": FeatureGraph(nodes=nodes, outputs=("composite",)),
            "signal_rules": SignalRules(
                rules=(
                    SignalRule(
                        rule_id="top_score_buy",
                        feature_id="composite",
                        comparator=SignalComparator.RANK_TOP,
                        threshold=0.5,
                        action=SignalAction.BUY,
                        rationale="复合得分前 50% 纳入目标仓位。",
                    ),
                )
            ),
        }
    )


def _real_manifest(spec: ResearchStrategySpec) -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id="RR-multiproctest0001",
        idempotency_key="multiproc-test-0001",
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
        parameters={"rebalance_frequency": "monthly"},
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


def _month_end_decisions() -> list[date]:
    """monthly 决策日:每月最后一个交易日(其后仍有交易日)。"""
    ends: dict[tuple[int, int], date] = {}
    for day in SESSIONS:
        ends[(day.year, day.month)] = day
    out = []
    for day in sorted(ends.values()):
        if any(item > day for item in SESSIONS):
            out.append(day)
    return out


def _decision_at(day: date) -> datetime:
    return datetime.combine(day, time(15, 0), tzinfo=UTC)


async def _noop_snapshot_provider(snapshot_id: str) -> None:
    del snapshot_id


def _assert_contexts_equal(
    left: tuple[DecisionLoadContext, ...],
    right: tuple[DecisionLoadContext, ...],
) -> None:
    """逐字段比较(协方差含 numpy 矩阵,不能用 dataclass 全等)。"""
    assert len(left) == len(right)
    for a, b in zip(left, right, strict=True):
        assert a.context.business_date == b.context.business_date
        assert a.context.decision_at == b.context.decision_at
        assert a.context.execution_at == b.context.execution_at
        assert a.context.prices == b.context.prices
        assert a.context.execution_prices == b.context.execution_prices
        assert list(a.context.lot_info) == list(b.context.lot_info)
        assert a.context.input_artifact_ids == b.context.input_artifact_ids
        assert a.candidates == b.candidates
        assert a.features == b.features
        assert a.signalable == b.signalable
        assert a.price_series == b.price_series
        assert a.snapshot_id == b.snapshot_id
        if a.covariance is None or b.covariance is None:
            assert a.covariance is None
            assert b.covariance is None
        else:
            assert a.covariance.tickers == b.covariance.tickers
            assert a.covariance.n_observations == b.covariance.n_observations
            assert a.covariance.method == b.covariance.method
            np.testing.assert_allclose(a.covariance.matrix, b.covariance.matrix)


@pytest.mark.timeout(240)
@pytest.mark.asyncio
class TestProcessPoolFeatureEquality:
    """AC:常驻 spawn 进程池与进程内协程路径的特征结果逐值相等(真实多进程)。"""

    async def test_pool_snapshot_equals_in_process_and_reuses_pool(
        self, tmp_path: Path
    ) -> None:
        from finboard_backtest.factor_lab import (
            PriceFeatureProcessPool,
            build_price_feature_snapshot,
        )

        provider, _ = await _build_release(tmp_path)
        decision_first = _decision_at(_month_end_decisions()[3])
        decision_second = _decision_at(_month_end_decisions()[4])
        kwargs: dict[str, Any] = {
            "provider": provider,
            "code_version": "deadbeef",
            "volatility_windows": (20,),
        }

        serial = await build_price_feature_snapshot(
            decision_at=decision_first, **kwargs
        )

        pool = PriceFeatureProcessPool(provider=provider, worker_count=2)
        await pool.start()
        try:
            first = await build_price_feature_snapshot(
                decision_at=decision_first,
                process_workers=pool.worker_count,
                process_executor=pool.executor,
                **kwargs,
            )
            # 池跨期次复用:第二期不再建池 / 预热 / 关池。
            second = await build_price_feature_snapshot(
                decision_at=decision_second,
                process_workers=pool.worker_count,
                process_executor=pool.executor,
                **kwargs,
            )
        finally:
            await pool.aclose()
        # aclose 幂等。
        await pool.aclose()

        # 逐值相等:进程池只是计算位置不同,观测与快照内容完全一致。
        # (snapshot_id 含 published_at 时间戳,每次构建必然不同,不比。)
        assert first.observations == serial.observations
        assert first.dataset_release_id == serial.dataset_release_id
        assert first.decision_at == serial.decision_at
        assert first.calculation_windows == serial.calculation_windows
        # 第二期与其他期的快照不同(不同决策时点),证明复用池仍按期计算。
        assert second.decision_at == decision_second
        assert second.observations != first.observations

    async def test_compute_period_features_pool_equals_in_process(
        self, tmp_path: Path
    ) -> None:
        """``_compute_period_features`` 两条路径产出的 FeatureValue 逐值相等。"""
        from finboard_backtest.factor_lab import PriceFeatureProcessPool

        provider, _ = await _build_release(tmp_path)
        manifest = _real_manifest(_price_only_spec())
        decision_at = _decision_at(_month_end_decisions()[4])

        in_process = await _compute_period_features(
            provider, manifest, decision_at, _RELEASE_ID
        )
        assert in_process, "决策期必须有可计算的价格特征"

        pool = PriceFeatureProcessPool(provider=provider, worker_count=2)
        await pool.start()
        try:
            via_pool = await _compute_period_features(
                provider,
                manifest,
                decision_at,
                _RELEASE_ID,
                process_pool=pool,
            )
        finally:
            await pool.aclose()

        assert via_pool == in_process
        assert {item.feature_id for item in via_pool} >= {"momentum", "volatility_20d"}

    async def test_real_broken_pool_degrades_without_failing(
        self, tmp_path: Path
    ) -> None:
        """杀掉 worker 进程(BrokenProcessPool)→ 本期具名降级,结果不变。

        真实多进程场景(Windows TerminateProcess),不是 mock:池损坏后
        ``_compute_period_features`` 回退进程内协程路径重算一次,值与从未
        启用池时逐值相等,且池标记 broken(后续期不再提交)。
        """
        from finboard_backtest.factor_lab import PriceFeatureProcessPool

        provider, _ = await _build_release(tmp_path)
        manifest = _real_manifest(_price_only_spec())
        decision_at = _decision_at(_month_end_decisions()[4])
        reference = await _compute_period_features(
            provider, manifest, decision_at, _RELEASE_ID
        )

        pool = PriceFeatureProcessPool(provider=provider, worker_count=1)
        await pool.start()
        try:
            assert pool.executor._processes
            for proc in list(pool.executor._processes.values()):
                proc.kill()
            # 等 executor 管理线程感知进程死亡(确定性-submit 前完成)。
            for _ in range(100):
                if pool.executor._broken:
                    break
                await asyncio.sleep(0.05)

            degraded = await _compute_period_features(
                provider,
                manifest,
                decision_at,
                _RELEASE_ID,
                process_pool=pool,
            )
        finally:
            await pool.aclose()

        assert pool.broken is True
        assert degraded == reference

    async def test_broken_pool_short_circuits_subsequent_periods(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """池标记 broken 后,后续期直接走进程内路径,不再向池提交任务。"""
        from concurrent.futures.process import BrokenProcessPool

        from finboard_backtest import factor_lab

        provider, _ = await _build_release(tmp_path)
        manifest = _real_manifest(_price_only_spec())
        decision_at = _decision_at(_month_end_decisions()[4])
        real_build = factor_lab.build_price_feature_snapshot
        calls = {"pool": 0, "in_process": 0}

        async def fake_build(**kwargs: Any) -> object:
            if kwargs.get("process_executor") is not None:
                calls["pool"] += 1
                raise BrokenProcessPool("worker died")
            calls["in_process"] += 1
            return await real_build(**kwargs)

        monkeypatch.setattr(factor_lab, "build_price_feature_snapshot", fake_build)

        pool = factor_lab.PriceFeatureProcessPool(provider=provider, worker_count=2)
        await pool.start()
        try:
            first = await _compute_period_features(
                provider, manifest, decision_at, _RELEASE_ID, process_pool=pool
            )
            second = await _compute_period_features(
                provider, manifest, decision_at, _RELEASE_ID, process_pool=pool
            )
        finally:
            await pool.aclose()

        assert first == second
        assert pool.broken is True
        # 第一期:池提交 1 次 + 降级进程内 1 次;第二期:只走进程内。
        assert calls == {"pool": 1, "in_process": 2}


@pytest.mark.asyncio
class TestChunkedLoadEquivalence:
    """AC:分块 gather 与逐期串行的 contexts 逐字段相等(顺序 / 内容)。"""

    async def test_chunked_equals_serial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真实发布 + 多期:默认分块与「分块=1 串行」的 contexts 全等。"""
        import finboard_backtest.research_run.signal_engine as signal_engine

        provider, _ = await _build_release(tmp_path)
        manifest = _real_manifest(_price_only_spec())

        def factory(release_id: str) -> FrozenReleaseProvider:
            assert release_id == _RELEASE_ID
            return provider

        chunked = await build_decision_load_contexts(
            manifest,
            release_provider_factory=factory,
            snapshot_provider=_noop_snapshot_provider,
            process_workers=0,
        )
        monkeypatch.setattr(signal_engine, "_DECISION_LOAD_CHUNK", 1)
        serial = await build_decision_load_contexts(
            manifest,
            release_provider_factory=factory,
            snapshot_provider=_noop_snapshot_provider,
            process_workers=0,
        )

        # 6 个月末决策期:默认分块(4)跨了块边界,串行(1)逐期。
        assert len(chunked) == len(_month_end_decisions())
        assert len(chunked) > _DECISION_LOAD_CHUNK
        _assert_contexts_equal(chunked, serial)
        # 期序严格递增(结果按原始期序归位)。
        dates = [item.context.decision_at for item in chunked]
        assert dates == sorted(dates)

    async def test_multi_period_process_workers_equals_in_process(
        self, tmp_path: Path
    ) -> None:
        """AC:process_workers=2(真实 spawn 池)与 0 的 contexts 全等。"""
        provider, _ = await _build_release(tmp_path)
        manifest = _real_manifest(_price_only_spec())

        def factory(release_id: str) -> FrozenReleaseProvider:
            assert release_id == _RELEASE_ID
            return provider

        in_process = await build_decision_load_contexts(
            manifest,
            release_provider_factory=factory,
            snapshot_provider=_noop_snapshot_provider,
            process_workers=0,
        )
        via_pool = await build_decision_load_contexts(
            manifest,
            release_provider_factory=factory,
            snapshot_provider=_noop_snapshot_provider,
            process_workers=2,
        )
        _assert_contexts_equal(via_pool, in_process)


@pytest.mark.asyncio
class TestFailureMarkerUnderChunking:
    """AC:#263 逐期失败标记在分块并行下仍精确定位失败期次。"""

    async def _contexts(self, manifest: ResearchRunManifest, provider: Any) -> Any:
        return await build_decision_load_contexts(
            manifest,
            release_provider_factory=lambda release_id: provider,
            snapshot_provider=_noop_snapshot_provider,
            process_workers=0,
        )

    async def test_first_failure_in_chunk_carries_marker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """第 2 期失败(在首个分块内)→ 标记第 2 期,消息类型不变。"""
        provider, _ = await _build_release(tmp_path)
        manifest = _real_manifest(_price_only_spec())
        failing_day = _month_end_decisions()[1]
        real_load = FrozenInputLoader.load_context

        async def failing_second(
            self: FrozenInputLoader,
            manifest_arg: ResearchRunManifest,
            *,
            decision_at: datetime,
            execution_at: datetime,
        ) -> LoadedDecisionContext:
            if decision_at == _decision_at(failing_day):
                raise RuntimeError("中期数据缺失:第 2 期发布损坏")
            return await real_load(
                self, manifest_arg, decision_at=decision_at, execution_at=execution_at
            )

        monkeypatch.setattr(FrozenInputLoader, "load_context", failing_second)

        with pytest.raises(RuntimeError, match="中期数据缺失") as excinfo:
            await self._contexts(manifest, provider)

        marker = read_decision_load_context(excinfo.value)
        assert marker is not None
        assert marker.decision_at == _decision_at(failing_day)
        assert marker.release_id == _RELEASE_ID
        # bare raise 语义:消息与类型均不被改写。
        assert str(excinfo.value) == "中期数据缺失:第 2 期发布损坏"

    async def test_multiple_failures_raise_first_in_period_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """同分块内第 2、4 期都失败 → 抛原始期序第一个(第 2 期),与串行一致。"""
        provider, _ = await _build_release(tmp_path)
        manifest = _real_manifest(_price_only_spec())
        failing_days = {_decision_at(_month_end_decisions()[1]),
                        _decision_at(_month_end_decisions()[3])}
        real_load = FrozenInputLoader.load_context

        async def failing_periods(
            self: FrozenInputLoader,
            manifest_arg: ResearchRunManifest,
            *,
            decision_at: datetime,
            execution_at: datetime,
        ) -> LoadedDecisionContext:
            if decision_at in failing_days:
                raise RuntimeError(f"中期数据缺失:{decision_at.date().isoformat()}")
            return await real_load(
                self, manifest_arg, decision_at=decision_at, execution_at=execution_at
            )

        monkeypatch.setattr(FrozenInputLoader, "load_context", failing_periods)

        first_failing = _month_end_decisions()[1]
        with pytest.raises(RuntimeError, match="中期数据缺失") as excinfo:
            await self._contexts(manifest, provider)

        # 两个失败期同分块:抛原始期序第一个(第 2 期),与串行首个失败一致。
        assert str(excinfo.value).endswith(first_failing.isoformat())
        marker = read_decision_load_context(excinfo.value)
        assert marker is not None
        assert marker.decision_at == _decision_at(first_failing)

    async def test_failure_in_later_chunk_found_after_earlier_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """失败落在第 2 个分块(第 6 期)→ 前块照常完成,标记精确定位第 6 期。"""
        provider, _ = await _build_release(tmp_path)
        manifest = _real_manifest(_price_only_spec())
        failing_day = _month_end_decisions()[5]
        real_load = FrozenInputLoader.load_context

        async def failing_sixth(
            self: FrozenInputLoader,
            manifest_arg: ResearchRunManifest,
            *,
            decision_at: datetime,
            execution_at: datetime,
        ) -> LoadedDecisionContext:
            if decision_at == _decision_at(failing_day):
                raise RuntimeError("中期数据缺失:第 6 期发布损坏")
            return await real_load(
                self, manifest_arg, decision_at=decision_at, execution_at=execution_at
            )

        monkeypatch.setattr(FrozenInputLoader, "load_context", failing_sixth)

        def factory(release_id: str) -> FrozenReleaseProvider:
            return provider

        with pytest.raises(RuntimeError, match="第 6 期发布损坏") as excinfo:
            await build_decision_load_contexts(
                manifest,
                release_provider_factory=factory,
                snapshot_provider=_noop_snapshot_provider,
                process_workers=0,
            )
        marker = read_decision_load_context(excinfo.value)
        assert marker is not None
        assert marker.decision_at == _decision_at(failing_day)


# ---- stub provider:池启动失败降级(不依赖真实 spawn)------------------------


@dataclass(frozen=True, slots=True)
class _StubExecution:
    lot_size: Decimal = Decimal("100")
    price_tick: Decimal = Decimal("0.01")
    settlement_days: int = 1
    multiplier: Decimal = Decimal("1")
    margin_rate: Decimal | None = None
    stamp_tax_rate: Decimal = Decimal("0.001")
    commission_rate: Decimal = Decimal("0.0003")
    commission_min: Decimal = Decimal("5")
    trading_calendar: str = "SSE"
    allows_short: bool = False


@dataclass(frozen=True, slots=True)
class _StubInstrument:
    code: str
    name: str = "sample"
    name_history: tuple[tuple[str, date, date | None], ...] = ()
    market: Market = Market.A_SHARE
    asset_class: AssetClass = AssetClass.EQUITY
    ready: bool = True
    execution: _StubExecution = field(default_factory=_StubExecution)
    list_date: date | None = date(2020, 1, 1)
    delist_date: date | None = None
    coverage_pct: Decimal = Decimal("1.0")
    suspended_sessions: int = 0


@dataclass
class _StubRelease:
    release_id: str
    instruments: tuple[_StubInstrument, ...]
    period: BarPeriod = BarPeriod.D1
    adjustment: str = "qfq"
    start_date: date = _START
    end_date: date = _END
    source: str = "stub"
    version: str = "v1"
    release_checksum: str = "c" * 64
    is_usable: bool = True
    dataset_kind: object = ReleaseDatasetKind.BARS


@dataclass
class _StubPoint:
    timestamp: datetime
    close: Decimal
    available_at: datetime


@dataclass
class _StubPitBar:
    bar: Any
    available_at: datetime


@dataclass
class _StubBarBody:
    close: Decimal
    timestamp: datetime


@dataclass
class _StubProvider:
    """最小 stub 发布 provider(触发 monthly 决策推导与逐期重算)。

    PIT 语义对齐真实 ``FrozenReleaseProvider``:日线 available_at = 当日
    15:30 Asia/Shanghai(收盘后,= 07:30 UTC),且只暴露
    ``available_at <= decision_at`` 的 bar/price —— 决策时点(15:00 UTC)
    当日 bar 可见,与真实链路一致。
    """

    release: _StubRelease
    closes_by_symbol: dict[str, dict[date, Decimal]]

    @staticmethod
    def _available_at(day: date) -> datetime:
        return datetime.combine(
            day, time(15, 30), tzinfo=ZoneInfo("Asia/Shanghai")
        ).astimezone(UTC)

    async def fetch_point_in_time_bars(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> list[_StubPitBar]:
        del period, start, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if not by_date:
            return []
        return [
            _StubPitBar(
                _StubBarBody(
                    close=value,
                    timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                ),
                self._available_at(day),
            )
            for day, value in sorted(by_date.items())
            if day <= end and self._available_at(day) <= decision_at
        ]

    async def fetch_point_in_time_prices(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> list[_StubPoint]:
        del period, start, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if not by_date:
            return []
        return [
            _StubPoint(
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                close=value,
                available_at=self._available_at(day),
            )
            for day, value in sorted(by_date.items())
            if day <= end and self._available_at(day) <= decision_at
        ]

    async def fetch_close_history(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> CloseHistoryColumns:
        """列式 PIT close(issue #300);可见性与 fetch_point_in_time_prices 一致。"""
        del period, start, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        visible = [
            (day, value)
            for day, value in sorted((by_date or {}).items())
            if day <= end and self._available_at(day) <= decision_at
        ]
        return CloseHistoryColumns(
            dates=tuple(day for day, _ in visible),
            available_at=tuple(self._available_at(day) for day, _ in visible),
            closes=np.array([float(value) for _, value in visible], dtype=np.float64),
            last_timestamp=(
                datetime.combine(visible[-1][0], datetime.min.time(), tzinfo=UTC)
                if visible
                else None
            ),
        )

    async def fetch_bars(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[_StubBarBody]:
        del period, start, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if not by_date:
            return []
        return [
            _StubBarBody(
                close=value,
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            )
            for day, value in sorted(by_date.items())
            if day <= end
        ]


def _stub_manifest(spec: ResearchStrategySpec) -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id="RR-multiproctest0001",
        idempotency_key="multiproc-test-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="release-stub",
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        factor_snapshots=(),
        parameters={"rebalance_frequency": "monthly"},
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


def _stub_provider() -> _StubProvider:
    stub_sessions = SESSIONS[: 22 * 6]
    return _StubProvider(
        release=_StubRelease(
            release_id="release-stub",
            instruments=tuple(_StubInstrument(code=code) for code in _SYMBOLS),
            start_date=stub_sessions[0],
            end_date=stub_sessions[-1],
        ),
        closes_by_symbol={
            code: {day: _close(code, day) for day in stub_sessions}
            for code in _SYMBOLS
        },
    )


@pytest.mark.asyncio
class TestPoolStartFailureDegrades:
    """AC:进程池构建失败不炸 run —— 具名降级为进程内协程路径,结果不变。"""

    async def test_start_failure_falls_back_to_in_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finboard_backtest import factor_lab

        def explode(**kwargs: Any) -> Any:
            raise OSError("spawn unavailable")

        monkeypatch.setattr(
            factor_lab, "_create_price_feature_process_executor", explode
        )
        provider = _stub_provider()
        manifest = _stub_manifest(_price_only_spec(("release-stub",)))

        def factory(release_id: str) -> _StubProvider:
            assert release_id == "release-stub"
            return provider

        with_pool = await build_decision_load_contexts(
            manifest,
            release_provider_factory=factory,  # type: ignore[arg-type]
            snapshot_provider=_noop_snapshot_provider,
            process_workers=2,
        )
        without_pool = await build_decision_load_contexts(
            manifest,
            release_provider_factory=factory,  # type: ignore[arg-type]
            snapshot_provider=_noop_snapshot_provider,
            process_workers=0,
        )
        assert with_pool
        _assert_contexts_equal(with_pool, without_pool)


class TestSettingsAndFactoryWiring:
    """AC:settings 默认值确定;工厂按 settings 解析 process_workers。"""

    def test_settings_default_is_four(self) -> None:
        from finboard_app.config import Settings

        assert (
            Settings.model_fields["research_price_feature_process_workers"].default
            == 4
        )

    def _manifest(self) -> ResearchRunManifest:
        spec = build_strategy_template(
            "multi_factor",
            strategy_id="multiproc_wiring",
            dataset_release_ids=(_RELEASE_ID,),
        )
        return ResearchRunManifest(
            run_id="RR-multiprocwiri0001",
            idempotency_key="multiproc-wiring-0001",
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
            parameters={"rebalance_frequency": "monthly"},
            code_version="abcdef0123456789",
            initial_capital=Decimal("100000"),
            requested_by="unit-test",
        )

    def _adapter(self: Any, settings: Any) -> Any:
        factory = build_signal_engine_adapter_factory(
            cast(async_sessionmaker[Any], object()),
            release_root=str(Path("data_releases")),
            settings_factory=(lambda: settings) if settings is not None else None,
        )
        return factory(self._manifest())

    def test_factory_resolves_positive_workers(self) -> None:
        class _Settings:
            research_price_feature_process_workers = 3

        adapter = self._adapter(_Settings())
        assert adapter._process_workers == 3

    def test_factory_defaults_to_zero_without_settings(self) -> None:
        assert self._adapter(None)._process_workers == 0

        class _NoneSettings:
            research_price_feature_process_workers = None

        assert self._adapter(_NoneSettings())._process_workers == 0

    def test_factory_tolerates_raising_settings_factory(self) -> None:
        def _explode() -> Any:
            raise RuntimeError("config unavailable")

        factory = build_signal_engine_adapter_factory(
            cast(async_sessionmaker[Any], object()),
            release_root=str(Path("data_releases")),
            settings_factory=_explode,
        )
        # 工厂返回基类 ResearchStrategyAdapter,_process_workers 在子类。
        adapter = cast(Any, factory(self._manifest()))
        assert adapter._process_workers == 0


@pytest.mark.asyncio
class TestCloseMatrixPrebuild:
    """分块并行前的 close 矩阵预建(幂等,避免并发首建打穿矩阵收益)。"""

    async def test_ensure_close_histories_is_idempotent(self, tmp_path: Path) -> None:
        provider, _ = await _build_release(tmp_path)
        counter = {"pit": 0}

        # issue #300:矩阵构建读取入口改为列式 fetch_close_history。
        async def counting_pit(symbol: Symbol, period: BarPeriod, start: date,
                               end: date, *, decision_at: datetime,
                               adjust: str = "qfq") -> object:
            counter["pit"] += 1
            return await FrozenReleaseProvider.fetch_close_history(
                provider, symbol, period, start, end,
                decision_at=decision_at, adjust=adjust,
            )

        provider.fetch_close_history = counting_pit  # type: ignore[assignment]
        manifest = _real_manifest(_price_only_spec())

        def factory(release_id: str) -> FrozenReleaseProvider:
            return provider

        loader = FrozenInputLoader(
            release_provider_factory=factory,
            snapshot_provider=_noop_snapshot_provider,
        )
        await loader.ensure_close_histories(manifest)
        assert len(loader.close_histories) == len(_SYMBOLS)
        assert counter["pit"] == len(_SYMBOLS)
        # 幂等:已建后再次调用零读取。
        await loader.ensure_close_histories(manifest)
        assert counter["pit"] == len(_SYMBOLS)

    async def test_chunked_load_reads_each_symbol_exactly_once(
        self, tmp_path: Path
    ) -> None:
        """分块加载全程:每标的只发生 1 次全区间 PIT 读取(矩阵构建)。

        若矩阵惰性首建遇并发(无预建),同分块其它期会看到空矩阵而回退逐期
        读取,PIT 计数将远超标的数。issue #300 后 ``fetch_close_history`` 同时
        是矩阵构建与逐期价格特征重算的读取入口:期望计数 = 矩阵构建 1 次 +
        每期特征重算 1 次(均按标的数);矩阵被打穿回退全区间读取时计数显著
        超过该值。
        """
        provider, _ = await _build_release(tmp_path)
        counter = {"pit": 0}

        # issue #300:矩阵构建读取入口改为列式 fetch_close_history。
        async def counting_pit(symbol: Symbol, period: BarPeriod, start: date,
                               end: date, *, decision_at: datetime,
                               adjust: str = "qfq") -> object:
            counter["pit"] += 1
            return await FrozenReleaseProvider.fetch_close_history(
                provider, symbol, period, start, end,
                decision_at=decision_at, adjust=adjust,
            )

        provider.fetch_close_history = counting_pit  # type: ignore[assignment]
        manifest = _real_manifest(_price_only_spec())

        def factory(release_id: str) -> FrozenReleaseProvider:
            return provider

        contexts = await build_decision_load_contexts(
            manifest,
            release_provider_factory=factory,
            snapshot_provider=_noop_snapshot_provider,
            process_workers=0,
        )
        assert len(contexts) == len(_month_end_decisions())
        # issue #300 后 fetch_close_history 同时覆盖矩阵构建(每标的 1 次)与
        # 逐期价格特征重算(每期 × 每标的各 1 次);矩阵被打穿回退全区间
        # 逐期读取时,计数将显著超过该值。
        assert counter["pit"] == len(_SYMBOLS) * (1 + len(_month_end_decisions()))
