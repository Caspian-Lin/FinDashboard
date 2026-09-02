"""指数基准数据链路端到端集成测试(issue #256)。

#184 读取端能力的运营化:打通「指数登记 → 同步 → mixed 发布 →
benchmark_return 非 null」全链路:

* ``discover_indices``(受控登记表)→ ``InstrumentRepository.sync_with_diff``
  → instruments 表出现 ``instrument_type=index`` 行(断点①,登记写入者);
* 指数 + 股票 bars 进 ParquetCache → ``multi_asset_mixed`` 发布 —— manifest
  只允许一个 bars 主发布,基准行情必须与候选池同处一份发布(#184);
* research_run worker 走**真实 FrozenReleaseProvider**(非 stub)→ COMPLETED、
  ``benchmark_return`` 非 null、超额方向正确(断点②③④闭合);
* 指数不进候选池:UNIVERSE artifact 无 000300.SH(只做基准数据,不可撮合)。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。不连 broker / 不下实盘单。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs import JobExecutorRegistry
from finboard_backtest.background_jobs.executors import ResearchRunExecutor
from finboard_backtest.background_jobs.executors.research_run import (
    default_store_factory,
)
from finboard_backtest.background_jobs.worker import BackgroundWorker, WorkerConfig
from finboard_backtest.research_run import (
    FrozenArtifactRef,
    ResearchRunManifest,
    ResearchRunStatus,
    stable_checksum,
    to_json_value,
)
from finboard_backtest.research_run.signal_engine import (
    SignalEnginePipelineAdapter,
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
from finboard_data.releases import ReleaseDatasetKind
from finboard_persistence import (
    BackgroundJobRepository,
    Base,
    ResearchRunRepository,
    create_async_engine,
    session_factory,
)
from finboard_persistence.models import (
    BackgroundJobModel,
    InstrumentModel,
    ResearchDatasetReleaseModel,
    ResearchRunArtifactModel,
    ResearchRunModel,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

pytestmark = pytest.mark.asyncio

_RELEASE_ID = "mixed-index-r256"
_STOCKS = ("600001.SH", "600002.SH", "600003.SH")
_BENCHMARK = "000300.SH"
_START = date(2024, 1, 1)
_END = date(2024, 4, 30)


def _run_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"RR-{digest}"


def _trading_days() -> list[date]:
    """发布区间的真实 A 股交易日(akshare / exchange_calendars 双源一致)。"""
    from finboard_data.trading_calendar import trading_days

    return sorted(trading_days(_START, _END))


@pytest_asyncio.fixture(scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    from tests.integration.conftest import TEST_DB_URL, ensure_test_db

    await ensure_test_db(TEST_DB_URL)
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(delete(ResearchRunArtifactModel))
        await conn.execute(delete(ResearchRunModel))
        await conn.execute(delete(BackgroundJobModel))
        await conn.execute(
            delete(ResearchDatasetReleaseModel).where(
                ResearchDatasetReleaseModel.release_id == _RELEASE_ID
            )
        )
        await conn.execute(
            delete(InstrumentModel).where(
                InstrumentModel.code.in_([*_STOCKS, _BENCHMARK])
            )
        )


# ---- 步骤①:登记(受控登记表 → instruments 表)--------------------------------


async def test_index_registration_via_sync_with_diff(engine: AsyncEngine) -> None:
    """discover_indices 经 sync_with_diff 写入 instruments(issue #256 断点①)。"""
    from finboard_data.discovery import UniverseDiscovery
    from finboard_persistence import InstrumentRepository

    discovery = UniverseDiscovery()
    indices = await discovery.discover_indices()
    dicts: list[dict[str, object]] = [
        {
            "code": ins.code,
            "name": ins.name,
            "market": ins.market.value,
            "instrument_type": ins.instrument_type.value,
            "exchange": ins.exchange,
            "listing_board": ins.listing_board.value,
        }
        for ins in indices
    ]
    async with session_factory(engine)() as session:
        result = await InstrumentRepository(session).sync_with_diff(
            dicts, as_of=date.today()
        )
        await session.commit()
    # 共享测试库可能已有历史登记行(只删 000300.SH 做隔离):本次同步零失败,
    # 终态必须覆盖全部登记表条目。
    assert result.new + result.updated >= len(indices)

    async with session_factory(engine)() as session:
        rows, _ = await InstrumentRepository(session).list_page(
            instrument_type="index", status=None, limit=100
        )
        by_code = {row.code: row for row in rows}
        assert _BENCHMARK in by_code
        row = by_code[_BENCHMARK]
        assert row.name == "沪深300"
        assert row.market == "a_share"
        assert row.instrument_type == "index"
        assert row.exchange == "SSE"
        # 指数无 list_date 结构化上游:保持 null(可见缺失,不虚构元数据)。
        assert row.list_date is None


# ---- 步骤②③④:缓存 → mixed 发布 → research_run benchmark_return --------------


def _price_only_spec() -> ResearchStrategySpec:
    """价格因子专用 multi_factor 规格(多期免快照,基准收益来自同发布指数)。"""
    spec = build_strategy_template(
        "multi_factor",
        strategy_id="index_bench_r256",
        dataset_release_ids=(_RELEASE_ID,),
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
            # 测试样本 instruments 无 list_date,关掉上市天数过滤。
            "universe": spec.universe.model_copy(update={"min_listing_days": 0}),
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


def _manifest() -> ResearchRunManifest:
    spec = _price_only_spec()
    return ResearchRunManifest(
        run_id=_run_id("index-bench-r256"),
        idempotency_key="index-bench-r256",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id=_RELEASE_ID,
                version="v1",
                checksum="a" * 64,
                capabilities=("stock", "index"),
            ),
        ),
        parameters={"rebalance_frequency": "monthly"},
        benchmark_config={"symbol": _BENCHMARK},
        # 3 只候选 RANK_TOP 0.5 只选 1 只,1/n=1.0 会撞默认 0.35 风险贡献上限
        # (数学不可行,fail-closed);测试关注基准链路,放宽到 1.0。
        portfolio_config={"overrides": {"max_risk_contribution": 1.0}},
        code_version="abcdef0123456789",
        initial_capital=Decimal("200000"),
        requested_by="index-chain-test",
    )


