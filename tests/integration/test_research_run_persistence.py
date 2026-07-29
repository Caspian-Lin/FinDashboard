"""ResearchRun PostgreSQL full lifecycle, restart and deterministic replay."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.research_run import (
    DecisionBundle,
    DecisionSequenceAdapter,
    FrozenArtifactRef,
    LedgerSnapshot,
    ResearchRunCoordinator,
    ResearchRunManifest,
    ResearchRunReport,
    ResearchRunStatus,
    UniverseCandidate,
    stable_checksum,
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


def _decision() -> DecisionBundle:
    return DecisionBundle(
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
        constraints=(),
        targets_after_constraints=(),
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


def _adapter(kind: str) -> DecisionSequenceAdapter:
    decision = _decision()
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


async def test_full_run_history_restart_replay_and_strategy_isolation(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
) -> None:
    store = SqlAlchemyResearchRunStore(ResearchRunRepository(db_session))
    coordinator = ResearchRunCoordinator(store)
    source_manifest = _manifest("ma_cross", "source")
    source = await coordinator.execute(source_manifest, _adapter("ma_cross"))
    second_manifest = _manifest("etf_rotation", "etf")
    second = await coordinator.execute(second_manifest, _adapter("etf_rotation"))

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
        assert len(await restarted_store.list_artifacts(source_manifest.run_id)) == 11
        rows = await ResearchRunRepository(restarted).list_recent(limit=10)
        assert {row.strategy_kind for row in rows} == {"ma_cross", "etf_rotation"}

        replayed = await ResearchRunCoordinator(restarted_store).replay(
            source_run_id=source_manifest.run_id,
            new_run_id="RR-integration-replay",
            idempotency_key="integration-idempotency-replay",
            requested_by="integration-test",
            adapter=_adapter("ma_cross"),
        )
        assert replayed.status is ResearchRunStatus.COMPLETED
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
    resumed = await coordinator.execute(manifest, _adapter("ma_cross"))

    assert interrupted
    assert resumed.status is ResearchRunStatus.COMPLETED
    assert len(await store.list_artifacts(manifest.run_id)) == 11
