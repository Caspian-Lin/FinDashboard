"""ResearchRun worker 集成测试(issue #143)。

验证 research_run 迁移到统一队列后的端到端链路:

* queue(双写 research_runs + background_jobs) → worker 消费 → 两表同步推进到
  completed/succeeded;
* 双写幂等(同 idempotency_key 不重复创建);
* 取消链路(cancel research_run → background_job cancel_requested/cancelled);
* worker 中断 → 恢复 → 续跑。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``,回退 ``FINBOARD_DB_URL``)。不连 broker / 不下实盘单。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.background_jobs import JobExecutorRegistry
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
    ResearchRunCoordinator,
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
from finboard_backtest.strategy_spec import build_strategy_template
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

pytestmark = pytest.mark.asyncio


# ---- engine fixture(与 test_background_job_persistence.py 一致) -------------


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
    """每个测试前清空 background_jobs + research_runs(+ artifacts)。"""
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM research_run_artifacts"))
        await conn.execute(text("DELETE FROM research_runs"))
        await conn.execute(text("DELETE FROM background_jobs"))


# ---- manifest / adapter builders -------------------------------------------


def _run_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"RR-{digest}"


def _manifest(suffix: str) -> ResearchRunManifest:
    spec = build_strategy_template(
        "ma_cross",
        strategy_id=f"ma_cross_worker_{suffix}",
        dataset_release_ids=("frozen-release-v1",),
    )
    return ResearchRunManifest(
        run_id=_run_id(f"worker-test-{suffix}"),
        idempotency_key=f"worker-test-{suffix}",
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
        requested_by="worker-test",
    )


def _portfolio_adapter() -> PortfolioPipelineAdapter:
    symbols = ("A.SH", "B.SH", "C.SH")
    decision_at = datetime(2024, 1, 2, 15, tzinfo=UTC)
    return PortfolioPipelineAdapter(
        strategy_kind="ma_cross",
        decision_inputs=(
            PortfolioDecisionInput(
                business_date=date(2024, 1, 2),
                decision_at=decision_at,
                execution_at=datetime(2024, 1, 3, 9, 30, tzinfo=UTC),
                candidates=tuple(
                    UniverseCandidate(
                        symbol=symbol,
                        included=True,
                        reasons=("集成测试候选池通过",),
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
                        rule_id="integration-signal",
                        rationale="集成测试冻结信号",
                    )
                    for symbol in symbols
                ),
                prices=dict.fromkeys(symbols, 10.0),
                execution_prices=dict.fromkeys(symbols, 10.0),
                lot_info={
                    symbol: AssetLotInfo(code=symbol, lot_size=100)
                    for symbol in symbols
                },
                input_artifact_ids=("frozen-release-v1",),
                covariance=CovarianceEstimate(
                    matrix=np.diag([0.01, 0.01, 0.01]),
                    tickers=list(symbols),
                    shrinkage=0.0,
                    n_observations=252,
                ),
                sleeve_map=dict.fromkeys(symbols, "equity"),
            ),
        ),
    )


def _make_adapter_factory() -> Callable[[ResearchRunManifest], PortfolioPipelineAdapter]:
    """固定样本 adapter 工厂(注入到 ResearchRunExecutor)。"""

    def factory(manifest: ResearchRunManifest) -> PortfolioPipelineAdapter:
        del manifest  # 固定样本,不依赖 manifest 内容
        return _portfolio_adapter()

    return factory


class _SlowPortfolioAdapter(PortfolioPipelineAdapter):
    """固定样本慢速 adapter:两个决策之间 sleep,给集成测试留出观察窗口。"""

    async def decisions(
        self,
        manifest: ResearchRunManifest,
    ) -> AsyncIterator[DecisionBundle]:
        index = 0
        async for decision in super().decisions(manifest):
            if index > 0:
                await asyncio.sleep(0.3)
            index += 1
            yield decision


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
                reasons=("集成测试候选池通过",),
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
                rule_id="integration-signal",
                rationale="集成测试冻结信号",
            )
            for symbol in symbols
        ),
        prices=dict.fromkeys(symbols, 10.0),
        execution_prices=dict.fromkeys(symbols, 10.0),
        lot_info={symbol: AssetLotInfo(code=symbol, lot_size=100) for symbol in symbols},
        input_artifact_ids=("frozen-release-v1",),
        covariance=CovarianceEstimate(
            matrix=np.diag([0.01, 0.01, 0.01]),
            tickers=list(symbols),
            shrinkage=0.0,
            n_observations=252,
        ),
        sleeve_map=dict.fromkeys(symbols, "equity"),
    )


def _make_slow_adapter_factory() -> Callable[[ResearchRunManifest], PortfolioPipelineAdapter]:
    """两个决策、决策间带 sleep 的慢速 adapter 工厂(issue #188)。"""

    def factory(manifest: ResearchRunManifest) -> PortfolioPipelineAdapter:
        del manifest
        return _SlowPortfolioAdapter(
            strategy_kind="ma_cross",
            decision_inputs=(_portfolio_input_at(0), _portfolio_input_at(1)),
        )

    return factory


# ---- helpers ----------------------------------------------------------------


async def _queue_double_write(
    engine: AsyncEngine,
    manifest: ResearchRunManifest,
) -> str:
    """模拟 queue_research_run 路由的双写,返回 run_id。

    research_runs 与 background_jobs 共用 idempotency_key,同事务提交。
    """
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


def _build_worker(
    engine: AsyncEngine,
    *,
    adapter_factory: Callable[[ResearchRunManifest], ResearchStrategyAdapter] | None = None,
) -> BackgroundWorker:
    registry = JobExecutorRegistry()
    registry.register(
        "research_run",
        ResearchRunExecutor(
            session_maker=session_factory(engine),
            store_factory=default_store_factory,
            adapter_factory=adapter_factory or _make_adapter_factory(),
        ),
    )
    return BackgroundWorker(
        engine=engine,
        session_maker=session_factory(engine),
        registry=registry,
        config=WorkerConfig(
            worker_id="w-test",
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


class TestResearchRunWorkerEndToEnd:
    async def test_queue_then_worker_completes_double_write(
        self, engine: AsyncEngine
    ) -> None:
        manifest = _manifest("e2e")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(engine)
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            assert job_row.status == BackgroundJobStatus.SUCCEEDED.value
            assert job_row.result_ref == run_id

    async def test_double_write_idempotent(self, engine: AsyncEngine) -> None:
        manifest = _manifest("idem")
        await _queue_double_write(engine, manifest)
        # 同 idempotency_key 再次 queue:两表均命中,不重复创建。
        await _queue_double_write(engine, manifest)

        async with session_factory(engine)() as session:
            from sqlalchemy import func, select

            from finboard_persistence import BackgroundJobModel, ResearchRunModel

            run_count = (
                await session.execute(
                    select(func.count())
                    .select_from(ResearchRunModel)
                    .where(ResearchRunModel.idempotency_key == manifest.idempotency_key)
                )
            ).scalar_one()
            job_count = (
                await session.execute(
                    select(func.count())
                    .select_from(BackgroundJobModel)
                    .where(BackgroundJobModel.idempotency_key == manifest.idempotency_key)
                )
            ).scalar_one()
        assert run_count == 1
        assert job_count == 1

    async def test_cancel_propagates_to_running_job(
        self, engine: AsyncEngine
    ) -> None:
        """cancel research_run(RUNNING 态)→ job cancel_requested → 协作式收口。

        研究运行进入 RUNNING 后,coordinator.cancel 把它置 CANCELLED;同时调
        background_jobs.request_cancel(running → cancel_requested)。worker 下次
        checkpoint 时 coordinator 重读 status 发现 CANCELLED 优雅退出,job 收口。
        本测试覆盖 RUNNING 态取消链路(QUEUED 态 cancel 时 job 无人 claim,
        request_cancel 因非 running 被 suppress,job 保持 queued —— 这是预期行为)。
        """
        import contextlib

        manifest = _manifest("cancel")
        run_id = await _queue_double_write(engine, manifest)

        # 把 research_run 推进到 RUNNING(模拟 worker 已开始消费),
        # 同时让 background_job 进入 RUNNING(lease 持有)。
        async with session_factory(engine)() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            await store.transition(
                run_id,
                expected=frozenset({ResearchRunStatus.QUEUED}),
                target=ResearchRunStatus.RUNNING,
            )
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.job_id is not None
            await BackgroundJobRepository(session).transition(
                run_row.job_id,
                expected=frozenset({BackgroundJobStatus.QUEUED.value}),
                target=BackgroundJobStatus.RUNNING.value,
            )
            await store.checkpoint()

        # 取消链路:cancel research_run + request_cancel(job)。
        async with session_factory(engine)() as session:
            coordinator = ResearchRunCoordinator(
                SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            )
            record = await coordinator.cancel(run_id)
            if record.job_id:
                with contextlib.suppress(Exception):
                    await BackgroundJobRepository(session).request_cancel(
                        record.job_id
                    )
            await session.commit()

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.CANCELLED.value
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            # job 从 RUNNING → cancel_requested(协作式取消信号已写入)。
            assert job_row.status == BackgroundJobStatus.CANCEL_REQUESTED.value

    async def test_worker_recovers_interrupted_run(
        self, engine: AsyncEngine
    ) -> None:
        """worker 启动恢复:把残留 RUNNING research_run 收敛成 INTERRUPTED,可续跑。

        模拟:queue → 手动置 RUNNING(模拟 worker 崩溃前状态)→ 启动恢复
        (mark_stale_running_as_interrupted)→ 再次 queue background_job → worker
        消费 → research_run 从 INTERRUPTED 续跑到 COMPLETED。
        """
        manifest = _manifest("recover")
        run_id = await _queue_double_write(engine, manifest)

        # 模拟 worker 崩溃:把 research_run 置 RUNNING(无人推进)。
        async with session_factory(engine)() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            await store.transition(
                run_id,
                expected=frozenset({ResearchRunStatus.QUEUED}),
                target=ResearchRunStatus.RUNNING,
            )
            await store.checkpoint()

        # 启动恢复:mark_stale_running_as_interrupted。
        async with session_factory(engine)() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            await ResearchRunCoordinator(store).mark_stale_running_as_interrupted()
            await store.checkpoint()

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.INTERRUPTED.value

        # worker 消费:research_run 从 INTERRUPTED 续跑(COMPLETED)。
        worker = _build_worker(engine)
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            assert job_row.status == BackgroundJobStatus.SUCCEEDED.value

    async def test_running_job_exposes_stage_progress(
        self, engine: AsyncEngine
    ) -> None:
        """运行中 background_jobs 行可见 ``research_run:<stage>`` 阶段与逐段进度。

        用慢速双决策 adapter(决策间 sleep 0.3s)拉长运行窗口,在 worker 消费期间
        轮询任务行:必须观察到非 start/completed 的中间阶段(phase 前缀
        ``research_run:``)且 progress_done ≥ 1、progress_total ≥ done。终态 job
        phase 保持 ``research_run:completed`` 兼容(issue #188)。
        """
        manifest = _manifest("stage")
        run_id = await _queue_double_write(engine, manifest)
        worker = _build_worker(engine, adapter_factory=_make_slow_adapter_factory())

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.job_id is not None
            job_id = run_row.job_id

        await worker._fill_concurrency()
        observed_phase: str | None = None
        observed_done_total: tuple[int, int] | None = None
        for _ in range(300):  # 至多 ~6s,慢速 adapter 提供足够观察窗口
            if not worker._inflight:
                break
            async with session_factory(engine)() as session:
                job_row = await BackgroundJobRepository(session).get(job_id)
            if job_row is None or job_row.phase is None:
                continue
            is_intermediate_stage = job_row.phase.startswith(
                "research_run:"
            ) and job_row.phase not in {
                "research_run:start",
                "research_run:completed",
            }
            if is_intermediate_stage:
                observed_phase = job_row.phase
                observed_done_total = (
                    job_row.progress_done,
                    job_row.progress_total,
                )
                break
            await asyncio.sleep(0.02)

        assert observed_phase is not None, "运行中未观察到分阶段进度"
        assert observed_phase.startswith("research_run:")
        assert observed_done_total is not None
        done, total = observed_done_total
        assert done >= 1
        assert total >= done  # 当前估算 total(随已发现决策递增)不小于 done
        # 决策执行期 phase 携带决策级上下文(issue #308):
        # ``research_run:<stage>#<序号>@<日期>``;REPORT 段无后缀。此处按
        # stage 名(剥掉 #308 后缀)匹配既有阶段集合,终态兼容断言不变。
        observed_stage = observed_phase.split("#", 1)[0]
        assert observed_stage in {
            "research_run:universe",
            "research_run:features",
            "research_run:signals",
            "research_run:targets_before_constraints",
            "research_run:constraints",
            "research_run:targets_after_constraints",
            "research_run:risk_exits",
            "research_run:targets_after_risk",
            "research_run:capital_feasibility",
            "research_run:rebalance_plan",
            "research_run:orders",
            "research_run:fills",
            "research_run:ledger",
            "research_run:report",
        }

        await _drain_worker(worker)
        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value
            job_row = await BackgroundJobRepository(session).get(job_id)
            assert job_row is not None
            assert job_row.status == BackgroundJobStatus.SUCCEEDED.value
            # 终态 phase 保持与既有消费方兼容的 ``research_run:<status>``。
            assert job_row.phase == "research_run:completed"


# ---- 2026-09-13 事故回归:worker 被强杀 → lease 回收重排 → attempt 2 续跑 ----


class TestHardKillResidueResume:
    async def test_hard_kill_residue_run_resumes_on_next_attempt(
        self, engine: AsyncEngine
    ) -> None:
        """强杀残留(run 行 RUNNING + job 已重排 queued)→ attempt 2 自动续跑。

        事故链(2026-09-13):worker 被 taskkill 强杀 → attempt 1 无标注死亡
        (run 行残留 RUNNING、lease 过期)→ reclaim+requeue 后 attempt 2 领取
        → coordinator 的 RUNNING 重入门禁原样返回未执行 record → job 以
        failed + 空错误码收口、run 行永久悬挂 RUNNING。修复:执行器持有
        claim 时先把残留 RUNNING 收敛为 INTERRUPTED(process_restart 语义),
        coordinator 走 #305/#314 恢复通道续跑。
        """
        manifest = _manifest("hardkill")
        run_id = await _queue_double_write(engine, manifest)

        # attempt 1「被硬杀」:run 行置 RUNNING、job 行置 RUNNING(无任何标注)。
        async with session_factory(engine)() as session:
            store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            await store.transition(
                run_id,
                expected=frozenset({ResearchRunStatus.QUEUED}),
                target=ResearchRunStatus.RUNNING,
            )
            await store.checkpoint()
        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            job_row.status = BackgroundJobStatus.RUNNING.value
            await session.commit()

        # lease 过期回收 + 重排的合成效果:job 回到 queued 等待重领,run 行
        # 仍残留 RUNNING(事故的关键形态)。
        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            job_row.status = BackgroundJobStatus.QUEUED.value
            await session.commit()

        # attempt 2:执行器收敛残留 → coordinator 恢复通道 → COMPLETED。
        worker = _build_worker(engine)
        await _drain_worker(worker)

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            assert job_row.status == BackgroundJobStatus.SUCCEEDED.value
