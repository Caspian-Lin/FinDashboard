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
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.background_jobs import JobExecutorRegistry
from finboard_backtest.background_jobs.executors import ResearchRunExecutor
from finboard_backtest.background_jobs.executors.research_run import (
    SessionPerOperationResearchRunStore,
    default_store_factory,
)
from finboard_backtest.background_jobs.worker import BackgroundWorker, WorkerConfig
from finboard_backtest.portfolio import AssetLotInfo, CovarianceEstimate
from finboard_backtest.research_run import (
    DecisionBundle,
    FeatureValue,
    FrozenArtifactRef,
    NormalizedSignal,
    ResearchArtifact,
    ResearchRunConflictError,
    ResearchRunCoordinator,
    ResearchRunInterruptedError,
    ResearchRunManifest,
    ResearchRunStage,
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
from finboard_backtest.research_run.signal_engine import SignalEnginePipelineAdapter
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
from tests.unit.research_run.test_issue_306_load_probe import (
    SESSIONS,
    _build_release,
    _noop_snapshot_provider,
)
from tests.unit.research_run.test_issue_306_load_probe import (
    _manifest as _release_manifest,
)
from tests.unit.research_run.test_issue_470b_stub_prefix import _InterruptAfterKSignalAdapter

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
    async with asyncio.timeout(30):
        await asyncio.gather(*worker._inflight)
    assert not worker._inflight


# ---- tests --------------------------------------------------------------------


class TestIssue314CheckpointResumeIntegration:
    @pytest.mark.parametrize("delete_saved_features", [False, True])
    async def test_frozen_prefetch_and_resume_match_serial_artifacts(
        self, engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        delete_saved_features: bool,
    ) -> None:
        """真实 Parquet + worker + PostgreSQL,中断跨批续跑后逐 stage 对照。"""
        from finboard_data import trading_calendar

        monkeypatch.setattr(trading_calendar, "_CACHE", set(SESSIONS))
        provider = await _build_release(tmp_path)
        release = provider.release
        source = _release_manifest()
        spec = source.strategy_spec
        rule = spec.signal_rules.rules[0].model_copy(update={"threshold": 1.0})
        spec = spec.model_copy(update={
            "signal_rules": spec.signal_rules.model_copy(update={"rules": (rule,)}),
            "portfolio_policy": spec.portfolio_policy.model_copy(update={
                "max_target_weight": 0.25,
                "target_gross_exposure": 0.30,
                "target_net_exposure": 0.30,
            }),
        })
        selected = SESSIONS[70:96]
        manifest = replace(
            source,
            run_id="RR-frozen-perf-resumed",
            idempotency_key="frozen-perf-resumed",
            strategy_spec=spec,
            strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
            dataset_releases=(replace(source.dataset_releases[0], checksum=release.release_checksum),),
            parameters={"decision_schedule": {
                "kind": "custom", "dates": [day.isoformat() for day in selected],
            }},
        )
        control_manifest = replace(
            manifest, run_id="RR-frozen-perf-control", idempotency_key="frozen-perf-control",
        )

        async def trading_days() -> list[date]:
            return SESSIONS

        def adapter_kwargs() -> dict[str, Any]:
            return {
                "release_provider_factory": lambda release_id: provider,
                "snapshot_provider": _noop_snapshot_provider,
                "trading_days_loader": trading_days,
            }

        session_maker = session_factory(engine)
        store = SessionPerOperationResearchRunStore(session_maker, default_store_factory)
        control = await ResearchRunCoordinator(store).execute(
            control_manifest,
            SignalEnginePipelineAdapter(
                manifest=control_manifest, prefetch_next_batch=False, **adapter_kwargs()
            ),
        )
        assert control.status is ResearchRunStatus.COMPLETED, control.error_summary
        prefix = 13
        interrupted = await ResearchRunCoordinator(store).execute(
            manifest,
            _InterruptAfterKSignalAdapter(k=prefix, manifest=manifest, **adapter_kwargs()),
        )
        assert interrupted.status is ResearchRunStatus.INTERRUPTED, interrupted.error_summary
        checkpoint = await store.list_artifacts(manifest.run_id)
        assert len(checkpoint) == prefix * _STAGE_COUNT
        first_features_id = next(
            row.artifact_id for row in checkpoint if row.stage is ResearchRunStage.FEATURES
        )

        persisted_decision_ids: list[str | None] = []
        readback_progress: list[tuple[int, int | None, str]] = []
        append_artifacts = SqlAlchemyResearchRunStore.append_artifacts
        match_checksums = SessionPerOperationResearchRunStore.checkpoint_checksums_match
        update_progress = BackgroundJobRepository.update_progress
        missed = False

        async def record_checksum_match(
            store: SessionPerOperationResearchRunStore, run_id: str, checksums: dict[str, str],
        ) -> bool:
            nonlocal missed
            if delete_saved_features and not missed:
                missed = True
                async with session_maker() as session, session.begin():
                    await session.execute(
                        text("DELETE FROM research_run_artifacts WHERE run_id=:run AND artifact_id=:id"),
                        {"run": run_id, "id": first_features_id},
                    )
            return await match_checksums(store, run_id, checksums)

        async def record_append(
            store: SqlAlchemyResearchRunStore, artifacts: Sequence[ResearchArtifact]
        ) -> list[bool]:
            persisted_decision_ids.append(artifacts[0].decision_id)
            return await append_artifacts(store, artifacts)

        async def record_progress(
            repo: BackgroundJobRepository, job_id: str, **kwargs: Any,
        ) -> Any:
            phase = kwargs.get("phase")
            if phase and phase.startswith("research_run:resume_readback "):
                readback_progress.append((kwargs["done"], kwargs["total"], phase))
            return await update_progress(repo, job_id, **kwargs)

        monkeypatch.setattr(SqlAlchemyResearchRunStore, "append_artifacts", record_append)
        monkeypatch.setattr(
            SessionPerOperationResearchRunStore, "checkpoint_checksums_match", record_checksum_match,
        )
        monkeypatch.setattr(BackgroundJobRepository, "update_progress", record_progress)

        def factory(run_manifest: ResearchRunManifest) -> ResearchStrategyAdapter:
            return SignalEnginePipelineAdapter(
                manifest=run_manifest, process_workers=2, **adapter_kwargs()
            )

        job_id = await _queue_job(engine, manifest)
        await _drain_worker(_build_worker(engine, factory))
        recovered = await store.get(manifest.run_id)
        assert recovered is not None
        assert recovered.status is ResearchRunStatus.COMPLETED, recovered.error_summary
        assert recovered.result_checksum == control.result_checksum
        assert recovered.timing is not None
        assert recovered.timing["resume_checkpoints_reused"] == prefix - int(delete_saved_features)
        assert readback_progress[0] == (0, None, "research_run:resume_readback 1")
        fallback_writes = [f"{manifest.run_id}:D:00000000"] if delete_saved_features else []
        assert persisted_decision_ids == fallback_writes + [
            f"{manifest.run_id}:D:{index:08d}" for index in range(prefix, len(selected))
        ]
        timing = cast(dict[str, Any], recovered.timing["decision_load_stages"])
        assert timing["initial_covariance"]["calls"] == len(selected) - prefix
        assert timing["decision_load_without_covariance"]["calls"] == len(selected) - prefix

        resumed_rows = await store.list_artifacts(manifest.run_id)
        control_rows = await store.list_artifacts(control_manifest.run_id)
        assert len(resumed_rows) == len(selected) * _STAGE_COUNT + 1
        assert _artifact_fingerprint(resumed_rows) == _artifact_fingerprint(control_rows)
        assert [row.payload for row in resumed_rows] == [row.payload for row in control_rows]
        for resumed, original in zip(resumed_rows[:len(checkpoint)], checkpoint, strict=True):
            if delete_saved_features and original.artifact_id == first_features_id:
                assert resumed.created_at > original.created_at
            else:
                assert resumed.created_at == original.created_at
        async with session_maker() as session:
            job = await BackgroundJobRepository(session).get(job_id)
        assert job is not None
        assert job.status == BackgroundJobStatus.SUCCEEDED.value
        assert job.attempt == 1

    async def test_batch_concurrent_replay_and_sequence_conflict(self, engine: AsyncEngine) -> None:
        """两个短会话同批写入只提交一次,唯一约束冲突时整批回滚。"""
        manifest = _manifest("batch-conflict")
        store = SessionPerOperationResearchRunStore(session_factory(engine), default_store_factory)
        await store.create_or_get(manifest)

        def artifact(name: str, sequence: int) -> ResearchArtifact:
            return ResearchArtifact(
                artifact_id=f"{manifest.run_id}:A:{name}",
                run_id=manifest.run_id,
                decision_id=f"{manifest.run_id}:D:00000000",
                sequence=sequence,
                stage=ResearchRunStage.FEATURES,
                trace_id=f"trace-{name}",
                parent_trace_ids=(),
                payload={},
                payload_json='{"value":-0.0}',
                checksum=stable_checksum({"value": -0.0}),
            )

        batch = [artifact("feature-a", 0), artifact("feature-b", 1)]
        first, second = await asyncio.gather(
            store.append_artifacts(batch), store.append_artifacts(batch)
        )
        assert sorted([first, second]) == [[False, False], [True, True]]
        assert await store.append_artifacts([batch[0], batch[0]]) == [False, False]
        with pytest.raises(ResearchRunConflictError, match="checkpoint 内容冲突"):
            await store.append_artifacts([
                artifact("feature-c", 2), replace(batch[1], checksum="f" * 64),
            ])
        with pytest.raises(ResearchRunConflictError, match="序号或 trace_id 冲突"):
            await store.append_artifacts([artifact("feature-c", 2), artifact("collision", 0)])
        async with session_factory(engine)() as session:
            direct_store = SqlAlchemyResearchRunStore(ResearchRunRepository(session))
            with pytest.raises(ResearchRunConflictError, match="序号或 trace_id 冲突"):
                await direct_store.append_artifacts([
                    artifact("feature-c", 2), artifact("collision", 0),
                ])
            await direct_store.checkpoint()
        persisted = await store.list_artifacts(manifest.run_id)
        assert [row.checksum for row in persisted] == [row.checksum for row in batch]
        assert [stable_checksum(row.payload) for row in persisted] == [
            row.checksum for row in batch
        ]

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
