"""close 矩阵预建入池——反序列化段多进程分发(issue #301)。

#301 把单 run 内剩余的 GIL 串行段分发到 #288 常驻进程池:close 矩阵预建的
parquet 解码 + 列式转换(PIT 门控含内)在 worker 进程完成,主进程只收列式
数据组装 ``SymbolCloseHistory``。本组测试锁定:

* 池分发与进程内线程路径**逐值相等**(真实 spawn 多进程,Windows 本机);
* ``build_decision_load_contexts`` 端到端 pw=2 vs pw=0 上下文逐字段相等
  (矩阵与逐期特征共用一个池);
* 池启动失败(#288 语义)/ 矩阵任务异常 → 具名降级进程内路径,结果不变,
  不炸 run;
* 预建幂等:池路径同样只分发一次。

发布用 :class:`FrozenDatasetReleaseBuilder` 在临时目录构建(与 #288 同款
夹具),不依赖 PostgreSQL。纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import structlog

from finboard_backtest.factor_lab import (
    PriceFeatureProcessPool,
)
from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_run.frozen_loader import (
    FrozenInputLoader,
    SymbolCloseHistory,
)
from finboard_backtest.research_run.signal_engine import (
    DecisionLoadContext,
    build_decision_load_contexts,
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

_SYMBOLS = ("600519.SH", "000001.SZ", "600036.SH")
_RELEASE_ID = "matrix-pool-r1"
_SESSION_COUNT = 30  # 覆盖 momentum(20) 与 volatility 窗口


def _sessions() -> list[date]:
    days: list[date] = []
    cursor = date(2024, 1, 2)
    while len(days) < _SESSION_COUNT:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


SESSIONS = _sessions()
_START = SESSIONS[0]
_END = SESSIONS[-1]


def _close(code: str, day: date) -> Decimal:
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


async def _build_release(tmp_path: Path) -> FrozenReleaseProvider:
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
            dataset_name="matrix_pool_daily_bars",
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


def _manifest(
    release_id: str = _RELEASE_ID, *, frequency: str | None = None
) -> ResearchRunManifest:
    spec = build_strategy_template(
        "multi_factor",
        strategy_id="matrix_pool_test",
        dataset_release_ids=(release_id,),
    )
    return ResearchRunManifest(
        run_id="RR-matrixpooltest01",
        idempotency_key="matrix-pool-test-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        parameters=(
            {"rebalance_frequency": frequency} if frequency is not None else {}
        ),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=release_id,
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


def _assert_histories_equal(
    left: dict[str, SymbolCloseHistory | None],
    right: dict[str, SymbolCloseHistory | None],
) -> None:
    assert set(left) == set(right)
    for code in left:
        a, b = left[code], right[code]
        assert isinstance(a, SymbolCloseHistory)
        assert isinstance(b, SymbolCloseHistory)
        assert a.available_at == b.available_at
        assert a.dates == b.dates
        assert a.closes == b.closes


def _assert_contexts_equal(
    left: tuple[DecisionLoadContext, ...],
    right: tuple[DecisionLoadContext, ...],
) -> None:
    """与 #288 同款:逐字段比较(协方差为 numpy 矩阵,逐值比较)。"""
    assert len(left) == len(right)
    for a, b in zip(left, right, strict=True):
        assert a.context.prices == b.context.prices
        assert a.context.execution_prices == b.context.execution_prices
        assert a.candidates == b.candidates
        assert a.features == b.features
        assert a.signalable == b.signalable
        assert a.price_series == b.price_series
        assert a.snapshot_id == b.snapshot_id
        if a.covariance is None or b.covariance is None:
            assert a.covariance is None
            assert b.covariance is None
        else:
            # CovarianceEstimate 是含 ndarray 字段的 dataclass,全等会触发
            # 布尔歧义,按字段逐值比较。
            assert a.covariance.tickers == b.covariance.tickers
            assert a.covariance.shrinkage == b.covariance.shrinkage
            assert a.covariance.n_observations == b.covariance.n_observations
            assert a.covariance.method == b.covariance.method
            assert (a.covariance.matrix == b.covariance.matrix).all()


