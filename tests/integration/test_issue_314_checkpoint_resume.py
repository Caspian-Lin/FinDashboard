"""research_run 断点续算集成测试(issue #314,PostgreSQL 端到端)。

真实 ``SqlAlchemyResearchRunStore`` + ``PortfolioPipelineAdapter`` + 统一任务
队列 worker:

* 跑 N 个决策后中断(INTERRUPTED,已落库决策 artifact 保留)→ 重新入队由
  worker 恢复 → 已完成决策零重算(``_build_decision`` 调用计数断言);
* 恢复后 ``result_checksum`` 与一次跑完的对照 run 等值,artifact 指纹逐位
  一致(报告完整、确定性无漂移);
* job 进度列:恢复执行完成后 done/total 覆盖全部决策 + report(#188 兼容);
* checksum 篡改 → 重算端幂等去重抛「checkpoint 内容冲突」→ run FAILED
  (既有 fail-closed 语义零变化)。

依赖 PostgreSQL(``FINBOARD_TEST_DB_URL``,回退 ``FINBOARD_DB_URL``)。
纯离线研究域,不连 broker / 不下实盘单。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

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
from finboard_backtest.background_jobs.worker import BackgroundWorker, WorkerConfig
from finboard_backtest.portfolio import AssetLotInfo, CovarianceEstimate
from finboard_backtest.research_run import (
    DecisionBundle,
    FeatureValue,
    FrozenArtifactRef,
    NormalizedSignal,
    ResearchRunCoordinator,
    ResearchRunInterruptedError,
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

SYMBOLS = ("A.SH", "B.SH", "C.SH")
_DECISION_COUNT = 3
_STAGE_COUNT = 13


# ---- engine fixture(与 test_research_run_worker.py 一致) ---------------------


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


# ---- manifest / adapter 构造 --------------------------------------------------


def _manifest(suffix: str) -> ResearchRunManifest:
    spec = build_strategy_template(
        "ma_cross",
        # 固定 strategy_id:run_id/idempotency_key 之外的一切冻结输入必须
        # 一致(input_checksum 相同 → 管线 ID 命名空间相同 → 跨 run 可比)。
        strategy_id="ma_cross_314",
        dataset_release_ids=("frozen-release-v1",),
    )
    return ResearchRunManifest(
        run_id=f"RR-issue314-{suffix}",
        idempotency_key=f"issue314-{suffix}",
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
        requested_by="issue314-integration",
    )


def _input(day_index: int, scores: tuple[float, ...]) -> PortfolioDecisionInput:
    decision_at = datetime(2024, 1, 2 + day_index, 15, tzinfo=UTC)
    return PortfolioDecisionInput(
        business_date=date(2024, 1, 2 + day_index),
        decision_at=decision_at,
        execution_at=datetime(2024, 1, 3 + day_index, 9, 30, tzinfo=UTC),
        candidates=tuple(
            UniverseCandidate(
                symbol=symbol,
                included=True,
                reasons=("集成测试候选池通过",),
                asset_class="equity",
                market="a_share",
            )
            for symbol in SYMBOLS
        ),
        features=tuple(
            FeatureValue(
                symbol=symbol,
                feature_id="close",
                value=10.0,
                source_artifact_ids=("frozen-release-v1",),
                available_at=decision_at,
            )
            for symbol in SYMBOLS
        ),
        signals=tuple(
            NormalizedSignal(
                symbol=symbol,
                score=score,
                action="buy" if score > 0 else "sell",
                rule_id="issue314-integration",
                rationale="断点续算集成测试信号",
            )
            for symbol, score in zip(SYMBOLS, scores, strict=True)
        ),
        prices=dict.fromkeys(SYMBOLS, 10.0),
        execution_prices=dict.fromkeys(SYMBOLS, 10.0),
        lot_info={symbol: AssetLotInfo(code=symbol, lot_size=100) for symbol in SYMBOLS},
        input_artifact_ids=("frozen-release-v1",),
        covariance=CovarianceEstimate(
            matrix=np.diag([0.01, 0.01, 0.01]),
            tickers=list(SYMBOLS),
            shrinkage=0.0,
            n_observations=252,
        ),
    )


def _decision_inputs() -> tuple[PortfolioDecisionInput, ...]:
    return (
        _input(0, (1.0, 1.0, 1.0)),  # 建仓
        _input(1, (-1.0, -1.0, -1.0)),  # 清仓
        _input(2, (1.0, 1.0, 1.0)),  # 再建仓:恢复边界之后仍有真实交易
    )


class _CountingPipeline(PortfolioPipelineAdapter):
    """统计 _build_decision 调用次数(零重算断言)。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.build_calls = 0

    def _build_decision(self, **kwargs: Any) -> Any:
        self.build_calls += 1
        return super()._build_decision(**kwargs)