async def _register_instruments(engine: AsyncEngine) -> None:
    """股票(手工清单)+ 指数(discover_indices)经 sync_with_diff 登记。"""
    from finboard_data.discovery import UniverseDiscovery
    from finboard_persistence import InstrumentRepository

    dicts: list[dict[str, object]] = [
        {
            "code": code,
            "name": f"样本{index + 1}号",  # 不得含 ST 子串(issue #213)
            "market": "a_share",
            "instrument_type": "stock",
            "exchange": "SSE",
            "listing_board": "sse_main",
        }
        for index, code in enumerate(_STOCKS)
    ]
    for ins in await UniverseDiscovery().discover_indices():
        if ins.code != _BENCHMARK:
            continue
        dicts.append(
            {
                "code": ins.code,
                "name": ins.name,
                "market": ins.market.value,
                "instrument_type": ins.instrument_type.value,
                "exchange": ins.exchange,
                "listing_board": ins.listing_board.value,
            }
        )
    async with session_factory(engine)() as session:
        await InstrumentRepository(session).sync_with_diff(dicts, as_of=date.today())
        await session.commit()


async def _publish_mixed_release(engine: AsyncEngine, tmp_path: Path) -> None:
    """股票 + 指数 bars 进缓存,发布 multi_asset_mixed(单一 bars 主发布)。"""
    from finboard_data import DatasetReleaseSpec
    from finboard_persistence import ResearchDatasetReleaseService

    days = _trading_days()
    cache = ParquetCache(tmp_path / "cache")
    # 指数直线 10 → 15(+50%):基准收益方向确定。
    index_symbol = Symbol(_BENCHMARK, Market.A_SHARE)
    total = len(days) - 1
    index_bars: list[Bar] = []
    for index, day in enumerate(days):
        close = Decimal("10") + Decimal("5") * Decimal(index) / Decimal(total)
        index_bars.append(
            Bar(
                symbol=index_symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                open=close * Decimal("0.999"),
                high=close * Decimal("1.005"),
                low=close * Decimal("0.995"),
                close=close,
                volume=Decimal("100000000"),
                amount=Decimal("0"),
                source="fixed_sample",
            )
        )
    await cache.write(index_symbol, BarPeriod.D1, "qfq", index_bars)

    # 股票:固定 seed 独立随机游走(协方差满秩,组合优化可用)。
    import numpy as np

    rng = np.random.default_rng(20240401)
    for code in _STOCKS:
        symbol = Symbol(code, Market.A_SHARE)
        price = 10.0
        bars: list[Bar] = []
        for day in days:
            price *= 1.0 + float(rng.normal(0.0008, 0.008))
            close = Decimal(str(round(price, 4)))
            bars.append(
                Bar(
                    symbol=symbol,
                    period=BarPeriod.D1,
                    timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                    open=close * Decimal("0.999"),
                    high=close * Decimal("1.005"),
                    low=close * Decimal("0.995"),
                    close=close,
                    volume=Decimal("1000000"),
                    amount=close * Decimal("1000000"),
                    source="fixed_sample",
                )
            )
        await cache.write(symbol, BarPeriod.D1, "qfq", bars)

    async with session_factory(engine)() as session:
        service = ResearchDatasetReleaseService(
            session,
            cache_dir=tmp_path / "cache",
            release_root=tmp_path / "releases",
        )
        release = await service.publish(
            DatasetReleaseSpec(
                release_id=_RELEASE_ID,
                dataset_name="mixed_index_benchmark_bars",
                source="fixed_sample",
                version="r256-integration",
                start_date=_START,
                end_date=_END,
                code_version="integration-test",
                required_capabilities=("stock", "index"),
            ),
            [*_STOCKS, _BENCHMARK],
        )
        await session.commit()

    assert release.dataset_kind is ReleaseDatasetKind.BARS
    index_item = release.instrument(_BENCHMARK)
    assert index_item is not None
    assert index_item.ready


