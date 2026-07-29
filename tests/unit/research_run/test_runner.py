from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from finboard_backtest.research_run import (
    ConstraintOutcome,
    DecisionSequenceAdapter,
    InMemoryResearchRunStore,
    ResearchFillAction,
    ResearchOrderStatus,
    ResearchPosition,
    ResearchPositionSide,
    ResearchRunCoordinator,
    ResearchRunInterruptedError,
    ResearchRunStatus,
    registered_adapter_kinds,
)

from .conftest import fixed_report


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", registered_adapter_kinds())
async def test_all_six_strategies_share_complete_lifecycle(
    manifest_factory, decision_factory, kind: str
) -> None:
    manifest = manifest_factory(kind)
    action = (
        ResearchFillAction.OPEN_SHORT
        if kind == "futures_tsmom"
        else ResearchFillAction.OPEN_LONG
    )
    decision = decision_factory(action=action)
    adapter = DecisionSequenceAdapter(
        strategy_kind=kind,
        decisions=(decision,),
        report=fixed_report(kind, decision),
    )
    store = InMemoryResearchRunStore()
    coordinator = ResearchRunCoordinator(store)

    record = await coordinator.execute(manifest, adapter)

    assert record.status is ResearchRunStatus.COMPLETED
    assert record.result is not None
    artifacts = await store.list_artifacts(manifest.run_id)
    assert len(artifacts) == 11
    assert [item.stage.value for item in artifacts] == [
        "universe",
        "features",
        "signals",
        "targets_before_constraints",
        "constraints",
        "targets_after_constraints",
        "rebalance_plan",
        "orders",
        "fills",
        "ledger",
        "report",
    ]
    fill_lineage = await coordinator.lineage(manifest.run_id, artifacts[8].trace_id)
    assert [item.stage.value for item in fill_lineage] == [
        "universe",
        "features",
        "signals",
        "targets_before_constraints",
        "constraints",
        "targets_after_constraints",
        "rebalance_plan",
        "orders",
        "fills",
    ]


@pytest.mark.asyncio
async def test_idempotent_retry_does_not_duplicate_artifacts(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory()
    decision = decision_factory()
    adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=fixed_report("ma_cross", decision),
    )
    store = InMemoryResearchRunStore()
    coordinator = ResearchRunCoordinator(store)

    first = await coordinator.execute(manifest, adapter)
    second = await coordinator.execute(manifest, adapter)

    assert second is first
    assert len(await store.list_artifacts(manifest.run_id)) == 11


@pytest.mark.asyncio
async def test_same_frozen_version_replay_is_deterministic(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory()
    decision = decision_factory()
    adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=fixed_report("ma_cross", decision),
    )
    store = InMemoryResearchRunStore()
    coordinator = ResearchRunCoordinator(store)
    source = await coordinator.execute(manifest, adapter)

    replay = await coordinator.replay(
        source_run_id=manifest.run_id,
        new_run_id="RR-replay-run-0000001",
        idempotency_key="replay-idempotency-0001",
        requested_by="unit-test",
        adapter=adapter,
    )

    assert replay.status is ResearchRunStatus.COMPLETED
    assert replay.result_checksum == source.result_checksum
    assert replay.manifest.replay_of_run_id == manifest.run_id


@pytest.mark.asyncio
async def test_same_frozen_version_replay_fails_on_result_drift(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory()
    decision = decision_factory()
    source_adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=fixed_report("ma_cross", decision),
    )
    drifted_adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=replace(fixed_report("ma_cross", decision), sharpe_ratio=9.9),
    )
    coordinator = ResearchRunCoordinator(InMemoryResearchRunStore())
    await coordinator.execute(manifest, source_adapter)

    replay = await coordinator.replay(
        source_run_id=manifest.run_id,
        new_run_id="RR-replay-drift-000001",
        idempotency_key="replay-drift-idempotency",
        requested_by="unit-test",
        adapter=drifted_adapter,
    )

    assert replay.status is ResearchRunStatus.FAILED
    assert replay.error_code == "non_deterministic_replay"


