"""ResearchRun PostgreSQL full lifecycle, restart and deterministic replay."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.portfolio import AssetLotInfo, CovarianceEstimate
from finboard_backtest.research_run import (
    CapitalTierOutcome,
    ConstraintOutcome,
    DecisionBundle,
    DecisionSequenceAdapter,
    FeatureValue,
    FrozenArtifactRef,
    LedgerSnapshot,
    NormalizedSignal,
    ResearchPipelineEvidence,
    ResearchRiskState,
    ResearchRunCoordinator,
    ResearchRunManifest,
    ResearchRunReport,
    ResearchRunStatus,
    UniverseCandidate,
    pipeline_output_checksum,
    stable_checksum,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
)
from finboard_backtest.strategy_spec import build_strategy_template
from finboard_persistence import ResearchRunRepository, session_factory

pytestmark = pytest.mark.asyncio


def _manifest(kind: str, suffix: str) -> ResearchRunManifest:
    capabilities = {
        "ma_cross": ("stock",),
        "etf_rotation": ("etf:index",),
    }
    spec = build_strategy_template(
        kind,
        strategy_id=f"integration_{kind}_{suffix}",
        dataset_release_ids=("frozen-release-v1",),
    )
    return ResearchRunManifest(
        run_id=f"RR-integration-{suffix}",
        idempotency_key=f"integration-idempotency-{suffix}",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="frozen-release-v1",
                version="v1",
                checksum="a" * 64,
                capabilities=capabilities[kind],
            ),
        ),
        code_version="abcdef0123456789",
        initial_capital=Decimal("200000"),
        requested_by="integration-test",
    )


def _decision(manifest: ResearchRunManifest) -> DecisionBundle:
    input_checksum = stable_checksum({"fixture": "integration-empty-decision"})
    decision = DecisionBundle(
        business_date=date(2024, 1, 2),
        decision_at=datetime(2024, 1, 2, 15, tzinfo=UTC),
        candidates=(
            UniverseCandidate(
                symbol="510300.SH",
                included=False,
                reasons=("固定样本没有触发信号",),
                asset_class="equity",
                market="a_share",
            ),
        ),
        features=(),
        signals=(),
        targets_before_constraints=(),
        constraints=(
            ConstraintOutcome(
                constraint="max_risk_contribution",
                passed=True,
                before_value=0.0,
                after_value=0.0,
                limit=0.35,
                reason="空组合无风险贡献",
            ),
        ),
        targets_after_constraints=(),
        risk_exits=(),
        targets_after_risk=(),
        risk_state=ResearchRiskState(
            cooldown_until={},
            opened_on={},
            high_water_prices={},
            portfolio_equity_high_water=Decimal("200000"),
            portfolio_drawdown=0.0,
            portfolio_paused=False,
        ),
        capital_feasibility=tuple(
            CapitalTierOutcome(
                tier=tier,
                capital=capital,
                feasible=True,
                cash_utilization=0.0,
                tracking_error=0.0,
                unfillable_symbols=(),
                capacity_pressure=0.0,
                margin_required=Decimal("0"),
                estimated_costs=Decimal("0"),
                reasons=("空组合可执行",),
                input_checksum=input_checksum,
            )
            for tier, capital in (
                ("100k", Decimal("100000")),
                ("200k", Decimal("200000")),
                ("500k", Decimal("500000")),
            )
        ),
        rebalance_plan=(),
        orders=(),
        fills=(),
        positions=(),
        ledger=LedgerSnapshot(
            cash=Decimal("200000"),
            market_value=Decimal("0"),
            margin_used=Decimal("0"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            equity=Decimal("200000"),
            fees_paid=Decimal("0"),
            tax_paid=Decimal("0"),
            slippage_paid=Decimal("0"),
        ),
    )
    return replace(
        decision,
        pipeline_evidence=ResearchPipelineEvidence(
            manifest_input_checksum=manifest.input_checksum,
            input_checksum=input_checksum,
            output_checksum=pipeline_output_checksum(decision),
            hard_constraints_passed=True,
        ),
    )


def _adapter(
    kind: str, manifest: ResearchRunManifest
) -> DecisionSequenceAdapter:
    decision = _decision(manifest)
    return DecisionSequenceAdapter(
        strategy_kind=kind,
        decisions=(decision,),
        report=ResearchRunReport(
            strategy_kind=kind,
            strategy_return=0.0,
            benchmark_symbol="510300.SH",
            benchmark_return=0.0,
            excess_return=0.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
            final_equity=Decimal("200000"),
            final_cash=Decimal("200000"),
            commission_paid=Decimal("0"),
            tax_paid=Decimal("0"),
            slippage_paid=Decimal("0"),
            fill_shortfall=Decimal("0"),
            constraint_impact={},
            decision_count=1,
            order_count=0,
            fill_count=0,
        ),
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


async def test_full_run_history_restart_replay_and_strategy_isolation(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
) -> None:
    store = SqlAlchemyResearchRunStore(ResearchRunRepository(db_session))
    coordinator = ResearchRunCoordinator(store)
    source_manifest = _manifest("ma_cross", "source")
    source = await coordinator.execute(source_manifest, _portfolio_adapter())
    second_manifest = _manifest("etf_rotation", "etf")
    second = await coordinator.execute(
        second_manifest, _adapter("etf_rotation", second_manifest)
    )

    assert source.status is ResearchRunStatus.COMPLETED
    assert second.status is ResearchRunStatus.COMPLETED

    maker = session_factory(_engine)
    async with maker() as restarted:
        restarted_store = SqlAlchemyResearchRunStore(
            ResearchRunRepository(restarted)
        )
        restored = await restarted_store.get(source_manifest.run_id)
        assert restored is not None
        assert restored.status is ResearchRunStatus.COMPLETED
        assert restored.manifest.input_checksum == source_manifest.input_checksum
        assert len(await restarted_store.list_artifacts(source_manifest.run_id)) == 14
        rows = await ResearchRunRepository(restarted).list_recent(limit=10)
        assert {row.strategy_kind for row in rows} == {"ma_cross", "etf_rotation"}

        replayed = await ResearchRunCoordinator(restarted_store).replay(
            source_run_id=source_manifest.run_id,
            new_run_id="RR-integration-replay",
            idempotency_key="integration-idempotency-replay",
            requested_by="integration-test",
            adapter=_portfolio_adapter(),
        )
        assert replayed.status is ResearchRunStatus.COMPLETED, replayed.error_summary
        assert replayed.result_checksum == restored.result_checksum


async def test_running_checkpoint_is_recovered_after_new_session(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
) -> None:
    del _engine
    manifest = _manifest("ma_cross", "interrupted")
    store = SqlAlchemyResearchRunStore(ResearchRunRepository(db_session))
    await store.create_or_get(manifest)
    await store.transition(
        manifest.run_id,
        expected=frozenset({ResearchRunStatus.QUEUED}),
        target=ResearchRunStatus.RUNNING,
    )
    await store.checkpoint()

    coordinator = ResearchRunCoordinator(store)
    interrupted = await coordinator.mark_stale_running_as_interrupted()
    resumed = await coordinator.execute(manifest, _adapter("ma_cross", manifest))

    assert interrupted
    assert resumed.status is ResearchRunStatus.COMPLETED
    assert len(await store.list_artifacts(manifest.run_id)) == 14


async def test_interrupted_run_replay_recovers_with_frozen_inputs(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
) -> None:
    """issue #305:RR-7a74 恢复通道 —— interrupted run 一条 replay 命令恢复。

    进程重启(mark_stale_running_as_interrupted)后,源 run 的 error_summary
    指向 finboard_run_replay;replay 的新 run 经 PostgreSQL 存储自动继承全部
    冻结输入(含 factor_snapshots 全集,零手工),血缘标注
    replay_of_run_id + replay_source_status;源 run 保持 INTERRUPTED。
    """
    del _engine
    base_manifest = _manifest("ma_cross", "interrupted-305")
    snapshots = (
        FrozenArtifactRef(
            artifact_id="factor-305-a",
            version="v1",
            checksum="b" * 64,
            capabilities=("factor:close",),
        ),
        FrozenArtifactRef(
            artifact_id="factor-305-b",
            version="v1",
            checksum="c" * 64,
            capabilities=("factor:momentum",),
        ),
    )
    manifest = replace(base_manifest, factor_snapshots=snapshots)
    store = SqlAlchemyResearchRunStore(ResearchRunRepository(db_session))
    await store.create_or_get(manifest)
    await store.transition(
        manifest.run_id,
        expected=frozenset({ResearchRunStatus.QUEUED}),
        target=ResearchRunStatus.RUNNING,
    )
    await store.checkpoint()

    coordinator = ResearchRunCoordinator(store)
    recovered = await coordinator.mark_stale_running_as_interrupted()
    assert [item.status for item in recovered] == [ResearchRunStatus.INTERRUPTED]
    assert "finboard_run_replay" in (recovered[0].error_summary or "")

    replayed = await coordinator.replay(
        source_run_id=manifest.run_id,
        new_run_id="RR-integration-305-replay",
        idempotency_key="integration-idempotency-305-replay",
        requested_by="integration-test",
        adapter=_adapter("ma_cross", manifest),
    )

    assert replayed.status is ResearchRunStatus.COMPLETED, replayed.error_summary
    # 血缘标注:replay-of-interrupted。
    assert replayed.manifest.replay_of_run_id == manifest.run_id
    assert replayed.manifest.replay_source_status == "interrupted"
    # 冻结输入零手工继承(跨 PostgreSQL JSONB 往返后仍逐项一致)。
    assert replayed.manifest.input_checksum == manifest.input_checksum
    assert replayed.manifest.factor_snapshots == snapshots
    assert replayed.manifest.dataset_releases == manifest.dataset_releases
    # 源 run 不被复活。
    source_after = await store.get(manifest.run_id)
    assert source_after is not None
    assert source_after.status is ResearchRunStatus.INTERRUPTED
    assert source_after.result_checksum is None


async def test_job_timing_round_trips_through_result_json(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
) -> None:
    """issue #285:分段耗时随 result JSON 落库,重启读回后原样可见。"""
    manifest = _manifest("ma_cross", "timing")
    store = SqlAlchemyResearchRunStore(ResearchRunRepository(db_session))
    record = await ResearchRunCoordinator(store).execute(
        manifest, _adapter("ma_cross", manifest)
    )

    assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
    timing = record.timing
    assert timing is not None
    assert cast(int, cast(dict[str, object], timing["decision_execute"])["count"]) == 1
    assert cast(float, timing["total_elapsed_seconds"]) >= 0

    # 落库位置:timing 冗余存放在 result JSON 的 "timing" 键下;
    # result_checksum 只锚定报告字段,与 timing 取值无关。
    row = await ResearchRunRepository(db_session).get(manifest.run_id)
    assert row is not None
    assert isinstance(row.result, dict)
    assert row.result["timing"] == timing
    assert row.result_checksum == record.result_checksum

    # 重启(新 session)读回:timing 原样可见。
    maker = session_factory(_engine)
    async with maker() as restarted:
        restored = await SqlAlchemyResearchRunStore(
            ResearchRunRepository(restarted)
        ).get(manifest.run_id)
        assert restored is not None
        assert restored.timing == timing
