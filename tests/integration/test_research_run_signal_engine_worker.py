"""ResearchRun 信号引擎 worker 集成测试(issue #170)。

验证 multi_factor 已发布规格经真实信号引擎路径端到端执行:

* queue(双写) → worker → ``SignalEnginePipelineAdapter``(stub 冻结发布 +
  内存快照) → Coordinator 全链路 → COMPLETED + 14 stage artifacts;
* 非 multi_factor kind 明确报 not_implemented,且 ``research_runs`` 状态同步
  FAILED(伴生缺陷 A:失败原因在 run 记录上可见);
* 冻结快照缺失时 fail-closed,run 状态 FAILED 且带可查询错误码。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``)。不连 broker / 不下实盘单。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_backtest.background_jobs import JobExecutorRegistry
from finboard_backtest.background_jobs.executors import ResearchRunExecutor
from finboard_backtest.background_jobs.executors.research_run import (
    default_store_factory,
)
from finboard_backtest.background_jobs.worker import (
    BackgroundWorker,
    WorkerConfig,
)
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
    ResearchStrategySpec,
    SignalAction,
    SignalComparator,
    SignalConflictPolicy,
    SignalRule,
    SignalRules,
)
from finboard_data.factor_lab import FeatureObservation
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
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM research_run_artifacts"))
        await conn.execute(text("DELETE FROM research_runs"))
        await conn.execute(text("DELETE FROM background_jobs"))


# ---- stub 冻结发布 / 快照(与 test_frozen_loader.py 风格一致) -----------------


@dataclass(frozen=True, slots=True)
class _StubBar:
    close: Decimal
    timestamp: datetime | None = None


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
    # 注意:名称不得含 "ST" 子串(大写后),否则被 ST 过滤排除(issue #213)。
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


@dataclass
class _StubProvider:
    release: _StubRelease
    closes_by_symbol: dict[str, dict[date, Decimal]]

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
        """返回请求结束日及之前的全部 PIT bars(时间升序)。"""
        del period, start, decision_at, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if not by_date:
            return []
        return [
            _StubPointInTimeBar(
                _StubBar(
                    close,
                    timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
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
        """价格特征所需的轻量 PIT 收盘价视图(多期回放每期重算 features 用)。"""
        del period, start, adjust
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
            if day <= end and datetime.combine(day, datetime.min.time(), tzinfo=UTC) <= decision_at
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
        """非 PIT 全量 bars(交易日历推断用),返回带 timestamp 的 Bar 形状。"""
        del period, start, adjust
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


@dataclass
class _StubSnapshot:
    snapshot_id: str
    decision_at: datetime
    observations: tuple[FeatureObservation, ...]


def _obs(symbol: str, feature_name: str, value: float) -> FeatureObservation:
    at = datetime(2023, 12, 29, tzinfo=UTC)
    return FeatureObservation(
        symbol=symbol,
        feature_name=feature_name,
        value=value,
        observed_at=at,
        available_at=at,
        source="frozen-release-v1",
        source_version="v1",
    )


SYMBOLS = ("A.SH", "B.SH", "C.SH", "D.SH", "E.SH", "F.SH")
DECISION_AT = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)


def _make_provider() -> _StubProvider:
    # 各标的价格轨迹独立(固定 seed 随机游走,样本协方差满秩)。
    import numpy as np

    days = [
        date(2023, 12, 14),
        date(2023, 12, 15),
        date(2023, 12, 18),
        date(2023, 12, 19),
        date(2023, 12, 20),
        date(2023, 12, 21),
        date(2023, 12, 22),
        date(2023, 12, 25),
        date(2023, 12, 26),
        date(2023, 12, 27),
        date(2023, 12, 28),
        date(2023, 12, 29),
        date(2024, 1, 2),
        date(2024, 1, 3),
    ]
    rng = np.random.default_rng(20240101)
    closes: dict[str, dict[date, Decimal]] = {}
    for symbol in SYMBOLS:
        price = 10.0
        closes[symbol] = {}
        for day in days:
            price *= 1.0 + float(rng.normal(0.001, 0.01))
            closes[symbol][day] = Decimal(str(round(price, 4)))
    return _StubProvider(
        release=_StubRelease(
            "frozen-release-v1",
            tuple(_StubInstrument(code=symbol) for symbol in SYMBOLS),
        ),
        closes_by_symbol=closes,
    )


def _make_snapshot(
    *,
    pb: tuple[float, ...] = (1.5, 1.2, 0.8, 1.4, 1.1, 0.7),
    momentum: tuple[float, ...] = (0.05, 0.15, 0.25, 0.10, 0.20, 0.30),
    volatility: tuple[float, ...] = (0.3, 0.2, 0.1, 0.28, 0.18, 0.08),
) -> _StubSnapshot:
    observations: list[FeatureObservation] = []
    for index, symbol in enumerate(SYMBOLS):
        observations.append(_obs(symbol, "pb", pb[index]))
        observations.append(_obs(symbol, "momentum", momentum[index]))
        observations.append(_obs(symbol, "volatility_20d", volatility[index]))
    return _StubSnapshot(
        snapshot_id="factor-v1",
        decision_at=DECISION_AT,
        observations=tuple(observations),
    )


def _run_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"RR-{digest}"


def _manifest(kind: str, suffix: str) -> ResearchRunManifest:
    spec = _signal_spec(kind, suffix)
    return ResearchRunManifest(
        run_id=_run_id(f"signal-engine-{suffix}"),
        idempotency_key=f"signal-engine-{suffix}",
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
        factor_snapshots=(
            FrozenArtifactRef(
                artifact_id="factor-v1",
                version="v1",
                checksum="b" * 64,
                capabilities=("factor:momentum",),
            ),
        ),
        code_version="abcdef0123456789",
        initial_capital=Decimal("200000"),
        requested_by="worker-test",
    )


def _signal_spec(kind: str, suffix: str) -> ResearchStrategySpec:
    """multi_factor 规格:排名阈值放宽,保证多头信号数满足风险贡献约束。

    模板默认 rank_top 0.2 在 6 标的中命中 2 个多头,单资产风险贡献
    1/2=0.5 > 0.35 数学不可行;放宽到 0.5 命中 3 个多头(1/3=0.333 < 0.35)。
    """
    spec = build_strategy_template(
        kind,
        strategy_id=f"{kind}_worker_{suffix}",
        dataset_release_ids=("frozen-release-v1",),
    )
    if kind != "multi_factor":
        return spec
    return spec.model_copy(
        update={
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
                    SignalRule(
                        rule_id="bottom_score_sell",
                        feature_id="composite",
                        comparator=SignalComparator.RANK_BOTTOM,
                        threshold=0.5,
                        action=SignalAction.SELL,
                        priority=10,
                        rationale="复合得分落入后 50% 时退出已有多头。",
                    ),
                ),
                conflict_policy=SignalConflictPolicy.HIGHEST_PRIORITY,
            ),
        }
    )


def _signal_engine_factory(
    provider: _StubProvider,
    snapshot: _StubSnapshot | None,
) -> object:
    """注入 stub 冻结产物 / 快照的真实信号引擎适配器工厂。"""

    def factory(manifest: ResearchRunManifest) -> SignalEnginePipelineAdapter:
        def release_factory(release_id: str) -> _StubProvider:
            assert release_id == "frozen-release-v1"
            return provider

        async def snapshot_provider(snapshot_id: str) -> _StubSnapshot | None:
            assert snapshot_id == "factor-v1"
            return snapshot

        return SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=release_factory,  # type: ignore[arg-type]
            snapshot_provider=snapshot_provider,  # type: ignore[arg-type]
        )

    return factory


async def _queue_double_write(
    engine: AsyncEngine,
    manifest: ResearchRunManifest,
) -> str:
    import json

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


def _build_worker(engine: AsyncEngine, adapter_factory: object) -> BackgroundWorker:
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
            worker_id="w-signal-engine",
            poll_interval_seconds=0.05,
            max_concurrent=2,
            lease_timeout_seconds=60,
            heartbeat_interval_seconds=10.0,
        ),
    )


async def _drain_worker(worker: BackgroundWorker) -> None:
    import asyncio

    await worker._fill_concurrency()
    for _ in range(400):
        if not worker._inflight:
            break
        await asyncio.sleep(0.02)
    assert not worker._inflight


# ---- tests ------------------------------------------------------------------


class TestSignalEngineWorkerEndToEnd:
    async def test_multi_factor_run_completes_with_artifacts(self, engine: AsyncEngine) -> None:
        """multi_factor 规格:真实信号引擎路径端到端 → COMPLETED + 14 artifacts。"""
        manifest = _manifest("multi_factor", "e2e")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(engine, _signal_engine_factory(_make_provider(), _make_snapshot()))
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value, (
                f"status={run_row.status} error_code={run_row.error_code} "
                f"error_summary={run_row.error_summary}"
            )
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            assert job_row.status == BackgroundJobStatus.SUCCEEDED.value
            assert job_row.result_ref == run_id

            artifacts = await ResearchRunRepository(session).list_artifacts(run_id)
        # 14 stage artifacts:13 个逐决策 stage + report。
        stages = {item.stage for item in artifacts}
        from finboard_backtest.research_run import ResearchRunStage

        assert stages == {
            ResearchRunStage.UNIVERSE.value,
            ResearchRunStage.FEATURES.value,
            ResearchRunStage.SIGNALS.value,
            ResearchRunStage.TARGETS_BEFORE_CONSTRAINTS.value,
            ResearchRunStage.CONSTRAINTS.value,
            ResearchRunStage.TARGETS_AFTER_CONSTRAINTS.value,
            ResearchRunStage.RISK_EXITS.value,
            ResearchRunStage.TARGETS_AFTER_RISK.value,
            ResearchRunStage.CAPITAL_FEASIBILITY.value,
            ResearchRunStage.REBALANCE_PLAN.value,
            ResearchRunStage.ORDERS.value,
            ResearchRunStage.FILLS.value,
            ResearchRunStage.LEDGER.value,
            ResearchRunStage.REPORT.value,
        }
        assert len(artifacts) == 14

    async def test_unsupported_kind_fails_with_run_error(self, engine: AsyncEngine) -> None:
        """非 multi_factor kind:not_implemented,且 research_runs 同步 FAILED。"""
        manifest = _manifest("etf_rotation", "unsupported")
        run_id = await _queue_double_write(engine, manifest)

        from finboard_backtest.research_run.signal_engine import (
            build_signal_engine_adapter_factory,
        )
        from finboard_persistence import session_factory as sf

        worker = _build_worker(
            engine,
            build_signal_engine_adapter_factory(sf(engine)),
        )
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            # 伴生缺陷 A 修复:run 状态反映失败,不停留在 QUEUED。
            assert run_row.status == ResearchRunStatus.FAILED.value
            assert run_row.error_code == "signal_engine_not_implemented"
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            assert job_row.status == BackgroundJobStatus.FAILED.value
            assert job_row.error_code == "signal_engine_not_implemented"

    async def test_missing_snapshot_fails_run_visibly(self, engine: AsyncEngine) -> None:
        """冻结快照缺失:fail-closed,run 状态 FAILED 且错误原因可查询。"""
        manifest = _manifest("multi_factor", "missing-snapshot")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(engine, _signal_engine_factory(_make_provider(), None))
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.FAILED.value
            assert "因子快照缺失" in (run_row.error_summary or "")
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            assert job_row.status == BackgroundJobStatus.FAILED.value


# ---- 多期再平衡(issue #183)----------------------------------------------------


def _multi_period_calendar(start: date, end: date) -> list[date]:
    """周内连续交易日 stub 日历(跳过周末),缩样区间跨 3 个月。"""
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _multi_period_provider() -> _StubProvider:
    """跨 2024-01~2024-04 的价格序列(固定 seed 独立随机游走,协方差满秩)。"""
    import numpy as np

    days = _multi_period_calendar(date(2024, 1, 1), date(2024, 4, 30))
    rng = np.random.default_rng(20240401)
    closes: dict[str, dict[date, Decimal]] = {}
    for symbol in SYMBOLS:
        price = 10.0
        closes[symbol] = {}
        for day in days:
            price *= 1.0 + float(rng.normal(0.0008, 0.008))
            closes[symbol][day] = Decimal(str(round(price, 4)))
    return _StubProvider(
        release=_StubRelease(
            "frozen-release-multi",
            tuple(_StubInstrument(code=symbol) for symbol in SYMBOLS),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 4, 30),
        ),
        closes_by_symbol=closes,
    )


def _price_only_spec(suffix: str) -> ResearchStrategySpec:
    """价格因子专用 multi_factor 规格(momentum + volatility_20d)。

    多期回放由管线从冻结发布重算价格因子,不要求预建每月因子快照。
    """
    from finboard_backtest.strategy_spec.contracts import (
        FeatureGraph,
        FeatureKind,
        FeatureNode,
        FeatureOperator,
    )

    spec = build_strategy_template(
        "multi_factor",
        strategy_id=f"multi_period_{suffix}",
        dataset_release_ids=("frozen-release-multi",),
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


def _multi_period_manifest(suffix: str) -> ResearchRunManifest:
    spec = _price_only_spec(suffix)
    return ResearchRunManifest(
        run_id=_run_id(f"signal-engine-multi-{suffix}"),
        idempotency_key=f"signal-engine-multi-{suffix}",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="frozen-release-multi",
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        parameters={"rebalance_frequency": "monthly"},
        code_version="abcdef0123456789",
        initial_capital=Decimal("200000"),
        requested_by="worker-test",
    )


def _multi_period_factory(provider: _StubProvider) -> object:
    """注入 stub 冻结发布(无因子快照)的信号引擎适配器工厂。"""

    def factory(manifest: ResearchRunManifest) -> SignalEnginePipelineAdapter:
        def release_factory(release_id: str) -> _StubProvider:
            assert release_id == "frozen-release-multi"
            return provider

        async def snapshot_provider(snapshot_id: str) -> None:
            return None

        return SignalEnginePipelineAdapter(
            manifest=manifest,
            release_provider_factory=release_factory,  # type: ignore[arg-type]
            snapshot_provider=snapshot_provider,
        )

    return factory


class TestMultiPeriodWorkerEndToEnd:
    async def test_monthly_rebalance_completes_with_equity_curve(self, engine: AsyncEngine) -> None:
        """monthly 再平衡:多期决策 + COMPLETED + 每日权益曲线 + 指标。"""
        manifest = _multi_period_manifest("monthly")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(engine, _multi_period_factory(_multi_period_provider()))
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value, (
                f"status={run_row.status} error_code={run_row.error_code} "
                f"error_summary={run_row.error_summary}"
            )
            result = run_row.result
            assert result is not None
            # 多期:2024-01~2024-04 共 4 个月,发布末日(4 月末)无下一成交日剔除。
            assert result["execution_mode"] == "multi_period"
            assert result["decision_count"] == 3
            assert result["annualized_return"] != 0.0
            equity_curve = cast(
                list[dict[str, object]], result["equity_curve"]
            )
            assert equity_curve
            # 覆盖发布全部交易日(1 月 1 日至 4 月 30 日,周末除外)。
            expected_days = _multi_period_calendar(date(2024, 1, 1), date(2024, 4, 30))
            assert [item["trade_date"] for item in equity_curve] == [
                day.isoformat() for day in expected_days
            ]
            assert all(item["equity"] for item in equity_curve)
            # 期末权益 = 曲线最后一点;总收益 > 0(价格序列整体上行)。
            assert result["final_equity"] == equity_curve[-1]["equity"]
            assert float(str(result["strategy_return"])) > 0

    async def test_mid_series_load_failure_summary_carries_stage_decision_release(
        self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """issue #263:multi_period 第 2 期缺数据失败,error_summary 自带定位上下文。

        故障注入:loader 在 2024-02-29(第 2 期决策日)抛裸 RuntimeError。
        全链路(信号引擎标记 → runner 通用收口 → research_runs → background_jobs)
        失败摘要头部含 stage / 决策日期 / bars 发布标识,不必翻 artifacts 定位。
        """
        from finboard_backtest.research_run.frozen_loader import (
            FrozenInputLoader,
            LoadedDecisionContext,
        )

        manifest = _multi_period_manifest("fail-context")
        real_load_context = FrozenInputLoader.load_context

        async def failing_second_period(
            self: FrozenInputLoader,
            manifest_arg: ResearchRunManifest,
            *,
            decision_at: datetime,
            execution_at: datetime,
        ) -> LoadedDecisionContext:
            if decision_at.date() == date(2024, 2, 29):
                raise RuntimeError("中期数据缺失:2 月发布 bars 段损坏")
            return await real_load_context(
                self,
                manifest_arg,
                decision_at=decision_at,
                execution_at=execution_at,
            )

        monkeypatch.setattr(
            FrozenInputLoader, "load_context", failing_second_period
        )

        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(engine, _multi_period_factory(_multi_period_provider()))
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.FAILED.value
            assert run_row.error_code == "RuntimeError"
            summary = run_row.error_summary or ""
            # 头部结构化上下文:stage + 失败期次精确决策日 + bars 主发布。
            assert summary.startswith(
                "[stage=decision_load; decision=2024-02-29; "
                "release=frozen-release-multi; dataset_releases=frozen-release-multi]"
            )
            # 原始根因消息保留在尾部。
            assert summary.endswith("中期数据缺失:2 月发布 bars 段损坏")
            # 决策总数 0:失败发生在任何工件落库之前(纯可观测性增强点)。
            assert await ResearchRunRepository(session).list_artifacts(run_id) == []
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            assert job_row.status == BackgroundJobStatus.FAILED.value
            assert job_row.error_summary == summary


def _benchmark_provider() -> _StubProvider:
    """multi-period 发布 + 000300.SH 指数基准(直线 10 → 15,+50%)。

    universe 随机游走几乎持平,基准强涨,超额收益应为负、方向确定。
    """
    days = _multi_period_calendar(date(2024, 1, 1), date(2024, 4, 30))
    provider = _multi_period_provider()
    # 指数直线上行 10 → 15(+50%)。
    start, end = Decimal("10"), Decimal("15")
    total = len(days) - 1
    provider.closes_by_symbol["000300.SH"] = {
        day: start + (end - start) * Decimal(index) / Decimal(max(total, 1))
        for index, day in enumerate(days)
    }
    return provider


def _benchmark_manifest(suffix: str, symbol: str) -> ResearchRunManifest:
    from dataclasses import replace

    return replace(
        _multi_period_manifest(suffix),
        benchmark_config={"symbol": symbol},
    )


class TestMultiPeriodBenchmarkEndToEnd:
    async def test_index_benchmark_return_computed_and_sign_correct(
        self, engine: AsyncEngine
    ) -> None:
        """000300.SH 指数基准:benchmark_return 来自真实行情,超额方向正确。"""
        manifest = _benchmark_manifest("bench-hs300", "000300.SH")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(engine, _multi_period_factory(_benchmark_provider()))
        await _drain_worker(worker)

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
            # 基准:买入持有 000300.SH(10 → 15,+50%)。
            assert bm == pytest.approx(0.5, abs=1e-6)
            # 超额 = 策略 - 基准;universe 随机游走接近持平,基准强涨 → 超额为负。
            assert ex == pytest.approx(st - bm, abs=1e-9)
            assert ex < 0
            # 报告展示字段记录基准标的。
            assert result["benchmark_symbol"] == "000300.SH"

    async def test_missing_benchmark_symbol_returns_null(self, engine: AsyncEngine) -> None:
        """基准标的在发布中缺失:run 仍 COMPLETED,benchmark_return 为 null。"""
        manifest = _benchmark_manifest("bench-missing", "399006.SZ")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(engine, _multi_period_factory(_benchmark_provider()))
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value, (
                f"status={run_row.status} error_code={run_row.error_code} "
                f"error_summary={run_row.error_summary}"
            )
            result = run_row.result
            assert result is not None
            assert result["benchmark_return"] is None
            assert result["excess_return"] is None


# ---- universe 预检运行时根因(issue #186)-----------------------------------


def _null_list_date_provider() -> _StubProvider:
    """多期发布但你发布 instruments 的 list_date 全 null(模拟 #185 前的数据)。"""
    provider = _multi_period_provider()
    provider.release.instruments = tuple(
        replace(item, list_date=None)
        for item in provider.release.instruments
    )
    return provider


class TestUniversePrecheckRuntimeRootCause:
    async def test_null_list_date_fails_run_with_field_root_cause(
        self, engine: AsyncEngine
    ) -> None:
        """list_date 全 null + min_listing_days(默认 60):执行期空池错误指向 list_date。

        验收:执行期空池错误附根因(缺失字段名),不再只有泛化
        「组合流水线输入必须包含候选池和标准化信号」。
        """
        manifest = _multi_period_manifest("rt-empty-null-list")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(
            engine, _multi_period_factory(_null_list_date_provider())
        )
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.FAILED.value
            summary = run_row.error_summary or ""
            assert "候选池为空" in summary, summary
            assert "list_date" in summary, summary
            assert "listing_age_below_minimum" in summary, summary
            assert "组合流水线输入必须包含候选池" not in summary
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            assert job_row.status == BackgroundJobStatus.FAILED.value
