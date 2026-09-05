"""issue #306 集成测试:run 误标防治 + 加载期打断自愈 + 僵尸检测 + run_status 透传。

覆盖验收标准:

* worker A 执行 run 期间 worker B 启动(启动恢复路径
  ``_recover_research_runs``),run 不被误标;lease 过期 / 无 job 的真孤儿
  照旧收敛 INTERRUPTED(回归:现状必现误标);
* 加载期 run 被外部标 interrupted → 分块探针秒级感知 → job 转
  retry_waiting(≤一个轮询周期)→ 自动重试续跑到 COMPLETED;
* 加载期 job 被 request_cancel → 秒级 cancelled 收口(不再孤儿悬挂);
* 无进展僵尸:heartbeat 正常续租但 progress/phase 不动 → 具名失败
  ``zombie_no_progress`` + retry_waiting,原属主心跳停止续租;健康 job 不误杀;
* ``GET /api/jobs/{id}`` / ``finboard_job_get`` 附 run_status。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。不连 broker / 不下实盘单。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_app.cli import _recover_research_runs
from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.background_jobs import JobExecutorRegistry
from finboard_backtest.background_jobs.contracts import (
    JobRecord,
    JobResult,
    ProgressCallback,
)
from finboard_backtest.background_jobs.executors import ResearchRunExecutor
from finboard_backtest.background_jobs.executors.research_run import (
    default_store_factory,
)
from finboard_backtest.background_jobs.worker import (
    BackgroundWorker,
    WorkerConfig,
)
from finboard_backtest.portfolio import AssetLotInfo, CovarianceEstimate
from finboard_backtest.research_run import (
    DecisionBundle,
    FeatureValue,
    FrozenArtifactRef,
    NormalizedSignal,
    ResearchRunManifest,
    ResearchRunStatus,
    ResearchStrategyAdapter,
    UniverseCandidate,
    stable_checksum,
    to_json_value,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
)
from finboard_backtest.research_run.signal_engine import (
    SignalEnginePipelineAdapter,
    build_run_interrupt_probe,
)
from finboard_data.releases import CloseHistoryColumns, ReleaseDatasetKind
from finboard_persistence import (
    BackgroundJobRepository,
    ResearchRunRepository,
    create_async_engine,
    session_factory,
)
from finboard_shared.background_jobs import (
    BackgroundJobStatus,
    generate_background_job_id,
)
from finboard_shared.types import AssetClass, Market

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    from finboard_persistence import Base
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
        await conn.execute(text("DELETE FROM research_run_artifacts"))
        await conn.execute(text("DELETE FROM research_runs"))
        await conn.execute(text("DELETE FROM background_jobs"))


# ---- 通用 helper(与 test_research_run_worker.py 同构)------------------------


def _run_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"RR-{digest}"


def _manifest(suffix: str) -> ResearchRunManifest:
    from finboard_backtest.strategy_spec import build_strategy_template

    spec = build_strategy_template(
        "ma_cross",
        strategy_id=f"ma_cross_306_{suffix}",
        dataset_release_ids=("frozen-release-v1",),
    )
    return ResearchRunManifest(
        run_id=_run_id(f"issue-306-{suffix}"),
        idempotency_key=f"issue-306-{suffix}",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="frozen-release-v1",
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        code_version="abcdef0123456789",
        initial_capital=Decimal("200000"),
        requested_by="issue-306-test",
    )


def _portfolio_input_at(day: int) -> PortfolioDecisionInput:
    symbols = ("A.SH", "B.SH", "C.SH")
    decision_at = datetime(2024, 1, 2 + day, 15, tzinfo=UTC)
    return PortfolioDecisionInput(
        business_date=date(2024, 1, 2 + day),
        decision_at=decision_at,
        execution_at=datetime(2024, 1, 3 + day, 9, 30, tzinfo=UTC),
        candidates=tuple(
            UniverseCandidate(
                symbol=symbol,
                included=True,
                reasons=("issue 306 集成测试候选池通过",),
                asset_class="equity",
                market="a_share",
            )
            for symbol in symbols
        ),
        features=tuple(
            FeatureValue(
                symbol=symbol,
                feature_id="close",
                value=10.0,
                source_artifact_ids=("frozen-release-v1",),
                available_at=decision_at,
            )
            for symbol in symbols
        ),
        signals=tuple(
            NormalizedSignal(
                symbol=symbol,
                score=1.0,
                action="buy",
                rule_id="issue-306-signal",
                rationale="issue 306 集成测试冻结信号",
            )
            for symbol in symbols
        ),
        prices=dict.fromkeys(symbols, 10.0),
        execution_prices=dict.fromkeys(symbols, 10.0),
        lot_info={
            symbol: AssetLotInfo(code=symbol, lot_size=100) for symbol in symbols
        },
        input_artifact_ids=("frozen-release-v1",),
        covariance=CovarianceEstimate(
            matrix=np.diag([0.01, 0.01, 0.01]),
            tickers=list(symbols),
            shrinkage=0.0,
            n_observations=252,
        ),
        sleeve_map=dict.fromkeys(symbols, "equity"),
    )


class _SlowPortfolioAdapter(PortfolioPipelineAdapter):
    """决策间 sleep 的慢速 adapter:给「worker A 活跃执行」留观察窗口。"""

    def __init__(self, *, decisions: int, sleep_seconds: float) -> None:
        super().__init__(
            strategy_kind="ma_cross",
            decision_inputs=tuple(
                _portfolio_input_at(day) for day in range(decisions)
            ),
        )
        self._sleep_seconds = sleep_seconds

    async def decisions(
        self,
        manifest: ResearchRunManifest,
    ) -> AsyncIterator[DecisionBundle]:
        index = 0
        async for decision in super().decisions(manifest):
            if index > 0:
                await asyncio.sleep(self._sleep_seconds)
            index += 1
            yield decision


async def _queue_double_write(
    engine: AsyncEngine,
    manifest: ResearchRunManifest,
) -> str:
    """模拟 queue_research_run 路由双写(research_runs.job_id 关联 job)。"""

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
        job_repo = BackgroundJobRepository(session)
        payload: dict[str, object] = {
            "run_id": manifest.run_id,
            "strategy_kind": manifest.strategy_kind,
        }
        checksum = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        job_row, _ = await job_repo.create_or_get(
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
    return manifest.run_id


def _build_worker(
    engine: AsyncEngine,
    *,
    adapter_factory: ResearchStrategyAdapter | object | None = None,
    worker_id: str = "w-306",
    zombie_no_progress_seconds: float = 3600.0,
    heartbeat_interval_seconds: float = 10.0,
    lease_timeout_seconds: float = 60.0,
    retry_backoff_seconds: float = 30.0,
) -> BackgroundWorker:
    registry = JobExecutorRegistry()
    registry.register(
        "research_run",
        ResearchRunExecutor(
            session_maker=session_factory(engine),
            store_factory=default_store_factory,
            adapter_factory=adapter_factory,  # type: ignore[arg-type]
        ),
    )
    return BackgroundWorker(
        engine=engine,
        session_maker=session_factory(engine),
        registry=registry,
        config=WorkerConfig(
            worker_id=worker_id,
            poll_interval_seconds=0.05,
            max_concurrent=2,
            lease_timeout_seconds=lease_timeout_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            zombie_no_progress_seconds=zombie_no_progress_seconds,
            retry_backoff_seconds=retry_backoff_seconds,
        ),
    )


async def _drain_worker(worker: BackgroundWorker) -> None:
    await worker._fill_concurrency()
    for _ in range(600):
        if not worker._inflight:
            break
        await asyncio.sleep(0.02)
    assert not worker._inflight


async def _get_run(engine: AsyncEngine, run_id: str):
    async with session_factory(engine)() as session:
        return await ResearchRunRepository(session).get(run_id)


async def _get_job(engine: AsyncEngine, job_id: str):
    async with session_factory(engine)() as session:
        return await BackgroundJobRepository(session).get(job_id)


async def _wait_until(predicate, *, timeout_seconds: float, message: str) -> None:
    """轮询等待 async 谓词为真(谓词内部自行取数)。"""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"等待超时: {message}")


async def _run_status_is(engine: AsyncEngine, run_id: str, status: str) -> bool:
    row = await _get_run(engine, run_id)
    return row is not None and row.status == status


async def _job_status_is(engine: AsyncEngine, job_id: str, status: str) -> bool:
    row = await _get_job(engine, job_id)
    return row is not None and row.status == status


async def _job_phase_is(engine: AsyncEngine, job_id: str, phase: str) -> bool:
    row = await _get_job(engine, job_id)
    return row is not None and row.phase == phase


async def _job_phase_starts_with(
    engine: AsyncEngine, job_id: str, prefix: str
) -> bool:
    """phase 前缀匹配(issue #308:加载期 phase 携带 k/N 计数后缀)。"""

    row = await _get_job(engine, job_id)
    return row is not None and row.phase is not None and row.phase.startswith(prefix)


# ---- 验收 1:worker B 启动不误标 worker A 活跃 run ----------------------------


class TestStartupOwnershipGuard:
    async def test_worker_b_startup_does_not_mislabel_worker_a_run(
        self, engine: AsyncEngine
    ) -> None:
        """A 执行中(job lease 活跃)→ B 启动恢复跳过;A 最终 COMPLETED。"""

        manifest = _manifest("owned")
        run_id = await _queue_double_write(engine, manifest)
        slow_adapter = _SlowPortfolioAdapter(decisions=4, sleep_seconds=0.2)
        worker_a = _build_worker(
            engine,
            adapter_factory=lambda manifest: slow_adapter,
            worker_id="w-a",
        )
        await worker_a._fill_concurrency()
        await _wait_until(
            lambda: _run_status_is(engine, run_id, ResearchRunStatus.RUNNING.value),
            timeout_seconds=5.0,
            message="worker A 未进入 RUNNING",
        )

        # worker B 启动:与 cli worker run 同一条恢复路径(含真实属主探针)。
        await _recover_research_runs(
            session_factory(engine), lease_active_seconds=600.0
        )

        row = await _get_run(engine, run_id)
        assert row is not None
        assert row.status == ResearchRunStatus.RUNNING.value, (
            "worker B 启动误标了 worker A 活跃执行的 run"
        )

        await _drain_worker(worker_a)
        row = await _get_run(engine, run_id)
        assert row is not None
        assert row.status == ResearchRunStatus.COMPLETED.value

    async def test_stale_lease_job_run_is_still_recovered(
        self, engine: AsyncEngine
    ) -> None:
        """job running 但 lease 已过期(worker 崩溃)→ 真孤儿,照旧标 INTERRUPTED。"""

        manifest = _manifest("stale-lease")
        run_id = await _queue_double_write(engine, manifest)
        async with session_factory(engine)() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            await store.transition(
                run_id,
                expected=frozenset({ResearchRunStatus.QUEUED}),
                target=ResearchRunStatus.RUNNING,
            )
            row = await ResearchRunRepository(session).get(run_id)
            assert row is not None
            assert row.job_id is not None
            job_repo = BackgroundJobRepository(session)
            job_row = await job_repo.get(row.job_id, for_update=True)
            assert job_row is not None
            job_row.status = BackgroundJobStatus.RUNNING.value
            job_row.lease_until = datetime.now(UTC) - timedelta(seconds=120)
            job_row.heartbeat_at = datetime.now(UTC) - timedelta(seconds=120)
            await store.checkpoint()

        await _recover_research_runs(
            session_factory(engine), lease_active_seconds=600.0
        )
        run_row = await _get_run(engine, run_id)
        assert run_row is not None
        assert run_row.status == ResearchRunStatus.INTERRUPTED.value

    async def test_run_without_job_is_recovered(self, engine: AsyncEngine) -> None:
        """无关联 job 的 RUNNING run(直连执行残留)照旧标 INTERRUPTED。"""

        manifest = _manifest("no-job")
        async with session_factory(engine)() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            await store.create_or_get(manifest)
            await store.transition(
                manifest.run_id,
                expected=frozenset({ResearchRunStatus.QUEUED}),
                target=ResearchRunStatus.RUNNING,
            )
            await store.checkpoint()

        await _recover_research_runs(
            session_factory(engine), lease_active_seconds=600.0
        )
        run_row = await _get_run(engine, manifest.run_id)
        assert run_row is not None
        assert run_row.status == ResearchRunStatus.INTERRUPTED.value


# ---- 加载期打断 stub(真实信号引擎 + stub 冻结发布)---------------------------


SYMBOLS = ("A.SH", "B.SH", "C.SH", "D.SH", "E.SH", "F.SH")


@dataclass(frozen=True, slots=True)
class _StubBar:
    close: Decimal
    timestamp: datetime | None = None
    # issue #336:next_open 执行价基读取 bar.open;测试桩与 close 相同。
    open: Decimal | None = None


@dataclass(frozen=True, slots=True)
class _StubPointInTimeBar:
    bar: _StubBar
    available_at: datetime


@dataclass(frozen=True, slots=True)
class _StubPointInTimePrice:
    timestamp: datetime
    close: Decimal
    available_at: datetime


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
    period: object = "d1"
    adjustment: str = "qfq"
    start_date: date = date(2024, 1, 1)
    end_date: date = date(2024, 12, 31)
    source: str = "stub"
    version: str = "v1"
    release_checksum: str = "c" * 64
    is_usable: bool = True
    dataset_kind: object | None = ReleaseDatasetKind.BARS


class _SlowStubProvider:
    """带逐次 fetch 延迟的 stub 冻结发布 provider(拉长加载窗口)。"""

    def __init__(
        self,
        release: _StubRelease,
        closes_by_symbol: dict[str, dict[date, Decimal]],
        *,
        fetch_delay_seconds: float = 0.02,
    ) -> None:
        self.release = release
        self.closes_by_symbol = closes_by_symbol
        self._delay = fetch_delay_seconds

    async def _pause(self) -> None:
        await asyncio.sleep(self._delay)

    async def fetch_point_in_time_bars(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        decision_at: datetime,
        adjust: str = "qfq",
    ) -> list[_StubPointInTimeBar]:
        del period, start, decision_at, adjust
        await self._pause()
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if not by_date:
            return []
        return [
            _StubPointInTimeBar(
                _StubBar(
                    close,
                    timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                    open=close,
                ),
                datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            )
            for day, close in sorted(by_date.items())
            if day <= end
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
    ) -> list[_StubPointInTimePrice]:
        del period, start, adjust
        await self._pause()
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if not by_date:
            return []
        return [
            _StubPointInTimePrice(
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                close=close,
                available_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            )
            for day, close in sorted(by_date.items())
            if day <= end
            and datetime.combine(day, datetime.min.time(), tzinfo=UTC) <= decision_at
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
        await self._pause()
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        visible = [
            (day, close)
            for day, close in sorted((by_date or {}).items())
            if day <= end
            and datetime.combine(day, datetime.min.time(), tzinfo=UTC) <= decision_at
        ]
        return CloseHistoryColumns(
            dates=tuple(day for day, _ in visible),
            available_at=tuple(
                datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                for day, _ in visible
            ),
            closes=np.array([float(close) for _, close in visible], dtype=np.float64),
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
    ) -> list[_StubBar]:
        del period, start, adjust
        await self._pause()
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if not by_date:
            return []
        return [
            _StubBar(
                close,
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            )
            for day, close in sorted(by_date.items())
            if day <= end
        ]


def _load_phase_provider() -> _SlowStubProvider:
    """跨 2024-01~2024-08(7 个决策期 → 2 个分块)的价格序列。"""

    days: list[date] = []
    current = date(2024, 1, 1)
    while current <= date(2024, 8, 31):
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    rng = np.random.default_rng(20240801)
    closes: dict[str, dict[date, Decimal]] = {}
    for symbol in SYMBOLS:
        price = 10.0
        closes[symbol] = {}
        for day in days:
            price *= 1.0 + float(rng.normal(0.0008, 0.008))
            closes[symbol][day] = Decimal(str(round(price, 4)))
    return _SlowStubProvider(
        release=_StubRelease(
            "frozen-release-load306",
            tuple(_StubInstrument(code=symbol) for symbol in SYMBOLS),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 8, 31),
        ),
        closes_by_symbol=closes,
    )


def _load_phase_manifest(suffix: str) -> ResearchRunManifest:
    from finboard_backtest.strategy_spec import build_strategy_template
    from finboard_backtest.strategy_spec.contracts import (
        FeatureGraph,
        FeatureKind,
        FeatureNode,
        FeatureOperator,
        SignalAction,
        SignalComparator,
        SignalRule,
        SignalRules,
    )

    spec = build_strategy_template(
        "multi_factor",
        strategy_id=f"load306_{suffix}",
        dataset_release_ids=("frozen-release-load306",),
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
    spec = spec.model_copy(
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
    return ResearchRunManifest(
        run_id=_run_id(f"issue-306-load-{suffix}"),
        idempotency_key=f"issue-306-load-{suffix}",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="frozen-release-load306",
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        parameters={"rebalance_frequency": "monthly"},
        code_version="abcdef0123456789",
        initial_capital=Decimal("200000"),
        requested_by="issue-306-test",
    )


def _load_phase_factory(
    engine: AsyncEngine, provider: _SlowStubProvider
) -> object:
    """真实信号引擎适配器工厂 + 真实 run 打断探针(经 DB 会话)。"""

    def factory(manifest: ResearchRunManifest) -> SignalEnginePipelineAdapter:
        def release_factory(release_id: str) -> _SlowStubProvider:
            assert release_id == "frozen-release-load306"
            return provider

        async def snapshot_provider(snapshot_id: str) -> None:
            del snapshot_id
            return None

        return SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=release_factory,  # type: ignore[arg-type]
            snapshot_provider=snapshot_provider,
            chunk_probe=build_run_interrupt_probe(
                session_factory(engine), manifest.run_id
            ),
        )

    return factory


class TestLoadPhaseInterrupt:
    async def test_run_interrupted_during_load_lands_retry_waiting_then_resumes(
        self, engine: AsyncEngine
    ) -> None:
        """加载期外部标 interrupted → 探针秒级感知 → job retry_waiting → 自愈。

        RR-7a74 僵尸场景的回归用例:旧链路下 run 被标 interrupted 后加载毫无
        感知、心跳续租使 job 永不收敛;本用例锁定「run interrupted / job
        retry_waiting(≤一个轮询周期)→ 自动重试续跑 COMPLETED」全链路。
        """

        manifest = _load_phase_manifest("interrupt")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(
            engine,
            adapter_factory=_load_phase_factory(engine, _load_phase_provider()),
            worker_id="w-load",
        )
        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.job_id is not None
            job_id = run_row.job_id

        await worker._fill_concurrency()
        # 探针在首块边界把 phase 置为 decision_load —— 加载窗口已打开。
        await _wait_until(
            lambda: _job_phase_starts_with(engine, job_id, "research_run:decision_load"),
            timeout_seconds=10.0,
            message="加载期探针未上报 decision_load 阶段",
        )
        # 外部打断(模拟误标 / 运维介入):run → INTERRUPTED。
        async with session_factory(engine)() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            await store.transition(
                run_id,
                expected=frozenset({ResearchRunStatus.RUNNING}),
                target=ResearchRunStatus.INTERRUPTED,
                error_code="process_restart",
            )
            await store.checkpoint()

        await _wait_until(
            lambda: _job_status_is(
                engine, job_id, BackgroundJobStatus.RETRY_WAITING.value
            ),
            timeout_seconds=10.0,
            message="加载期打断后 job 未秒级转 retry_waiting",
        )
        run_row = await _get_run(engine, run_id)
        assert run_row is not None
        assert run_row.status == ResearchRunStatus.INTERRUPTED.value
        job_row = await _get_job(engine, job_id)
        assert job_row is not None
        assert job_row.error_code == "interrupted"

        # 自动重试续跑:retry_waiting → requeue → 重新领取 → 探针见 RUNNING
        # → 加载照常 → COMPLETED(coordinator 接受 INTERRUPTED 起始态)。
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.requeue_interrupted(job_id)
            await repo.checkpoint()
        await _drain_worker(worker)

        run_row = await _get_run(engine, run_id)
        assert run_row is not None
        assert run_row.status == ResearchRunStatus.COMPLETED.value, (
            f"error={run_row.error_code} {run_row.error_summary}"
        )
        job_row = await _get_job(engine, job_id)
        assert job_row is not None
        assert job_row.status == BackgroundJobStatus.SUCCEEDED.value

    async def test_job_cancel_during_load_promptly_cancels(
        self, engine: AsyncEngine
    ) -> None:
        """加载期 request_cancel → 探针感知 → run INTERRUPTED + job 秒级 cancelled。

        协作式取消在 progress 回调处中止执行器;worker 将其立即收口为
        cancelled 终态(不再把孤儿 cancel_requested 行留给 lease 回收后被
        reclaim+requeue 原样重放)。run 落在可恢复的 INTERRUPTED(#305 通道)。
        """

        manifest = _load_phase_manifest("cancel")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(
            engine,
            adapter_factory=_load_phase_factory(engine, _load_phase_provider()),
            worker_id="w-cancel",
        )
        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.job_id is not None
            job_id = run_row.job_id

        await worker._fill_concurrency()
        await _wait_until(
            lambda: _job_phase_starts_with(engine, job_id, "research_run:decision_load"),
            timeout_seconds=10.0,
            message="加载窗口未打开",
        )
        async with session_factory(engine)() as session:
            repo = BackgroundJobRepository(session)
            await repo.request_cancel(job_id)
            await repo.checkpoint()

        await _wait_until(
            lambda: _job_status_is(
                engine, job_id, BackgroundJobStatus.CANCELLED.value
            ),
            timeout_seconds=10.0,
            message="加载期取消未秒级收口 cancelled",
        )
        run_row = await _get_run(engine, run_id)
        assert run_row is not None
        # run 落在可恢复的 INTERRUPTED(replay 通道),而非悬挂 RUNNING。
        assert run_row.status == ResearchRunStatus.INTERRUPTED.value
        await _drain_worker(worker)


# ---- 验收 3:僵尸无进展检测 ----------------------------------------------------


class _StallExecutor:
    """只心跳不推进的执行器(模拟卡死的 execute)。"""

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        del progress
        await asyncio.sleep(30)
        return JobResult(status="succeeded")  # pragma: no cover


class _SteppingExecutor:
    """逐步推进进度的健康执行器(对照:不误杀)。"""

    async def execute(self, job: JobRecord, progress: ProgressCallback) -> JobResult:
        steps = 4
        for index in range(steps):
            await asyncio.sleep(0.15)
            await progress(index, steps, f"stepping:{index}")
        await progress(steps, steps, "stepping:done")
        return JobResult(status="succeeded", result_ref="stepped")


def _zombie_worker(engine: AsyncEngine, *, threshold: float) -> BackgroundWorker:
    registry = JobExecutorRegistry()
    registry.register("stall_kind_306", _StallExecutor())
    registry.register("stepping_kind_306", _SteppingExecutor())
    return BackgroundWorker(
        engine=engine,
        session_maker=session_factory(engine),
        registry=registry,
        config=WorkerConfig(
            worker_id="w-zombie",
            poll_interval_seconds=0.05,
            max_concurrent=2,
            lease_timeout_seconds=30.0,
            heartbeat_interval_seconds=0.05,
            zombie_no_progress_seconds=threshold,
            retry_backoff_seconds=30.0,
        ),
    )


async def _enqueue_simple(
    engine: AsyncEngine, *, kind: str, payload: dict[str, object]
) -> str:
    async with session_factory(engine)() as session:
        repo = BackgroundJobRepository(session)
        job_id = generate_background_job_id()
        checksum = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        await repo.create_or_get(
            job_id=job_id,
            idempotency_key=f"idem-{job_id}",
            kind=kind,
            queue="default",
            status=BackgroundJobStatus.QUEUED.value,
            priority=0,
            payload=payload,
            payload_checksum=checksum,
            max_attempts=3,
            requested_by="issue-306-test",
        )
        await repo.checkpoint()
        return job_id


class TestZombieNoProgressDetection:
    async def test_stalled_heartbeat_alive_job_is_killed_and_heart_stops_renewing(
        self, engine: AsyncEngine
    ) -> None:
        """heartbeat 在续但进度不动 → 具名失败 zombie_no_progress + retry_waiting;
        原属主心跳停止续租(lease 冻结),重试语义交给 requeue_due。"""

        job_id = await _enqueue_simple(
            engine, kind="stall_kind_306", payload={"n": 1}
        )
        worker = _zombie_worker(engine, threshold=0.4)
        await worker._fill_concurrency()
        await _wait_until(
            lambda: _job_status_is(engine, job_id, BackgroundJobStatus.RUNNING.value),
            timeout_seconds=5.0,
            message="任务未被领取",
        )

        # 第一轮维护:建立基准,不判僵尸。
        await worker._maintenance()
        job_row = await _get_job(engine, job_id)
        assert job_row is not None
        assert job_row.status == BackgroundJobStatus.RUNNING.value

        # 等待超过阈值(期间心跳持续续租、进度零变化),再跑一轮维护。
        await asyncio.sleep(0.7)
        await worker._maintenance()

        job_row = await _get_job(engine, job_id)
        assert job_row is not None
        assert job_row.status == BackgroundJobStatus.RETRY_WAITING.value
        assert job_row.error_code == "zombie_no_progress"
        assert job_row.error_summary is not None
        assert "无变化" in job_row.error_summary

        # 原属主心跳已停止续租:lease_until 不再被推远。
        lease_frozen = job_row.lease_until
        assert lease_frozen is not None
        await asyncio.sleep(0.4)
        job_row = await _get_job(engine, job_id)
        assert job_row is not None
        assert job_row.lease_until == lease_frozen

        # 收尾:停掉 stall 任务(shutdown 语义 —— 不覆盖 retry_waiting 终态)。
        for task in list(worker._inflight):
            task.cancel()
        await asyncio.gather(*worker._inflight, return_exceptions=True)
        job_row = await _get_job(engine, job_id)
        assert job_row is not None
        assert job_row.status == BackgroundJobStatus.RETRY_WAITING.value

    async def test_progressing_job_is_not_killed(self, engine: AsyncEngine) -> None:
        """对照:进度持续推进的健康 job(阈值同样紧张)不被误杀。"""

        job_id = await _enqueue_simple(
            engine, kind="stepping_kind_306", payload={"n": 1}
        )
        worker = _zombie_worker(engine, threshold=0.4)
        await worker._fill_concurrency()
        # 维护与执行交错:指纹每步都变,任何一轮都不满足「超阈值无变化」。
        for _ in range(6):
            await asyncio.sleep(0.1)
            await worker._maintenance()
        await _drain_worker(worker)

        job_row = await _get_job(engine, job_id)
        assert job_row is not None
        assert job_row.status == BackgroundJobStatus.SUCCEEDED.value

    async def test_disabled_when_threshold_zero(self, engine: AsyncEngine) -> None:
        """threshold=0(默认关闭路径的显式形态)不做任何僵尸判定。"""

        job_id = await _enqueue_simple(
            engine, kind="stall_kind_306", payload={"n": 1}
        )
        worker = _zombie_worker(engine, threshold=0.0)
        await worker._fill_concurrency()
        await asyncio.sleep(0.2)
        await worker._maintenance()
        await asyncio.sleep(0.5)
        await worker._maintenance()
        job_row = await _get_job(engine, job_id)
        assert job_row is not None
        assert job_row.status == BackgroundJobStatus.RUNNING.value
        for task in list(worker._inflight):
            task.cancel()
        await asyncio.gather(*worker._inflight, return_exceptions=True)


# ---- 验收 4:job 视图透传 run_status ------------------------------------------


class TestJobViewRunStatus:
    async def test_rest_job_get_exposes_run_status(self, engine: AsyncEngine) -> None:
        """GET /api/jobs/{id}:research_run job 附 run_status;不一致一眼可见。"""

        from finboard_api.routes.jobs import get_job

        manifest = _manifest("view")
        run_id = await _queue_double_write(engine, manifest)
        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.job_id is not None
            job_id = run_row.job_id

        async with session_factory(engine)() as session:
            out = await get_job(job_id, session)
        assert out.run_status == ResearchRunStatus.QUEUED.value

        # run 被外部标 interrupted 而 job 仍 running —— 两表不一致在单查可见。
        async with session_factory(engine)() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            await store.transition(
                run_id,
                expected=frozenset({ResearchRunStatus.QUEUED}),
                target=ResearchRunStatus.RUNNING,
            )
            await store.transition(
                run_id,
                expected=frozenset({ResearchRunStatus.RUNNING}),
                target=ResearchRunStatus.INTERRUPTED,
                error_code="process_restart",
            )
            await store.checkpoint()
        async with session_factory(engine)() as session:
            out = await get_job(job_id, session)
        assert out.status == BackgroundJobStatus.QUEUED.value
        assert out.run_status == ResearchRunStatus.INTERRUPTED.value

        # 非 research_run kind:run_status 为 None。
        echo_id = await _enqueue_simple(engine, kind="echo", payload={"n": 1})
        async with session_factory(engine)() as session:
            out = await get_job(echo_id, session)
        assert out.run_status is None

    async def test_rest_list_jobs_leaves_run_status_none(
        self, engine: AsyncEngine
    ) -> None:
        """列表端点不 join(范围外):run_status 恒 None,单查才有。"""

        from finboard_api.routes.jobs import list_jobs

        manifest = _manifest("view-list")
        await _queue_double_write(engine, manifest)
        async with session_factory(engine)() as session:
            rows = await list_jobs(
                kind=None,
                status=None,
                queue=None,
                limit=10,
                archived="exclude",
                session=session,
            )
        assert rows
        assert all(item.run_status is None for item in rows)