@pytest.mark.asyncio
async def test_restart_marks_running_then_resumes_from_idempotent_checkpoint(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory()
    decision = decision_factory()
    adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=fixed_report("ma_cross", decision),
    )
    store = InMemoryResearchRunStore()
    await store.create_or_get(manifest)
    await store.transition(
        manifest.run_id,
        expected=frozenset({ResearchRunStatus.QUEUED}),
        target=ResearchRunStatus.RUNNING,
    )
    coordinator = ResearchRunCoordinator(store)

    interrupted = await coordinator.mark_stale_running_as_interrupted()
    resumed = await coordinator.execute(manifest, adapter)

    assert interrupted[0].status in {
        ResearchRunStatus.INTERRUPTED,
        ResearchRunStatus.COMPLETED,
    }
    assert resumed.status is ResearchRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_missing_dataset_capability_fails_closed(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory(capabilities=("stock",))
    decision = decision_factory()
    adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=fixed_report("ma_cross", decision),
        required_capabilities=("futures_events",),
    )

    record = await ResearchRunCoordinator(
        InMemoryResearchRunStore()
    ).execute(manifest, adapter)

    assert record.status is ResearchRunStatus.REJECTED
    assert record.error_code == "unsupported_capability"


@pytest.mark.asyncio
async def test_partial_fill_is_reflected_in_position_and_shortfall(
    manifest_factory, decision_factory
) -> None:
    decision = decision_factory(
        quantity=Decimal("50"),
        order_quantity=Decimal("100"),
        cash=Decimal("95000"),
        market_value=Decimal("5000"),
    )
    report = replace(
        fixed_report("ma_cross", decision),
        fill_shortfall=Decimal("5000"),
    )
    decision = replace(
        decision,
        ledger=replace(decision.ledger, fill_shortfall=Decimal("5000")),
    )
    adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(decision,),
        report=report,
    )

    record = await ResearchRunCoordinator(
        InMemoryResearchRunStore()
    ).execute(manifest_factory(), adapter)

    assert record.status is ResearchRunStatus.COMPLETED
    assert record.result is not None
    assert record.result.fill_shortfall == Decimal("5000")


@pytest.mark.asyncio
async def test_strategy_cannot_mutate_position_without_fill(
    manifest_factory, decision_factory
) -> None:
    decision = decision_factory()
    mutated = replace(decision, fills=())
    adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(mutated,),
        report=replace(fixed_report("ma_cross", mutated), fill_count=0),
    )

    record = await ResearchRunCoordinator(
        InMemoryResearchRunStore()
    ).execute(manifest_factory(), adapter)

    assert record.status is ResearchRunStatus.FAILED
    assert "持仓必须由成交驱动" in (record.error_summary or "")


@pytest.mark.asyncio
async def test_duplicate_fill_after_restart_cannot_double_account(
    manifest_factory, decision_factory
) -> None:
    first = decision_factory()
    second = decision_factory(
        index=1,
        fill_id=first.fills[0].research_fill_id,
        positions=(
            ResearchPosition(
                symbol="510300.SH",
                position_side=ResearchPositionSide.LONG,
                quantity=Decimal("200"),
                average_price=Decimal("100"),
                market_price=Decimal("100"),
                market_value=Decimal("20000"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            ),
        ),
        cash=Decimal("80000"),
        market_value=Decimal("20000"),
    )
    report = replace(
        fixed_report("ma_cross", second),
        decision_count=2,
        order_count=2,
        fill_count=2,
    )
    adapter = DecisionSequenceAdapter(
        strategy_kind="ma_cross",
        decisions=(first, second),
        report=report,
    )

    record = await ResearchRunCoordinator(
        InMemoryResearchRunStore()
    ).execute(manifest_factory(), adapter)

    assert record.status is ResearchRunStatus.FAILED
    assert "重复成交" in (record.error_summary or "")


@pytest.mark.asyncio
async def test_queued_run_can_be_cancelled(manifest_factory) -> None:
    store = InMemoryResearchRunStore()
    manifest = manifest_factory()
    await store.create_or_get(manifest)

    record = await ResearchRunCoordinator(store).cancel(manifest.run_id)

    assert record.status is ResearchRunStatus.CANCELLED


@pytest.mark.asyncio
async def test_duplicate_worker_does_not_fail_active_run(
    manifest_factory, decision_factory
) -> None:
    manifest = manifest_factory()
    store = InMemoryResearchRunStore()
    await store.create_or_get(manifest)
    active = await store.transition(
        manifest.run_id,
        expected=frozenset({ResearchRunStatus.QUEUED}),
        target=ResearchRunStatus.RUNNING,
    )
    decision = decision_factory()

    duplicate = await ResearchRunCoordinator(store).execute(
        manifest,
        DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=(decision,),
            report=fixed_report("ma_cross", decision),
        ),
    )

    assert duplicate is active
    assert duplicate.status is ResearchRunStatus.RUNNING
    assert duplicate.error_code is None