def _worker_factory(tmp_path: Path) -> object:
    """注入真实 FrozenReleaseProvider(读发布目录,非 stub)的适配器工厂。"""

    def factory(manifest: ResearchRunManifest) -> SignalEnginePipelineAdapter:
        from finboard_data import FrozenReleaseProvider

        def release_factory(release_id: str) -> FrozenReleaseProvider:
            return FrozenReleaseProvider(
                release_root=tmp_path / "releases",
                release_id=release_id,
            )

        async def snapshot_provider(snapshot_id: str) -> None:
            return None

        return SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=release_factory,
            snapshot_provider=snapshot_provider,
        )

    return factory


def _build_worker(engine: AsyncEngine, tmp_path: Path) -> BackgroundWorker:
    registry = JobExecutorRegistry()
    registry.register(
        "research_run",
        ResearchRunExecutor(
            session_maker=session_factory(engine),
            store_factory=default_store_factory,
            adapter_factory=_worker_factory(tmp_path),  # type: ignore[arg-type]
        ),
    )
    return BackgroundWorker(
        engine=engine,
        session_maker=session_factory(engine),
        registry=registry,
        config=WorkerConfig(
            worker_id="w-index-bench",
            poll_interval_seconds=0.05,
            max_concurrent=2,
            lease_timeout_seconds=60,
            heartbeat_interval_seconds=10.0,
        ),
    )


async def _queue_and_run(engine: AsyncEngine, tmp_path: Path) -> str:
    manifest = _manifest()
    async with session_factory(engine)() as session:
        repo = ResearchRunRepository(session)
        row, _ = await repo.create_or_get(
            run_id=manifest.run_id,
            idempotency_key=manifest.idempotency_key,
            replay_of_run_id=None,
            strategy_id=manifest.strategy_spec.strategy_id,
            strategy_kind=manifest.strategy_kind,
            status=ResearchRunStatus.QUEUED.value,
            schema_version=manifest.schema_version,
            manifest_checksum=manifest.checksum,
            manifest=cast(dict[str, object], to_json_value(manifest)),
            requested_by=manifest.requested_by,
        )
        payload: dict[str, object] = {
            "run_id": manifest.run_id,
            "strategy_kind": manifest.strategy_kind,
        }
        checksum = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        job_row, _ = await BackgroundJobRepository(session).create_or_get(
            job_id=generate_background_job_id(),
            idempotency_key=manifest.idempotency_key,
            kind="research_run",
            queue="research",
            status=BackgroundJobStatus.QUEUED.value,
            priority=0,
            payload=payload,
            payload_checksum=checksum,
            max_attempts=3,
            requested_by=manifest.requested_by,
        )
        row.job_id = job_row.job_id
        await repo.checkpoint()

    worker = _build_worker(engine, tmp_path)
    await worker._fill_concurrency()
    for _ in range(400):
        if not worker._inflight:
            break
        await asyncio.sleep(0.02)
    assert not worker._inflight
    return manifest.run_id


class TestIndexBenchmarkChain:
    async def test_mixed_release_run_benchmark_return_non_null(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """含指数的 mixed 发布 → research_run benchmark_return 非 null(#256)。"""
        await _register_instruments(engine)
        await _publish_mixed_release(engine, tmp_path)
        run_id = await _queue_and_run(engine, tmp_path)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value, (
                f"status={run_row.status} error_code={run_row.error_code} "
                f"error_summary={run_row.error_summary}"
            )
            result = run_row.result
            assert result is not None
            benchmark_return = result["benchmark_return"]
            assert benchmark_return is not None
            bm = float(str(benchmark_return))
            st = float(str(result["strategy_return"]))
            ex = float(str(result["excess_return"]))
            # 基准:买入持有 000300.SH(10 → 15,+50%),来自真实冻结发布。
            assert bm == pytest.approx(0.5, abs=1e-6)
            assert result["benchmark_symbol"] == _BENCHMARK
            assert ex == pytest.approx(st - bm, abs=1e-9)
            # 多期:2024-01~2024-04,期末无下一成交日的 4 月剔除 → 3 期。
            assert result["execution_mode"] == "multi_period"
            assert result["decision_count"] == 3

    async def test_benchmark_index_not_in_universe_candidates(
        self, engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """指数不进候选池:UNIVERSE artifact 无 000300.SH(#184 不可撮合边界)。"""
        await _register_instruments(engine)
        await _publish_mixed_release(engine, tmp_path)
        run_id = await _queue_and_run(engine, tmp_path)

        async with session_factory(engine)() as session:
            rows = (
                (
                    await session.execute(
                        select(ResearchRunArtifactModel).where(
                            ResearchRunArtifactModel.run_id == run_id,
                            ResearchRunArtifactModel.stage == "universe",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert rows, "UNIVERSE artifacts 应逐决策持久化"
            candidate_symbols: set[str] = set()
            for row in rows:
                payload = cast("dict[str, list[dict[str, object]]]", row.payload)
                for candidate in payload.get("candidates", []):
                    candidate_symbols.add(str(candidate["symbol"]))
            # 候选池只含股票;指数只做基准行情,不可撮合。
            assert candidate_symbols == set(_STOCKS)
            assert _BENCHMARK not in candidate_symbols