class _CountingSignalPipelineAdapter(PortfolioPipelineAdapter):
    """固定输入 + 计数的可复用适配器(equivalent to PortfolioPipelineAdapter)。"""

    def __init__(
        self,
        *,
        decision_inputs: tuple[PortfolioDecisionInput, ...],
    ) -> None:
        super().__init__(strategy_kind="ma_cross", decision_inputs=decision_inputs)
        self.pipeline: _CountingPipeline | None = None

    async def decisions(
        self, manifest: ResearchRunManifest
    ) -> AsyncIterator[DecisionBundle]:
        self.pipeline = _CountingPipeline(
            strategy_kind=self.strategy_kind,
            decision_inputs=self._inputs,
        )
        if self._resume_bundles is not None:
            self.pipeline.resume_from(self._resume_bundles)
        async for decision in self.pipeline.decisions(manifest):
            yield decision


class _InterruptAfterFirstAdapter(PortfolioPipelineAdapter):
    """产出第一条决策后模拟进程崩溃(runner 收口为 INTERRUPTED)。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    async def decisions(
        self, manifest: ResearchRunManifest
    ) -> AsyncIterator[DecisionBundle]:
        produced = 0
        async for decision in super().decisions(manifest):
            yield decision
            produced += 1
            if produced >= 1:
                raise ResearchRunInterruptedError("integration crash")


# ---- helper -------------------------------------------------------------------


def _artifact_fingerprint(rows: list[Any]) -> list[tuple[str, str, str]]:
    return [
        (item.artifact_id.split(":A:", 1)[1], item.stage.value, item.checksum)
        for item in rows
    ]




async def _queue_job(engine: AsyncEngine, manifest: ResearchRunManifest) -> str:
    """为既有 run 登记 background_job 并绑定 run.job_id,返回 job_id。"""
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
        checksum = _payload_checksum(payload)
        job_row, _ = await job_repo.create_or_get(
            job_id=generate_background_job_id(),
            idempotency_key=f"{manifest.idempotency_key}-job",
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
        return job_row.job_id


def _payload_checksum(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _build_worker(
    engine: AsyncEngine,
    adapter_factory: Any,
) -> BackgroundWorker:
    registry = JobExecutorRegistry()
    registry.register(
        "research_run",
        ResearchRunExecutor(
            session_maker=session_factory(engine),
            store_factory=default_store_factory,
            adapter_factory=adapter_factory,
        ),
    )
    return BackgroundWorker(
        engine=engine,
        session_maker=session_factory(engine),
        registry=registry,
        config=WorkerConfig(
            worker_id="w-test-314",
            poll_interval_seconds=0.05,
            max_concurrent=2,
            lease_timeout_seconds=60,
            heartbeat_interval_seconds=10.0,
        ),
    )


async def _drain_worker(worker: BackgroundWorker) -> None:
    await worker._fill_concurrency()
    for _ in range(400):
        if not worker._inflight:
            break
        await asyncio.sleep(0.02)
    assert not worker._inflight


# ---- tests --------------------------------------------------------------------


class TestIssue314CheckpointResumeIntegration:
    async def test_interrupt_then_worker_resume_skips_completed_decisions(
        self, engine: AsyncEngine
    ) -> None:
        """验收主路径:跑 3 决策中断于 1 → worker 恢复 → 已落库决策零重算,
        result_checksum 与一次跑完等值。"""
        inputs = _decision_inputs()

        # 对照:一次跑完(独立 run)
        control_manifest = _manifest("control")
        control_adapter = _CountingSignalPipelineAdapter(decision_inputs=inputs)
        async with session_factory(engine)() as session:
            control_store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            control = await ResearchRunCoordinator(control_store).execute(
                control_manifest, control_adapter
            )
            await control_store.checkpoint()
        assert control.status is ResearchRunStatus.COMPLETED
        assert control.result_checksum is not None
        assert control_adapter.pipeline is not None
        assert control_adapter.pipeline.build_calls == 3
        async with session_factory(engine)() as session:
            control_rows = await SqlAlchemyResearchRunStore(
                ResearchRunRepository(session)
            ).list_artifacts(control_manifest.run_id)
        assert len(control_rows) == _DECISION_COUNT * _STAGE_COUNT + 1

        # 第一跳:真实组合管线跑 1 个决策后中断
        manifest = _manifest("resume")
        interrupted_adapter = _InterruptAfterFirstAdapter(strategy_kind="ma_cross", decision_inputs=inputs)
        async with session_factory(engine)() as session:
            run_store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            interrupted = await ResearchRunCoordinator(run_store).execute(
                manifest, interrupted_adapter
            )
            await run_store.checkpoint()
        assert interrupted.status is ResearchRunStatus.INTERRUPTED
        async with session_factory(engine)() as session:
            rows = await SqlAlchemyResearchRunStore(
                ResearchRunRepository(session)
            ).list_artifacts(manifest.run_id)
        assert len(rows) == _STAGE_COUNT  # 仅决策 1 落库

        # 重新入队:worker 消费恢复(计数 adapter 经工厂注入)
        adapter_holder: list[_CountingSignalPipelineAdapter] = []

        def factory(run_manifest: ResearchRunManifest) -> ResearchStrategyAdapter:
            del run_manifest
            adapter = _CountingSignalPipelineAdapter(decision_inputs=inputs)
            adapter_holder.append(adapter)
            return adapter

        await _queue_job(engine, manifest)
        worker = _build_worker(engine, factory)
        await _drain_worker(worker)

        # 断言:恢复完成、已完成决策零重算(仅决策 2、3 参与组合构建)
        assert len(adapter_holder) == 1
        resumed_adapter = adapter_holder[0]
        assert resumed_adapter.pipeline is not None
        assert resumed_adapter.pipeline.build_calls == 2
        assert resumed_adapter._resume_bundles is not None
        assert len(resumed_adapter._resume_bundles) == 1

        async with session_factory(engine)() as session:
            run_row = await ResearchRunRepository(session).get(manifest.run_id)
            assert run_row is not None
            assert run_row.status == ResearchRunStatus.COMPLETED.value
            assert run_row.result_checksum == control.result_checksum
            assert run_row.job_id is not None
            job_row = await BackgroundJobRepository(session).get(run_row.job_id)
            assert job_row is not None
            assert job_row.status == BackgroundJobStatus.SUCCEEDED.value
            # 进度列覆盖全部决策 + report(3*13 + 1),#188 口径兼容
            assert job_row.progress_done == _DECISION_COUNT * _STAGE_COUNT + 1
            assert job_row.progress_total == _DECISION_COUNT * _STAGE_COUNT + 1
            assert job_row.phase == "research_run:completed"

        # artifact 指纹逐位一致(跨 run_id 可比)
        async with session_factory(engine)() as session:
            resumed_rows = await SqlAlchemyResearchRunStore(
                ResearchRunRepository(session)
            ).list_artifacts(manifest.run_id)
        assert len(resumed_rows) == len(control_rows)
        assert _artifact_fingerprint(resumed_rows) == _artifact_fingerprint(control_rows)

    async def test_checksum_tampering_fails_closed_with_conflict(
        self, engine: AsyncEngine
    ) -> None:
        """checksum 篡改 → 读回截断 → 全量重算 → 幂等去重抛
        「checkpoint 内容冲突」→ run FAILED(既有 fail-closed 语义零变化)。"""
        from sqlalchemy import text

        inputs = _decision_inputs()
        manifest = _manifest("tamper")
        async with session_factory(engine)() as session:
            run_store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            interrupted = await ResearchRunCoordinator(run_store).execute(
                manifest, _InterruptAfterFirstAdapter(strategy_kind="ma_cross", decision_inputs=inputs)
            )
            await run_store.checkpoint()
        assert interrupted.status is ResearchRunStatus.INTERRUPTED

        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE research_run_artifacts SET checksum = :checksum "
                    "WHERE run_id = :run_id AND stage = 'universe'"
                ),
                {"run_id": manifest.run_id, "checksum": "f" * 64},
            )
            assert result.rowcount == 1

        async with session_factory(engine)() as session:
            resume_store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            resumed = await ResearchRunCoordinator(resume_store).execute(
                manifest, _CountingSignalPipelineAdapter(decision_inputs=inputs)
            )
            await resume_store.checkpoint()
        assert resumed.status is ResearchRunStatus.FAILED
        assert resumed.error_code == "ResearchRunConflictError"
        assert "checkpoint 内容冲突" in (resumed.error_summary or "")