@pytest.mark.asyncio
async def test_risk_rejection_remains_order_evidence_without_position(
    manifest_factory, decision_factory
) -> None:
    original = decision_factory()
    rejected_order = replace(
        original.orders[0],
        status=ResearchOrderStatus.REJECTED,
        reject_reason="risk: cash buffer",
    )
    rejected = replace(
        original,
        constraints=(
            ConstraintOutcome(
                constraint="cash_buffer",
                passed=False,
                before_value=0.0,
                after_value=1.0,
                limit=0.05,
                reason="现金缓冲不足,目标仓位归零",
            ),
        ),
        targets_after_constraints=(),
        orders=(rejected_order,),
        fills=(),
        positions=(),
        ledger=replace(
            original.ledger,
            cash=Decimal("100000"),
            market_value=Decimal("0"),
            equity=Decimal("100000"),
        ),
    )
    report = replace(
        fixed_report("ma_cross", rejected),
        fill_count=0,
        constraint_impact={"cash_buffer": -0.1},
    )

    store = InMemoryResearchRunStore()
    record = await ResearchRunCoordinator(store).execute(
        manifest_factory(),
        DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=(rejected,),
            report=report,
        ),
    )

    assert record.status is ResearchRunStatus.COMPLETED
    order_artifact = (await store.list_artifacts(record.manifest.run_id))[7]
    orders = order_artifact.payload["orders"]
    assert isinstance(orders, list)
    first_order = orders[0]
    assert isinstance(first_order, dict)
    assert first_order["reject_reason"] == "risk: cash buffer"


@pytest.mark.asyncio
async def test_strategy_exception_is_persisted_as_failed(manifest_factory) -> None:
    class StrategyFailureAdapter:
        strategy_kind = "ma_cross"

        def validate_manifest(self, manifest) -> None:
            del manifest

        async def decisions(self, manifest):
            del manifest
            raise ValueError("strategy calculation failed")
            yield  # pragma: no cover

        def build_report(self, manifest, decisions):
            del manifest, decisions
            raise AssertionError("report must not be called")

    record = await ResearchRunCoordinator(
        InMemoryResearchRunStore()
    ).execute(manifest_factory(), StrategyFailureAdapter())

    assert record.status is ResearchRunStatus.FAILED
    assert record.error_code == "ValueError"
    assert record.error_summary == "strategy calculation failed"


@pytest.mark.asyncio
async def test_timeout_interrupts_after_checkpoint_and_resume_is_idempotent(
    manifest_factory, decision_factory
) -> None:
    decision = decision_factory()

    class TimeoutAdapter:
        strategy_kind = "ma_cross"

        def validate_manifest(self, manifest) -> None:
            del manifest

        async def decisions(self, manifest):
            del manifest
            yield decision
            raise ResearchRunInterruptedError("strategy timeout")

        def build_report(self, manifest, decisions):
            del manifest, decisions
            raise AssertionError("report must not be called")

    manifest = manifest_factory()
    store = InMemoryResearchRunStore()
    coordinator = ResearchRunCoordinator(store)
    interrupted = await coordinator.execute(manifest, TimeoutAdapter())
    assert interrupted.status is ResearchRunStatus.INTERRUPTED
    assert len(await store.list_artifacts(manifest.run_id)) == 10

    resumed = await coordinator.execute(
        manifest,
        DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=(decision,),
            report=fixed_report("ma_cross", decision),
        ),
    )
    assert resumed.status is ResearchRunStatus.COMPLETED
    assert len(await store.list_artifacts(manifest.run_id)) == 11


@pytest.mark.asyncio
async def test_persistence_fault_does_not_mark_run_complete(
    manifest_factory, decision_factory
) -> None:
    class FaultingStore(InMemoryResearchRunStore):
        async def append_artifact(self, artifact):
            del artifact
            raise OSError("checkpoint storage unavailable")

    decision = decision_factory()
    record = await ResearchRunCoordinator(FaultingStore()).execute(
        manifest_factory(),
        DecisionSequenceAdapter(
            strategy_kind="ma_cross",
            decisions=(decision,),
            report=fixed_report("ma_cross", decision),
        ),
    )

    assert record.status is ResearchRunStatus.FAILED
    assert record.error_code == "OSError"