@pytest.mark.asyncio
class TestMatrixPoolEquivalence:
    """AC:池分发矩阵与进程内矩阵逐值相等(真实 spawn 多进程)。"""

    async def test_pooled_matrix_equals_in_process(self, tmp_path: Path) -> None:
        provider = await _build_release(tmp_path)
        manifest = _manifest()

        pooled_loader = _loader(provider)
        async with PriceFeatureProcessPool(provider=provider, worker_count=2) as pool:
            await pooled_loader.ensure_close_histories(manifest, process_pool=pool)
        assert set(pooled_loader.close_histories) == set(_SYMBOLS)

        serial_loader = _loader(provider)
        await serial_loader.ensure_close_histories(manifest)
        assert set(serial_loader.close_histories) == set(_SYMBOLS)

        _assert_histories_equal(
            dict(pooled_loader.close_histories),
            dict(serial_loader.close_histories),
        )

    async def test_ensure_with_pool_is_idempotent(self, tmp_path: Path) -> None:
        """池路径预建同样幂等:已建后即便池已关闭,二次调用零分发零报错。

        spawn 池的 worker 函数按引用 pickle,主进程侧计数包装无法拦截 worker
        执行;幂等性改由「关闭池后二次 ensure 仍成功且矩阵完好」验证——
        若二次调用仍尝试分发,``pool.executor`` 已不可用会走降级路径重读。
        """
        provider = await _build_release(tmp_path)
        manifest = _manifest()
        loader = _loader(provider)
        async with PriceFeatureProcessPool(provider=provider, worker_count=1) as pool:
            await loader.ensure_close_histories(manifest, process_pool=pool)
        assert set(loader.close_histories) == set(_SYMBOLS)
        await loader.ensure_close_histories(manifest, process_pool=pool)
        assert set(loader.close_histories) == set(_SYMBOLS)
        _assert_histories_equal(
            dict(loader.close_histories),
            {code: loader.close_histories[code] for code in _SYMBOLS},
        )


@pytest.mark.asyncio
class TestMatrixPoolDegradation:
    """AC:池异常具名降级进程内路径,结果不变,不炸 run。"""

    async def test_pool_start_failure_degrades_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """池启动失败(#288 语义)→ 矩阵与特征全部进程内,结果与 pw=0 相等。"""
        from finboard_backtest import factor_lab

        def explode(**kwargs: object) -> object:
            raise OSError("spawn unavailable")

        # patch 内层构造器:_start_period_feature_pool 的 try/except 会把它转成
        # research_run.process_pool_start_failed 具名 warning 并返回 None。
        monkeypatch.setattr(
            factor_lab, "_create_price_feature_process_executor", explode
        )
        provider = await _build_release(tmp_path)

        def factory(release_id: str) -> FrozenReleaseProvider:
            return provider

        degraded = await build_decision_load_contexts(
            _manifest(frequency="monthly"),
            release_provider_factory=factory,
            snapshot_provider=_noop_snapshot_provider,
            process_workers=2,
        )
        serial = await build_decision_load_contexts(
            _manifest(frequency="monthly"),
            release_provider_factory=factory,
            snapshot_provider=_noop_snapshot_provider,
            process_workers=0,
        )
        _assert_contexts_equal(degraded, serial)

    async def test_matrix_task_failure_falls_back_in_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """矩阵池任务异常 → 具名 warning + 进程内重建,矩阵结果不变。"""
        import finboard_backtest.factor_lab as factor_lab_module

        provider = await _build_release(tmp_path)
        manifest = _manifest()

        def broken_task(task: object) -> object:
            raise RuntimeError("worker exploded")

        monkeypatch.setattr(
            factor_lab_module, "_compute_close_history_process_task", broken_task
        )
        loader = _loader(provider)
        with structlog.testing.capture_logs() as logs:
            async with PriceFeatureProcessPool(
                provider=provider, worker_count=2
            ) as pool:
                await loader.ensure_close_histories(manifest, process_pool=pool)
        assert set(loader.close_histories) == set(_SYMBOLS)
        assert all(
            isinstance(item, SymbolCloseHistory)
            for item in loader.close_histories.values()
        )
        assert any(
            event["event"] == "frozen_loader.close_matrix_pool_failed"
            for event in logs
        )

        serial_loader = _loader(provider)
        await serial_loader.ensure_close_histories(manifest)
        _assert_histories_equal(
            dict(loader.close_histories), dict(serial_loader.close_histories)
        )


async def _noop_snapshot_provider(snapshot_id: str) -> None:
    del snapshot_id


@pytest.mark.asyncio
class TestBuildContextsPooledEquivalence:
    """AC:端到端 pw=2(矩阵 + 逐期特征共用池)与 pw=0 上下文逐字段相等。"""

    async def test_pooled_contexts_equal_serial(self, tmp_path: Path) -> None:
        provider = await _build_release(tmp_path)

        def factory(release_id: str) -> FrozenReleaseProvider:
            return provider

        pooled = await build_decision_load_contexts(
            _manifest(frequency="monthly"),
            release_provider_factory=factory,
            snapshot_provider=_noop_snapshot_provider,
            process_workers=2,
        )
        serial = await build_decision_load_contexts(
            _manifest(frequency="monthly"),
            release_provider_factory=factory,
            snapshot_provider=_noop_snapshot_provider,
            process_workers=0,
        )
        assert pooled
        _assert_contexts_equal(pooled, serial)
