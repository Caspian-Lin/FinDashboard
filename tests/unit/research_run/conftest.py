"""ResearchRun fixed-sample builders."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finboard_backtest.research_run import (
    ConstraintOutcome,
    DecisionBundle,
    FeatureValue,
    FrozenArtifactRef,
    LedgerSnapshot,
    NormalizedSignal,
    RebalanceInstruction,
    ResearchActorType,
    ResearchFill,
    ResearchFillAction,
    ResearchOrder,
    ResearchOrderStatus,
    ResearchPosition,
    ResearchPositionSide,
    ResearchRunManifest,
    ResearchRunReport,
    TargetPosition,
    UniverseCandidate,
    stable_checksum,
)
from finboard_backtest.strategy_spec import build_strategy_template


@pytest.fixture
def manifest_factory():
    def build(
        kind: str = "ma_cross",
        *,
        run_id: str = "RR-test-run-00000001",
        idempotency_key: str = "test-idempotency-0001",
        capabilities: tuple[str, ...] | None = None,
    ) -> ResearchRunManifest:
        default_capabilities = {
            "ma_cross": ("stock",),
            "multi_factor": ("stock",),
            "etf_rotation": ("etf:index",),
            "mean_reversion": ("etf:index",),
            "convertible_double_low": ("convertible",),
            "futures_tsmom": ("futures",),
        }
        spec = build_strategy_template(
            kind,
            strategy_id=f"{kind}_research",
            dataset_release_ids=("release-v1",),
        )
        return ResearchRunManifest(
            run_id=run_id,
            idempotency_key=idempotency_key,
            strategy_spec=spec,
            strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
            dataset_releases=(
                FrozenArtifactRef(
                    artifact_id="release-v1",
                    version="2026-01-01",
                    checksum="a" * 64,
                    capabilities=capabilities or default_capabilities[kind],
                ),
            ),
            factor_snapshots=(
                FrozenArtifactRef(
                    artifact_id="factor-v1",
                    version="v1",
                    checksum="b" * 64,
                    capabilities=("factor:close",),
                ),
            ),
            code_version="abcdef0123456789",
            initial_capital=Decimal("100000"),
            requested_by="unit-test",
            actor_type=ResearchActorType.HUMAN,
        )

    return build


@pytest.fixture
def decision_factory():
    def build(
        *,
        index: int = 0,
        action: ResearchFillAction = ResearchFillAction.OPEN_LONG,
        quantity: Decimal = Decimal("100"),
        order_quantity: Decimal | None = None,
        positions: tuple[ResearchPosition, ...] | None = None,
        cash: Decimal = Decimal("90000"),
        market_value: Decimal = Decimal("10000"),
        fees: Decimal = Decimal("0"),
        fill_id: str | None = None,
        order_status: ResearchOrderStatus = ResearchOrderStatus.FILLED,
    ) -> DecisionBundle:
        side = (
            ResearchPositionSide.SHORT
            if action in {
                ResearchFillAction.OPEN_SHORT,
                ResearchFillAction.CLOSE_SHORT,
            }
            else ResearchPositionSide.LONG
        )
        decision_date = date(2024, 1, 2 + index)
        timestamp = datetime(2024, 1, 2 + index, 15, tzinfo=UTC)
        signed_weight = -0.1 if side is ResearchPositionSide.SHORT else 0.1
        order_id = f"RR-order-{index:04d}"
        instruction_id = f"RR-instruction-{index:04d}"
        actual_positions = positions
        if actual_positions is None:
            actual_positions = (
                ResearchPosition(
                    symbol="510300.SH",
                    position_side=side,
                    quantity=quantity,
                    average_price=Decimal("100"),
                    market_price=Decimal("100"),
                    market_value=market_value,
                    realized_pnl=Decimal("0"),
                    unrealized_pnl=Decimal("0"),
                ),
            )
        return DecisionBundle(
            business_date=decision_date,
            decision_at=timestamp,
            candidates=(
                UniverseCandidate(
                    symbol="510300.SH",
                    included=True,
                    reasons=("数据完整且通过流动性门槛",),
                    asset_class="equity",
                    market="a_share",
                ),
                UniverseCandidate(
                    symbol="000001.SZ",
                    included=False,
                    reasons=("成交额低于阈值",),
                    asset_class="equity",
                    market="a_share",
                ),
            ),
            features=(
                FeatureValue(
                    symbol="510300.SH",
                    feature_id="close",
                    value=100.0,
                    source_artifact_ids=("release-v1",),
                    available_at=timestamp,
                ),
            ),
            signals=(
                NormalizedSignal(
                    symbol="510300.SH",
                    score=1.0,
                    action="buy" if side is ResearchPositionSide.LONG else "sell",
                    rule_id="fixed_sample",
                    factor_snapshot_id="factor-v1",
                    rationale="固定样本信号",
                ),
            ),
            targets_before_constraints=(
                TargetPosition(
                    symbol="510300.SH",
                    weight=signed_weight,
                    position_side=side,
                ),
            ),
            constraints=(
                ConstraintOutcome(
                    constraint="lot_size",
                    passed=True,
                    before_value=100.0,
                    after_value=100.0,
                    limit=100.0,
                    reason="数量符合交易单位",
                ),
            ),
            targets_after_constraints=(
                TargetPosition(
                    symbol="510300.SH",
                    weight=signed_weight,
                    position_side=side,
                ),
            ),
            rebalance_plan=(
                RebalanceInstruction(
                    instruction_id=instruction_id,
                    symbol="510300.SH",
                    action=action,
                    target_quantity=quantity,
                    current_quantity=Decimal("0"),
                    delta_quantity=quantity,
                    lot_size=1,
                    estimated_value=market_value,
                    reason="目标权重离散化",
                ),
            ),
            orders=(
                ResearchOrder(
                    research_order_id=order_id,
                    instruction_id=instruction_id,
                    symbol="510300.SH",
                    action=action,
                    quantity=order_quantity or quantity,
                    status=order_status,
                ),
            ),
            fills=(
                ResearchFill(
                    research_fill_id=fill_id or f"RR-fill-{index:04d}",
                    research_order_id=order_id,
                    symbol="510300.SH",
                    action=action,
                    quantity=quantity,
                    price=Decimal("100"),
                    filled_at=datetime(2024, 1, 3 + index, 9, 30, tzinfo=UTC),
                ),
            ),
            positions=actual_positions,
            ledger=LedgerSnapshot(
                cash=cash,
                market_value=market_value,
                margin_used=Decimal("0"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                equity=cash + market_value,
                fees_paid=fees,
                tax_paid=Decimal("0"),
                slippage_paid=Decimal("0"),
            ),
        )

    return build


def fixed_report(kind: str, decision: DecisionBundle) -> ResearchRunReport:
    return ResearchRunReport(
        strategy_kind=kind,
        strategy_return=0.0,
        benchmark_symbol="510300.SH",
        benchmark_return=0.0,
        excess_return=0.0,
        sharpe_ratio=0.0,
        max_drawdown=0.0,
        final_equity=decision.ledger.equity,
        final_cash=decision.ledger.cash,
        commission_paid=decision.ledger.fees_paid,
        tax_paid=decision.ledger.tax_paid,
        slippage_paid=decision.ledger.slippage_paid,
        fill_shortfall=decision.ledger.fill_shortfall,
        constraint_impact={"lot_size": 0.0},
        decision_count=1,
        order_count=len(decision.orders),
        fill_count=len(decision.fills),
    )
