"""#91 正式组合流水线接入 ResearchRun 的端到端回归。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import numpy as np
import pytest

from finboard_backtest.portfolio import AssetLotInfo, CovarianceEstimate
from finboard_backtest.research_run import (
    DecisionBundle,
    FeatureValue,
    InMemoryResearchRunStore,
    NormalizedSignal,
    ResearchFillAction,
    ResearchOrderStatus,
    ResearchRunCoordinator,
    ResearchRunManifest,
    ResearchRunStatus,
    UniverseCandidate,
    stable_checksum,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
)
from finboard_backtest.strategy_spec import (
    RiskExitPolicy,
    RiskExitRule,
    RiskExitType,
)

SYMBOLS = ("A.SH", "B.SH", "C.SH")


async def _collect_decisions(
    adapter: PortfolioPipelineAdapter,
    manifest: ResearchRunManifest,
) -> list[DecisionBundle]:
    adapter.validate_manifest(manifest)
    return [decision async for decision in adapter.decisions(manifest)]


def _covariance() -> CovarianceEstimate:
    return CovarianceEstimate(
        matrix=np.diag([0.09, 0.01, 0.01]),
        tickers=list(SYMBOLS),
        shrinkage=0.0,
        n_observations=252,
    )


def _manifest(manifest_factory, *, run_id: str = "RR-pipeline-source"):
    manifest = manifest_factory(
        run_id=run_id,
        idempotency_key=f"idempotency-{run_id}",
    )
    return replace(
        manifest,
        portfolio_config={
            "overrides": {
                "max_weight_per_asset": 0.50,
                "max_weight_per_sleeve": 1.0,
                "max_risk_contribution": 0.40,
            }
        },
    )


def _input(
    *,
    day: int = 2,
    covariance: CovarianceEstimate | None = None,
    prices: dict[str, float] | None = None,
    fill_ratios: dict[str, float] | None = None,
    rejected: frozenset[str] = frozenset(),
) -> PortfolioDecisionInput:
    business_date = date(2025, 1, day)
    decision_at = datetime(2025, 1, day, 15, tzinfo=UTC)
    execution_at = datetime(2025, 1, day + 1, 9, 30, tzinfo=UTC)
    mark_prices = prices or dict.fromkeys(SYMBOLS, 10.0)
    return PortfolioDecisionInput(
        business_date=business_date,
        decision_at=decision_at,
        execution_at=execution_at,
        candidates=tuple(
            UniverseCandidate(
                symbol=symbol,
                included=True,
                reasons=("通过冻结候选池规则",),
                asset_class="equity",
                market="a_share",
            )
            for symbol in SYMBOLS
        ),
        features=tuple(
            FeatureValue(
                symbol=symbol,
                feature_id="close",
                value=mark_prices[symbol],
                source_artifact_ids=("release-v1",),
                available_at=decision_at,
            )
            for symbol in SYMBOLS
        ),
        signals=tuple(
            NormalizedSignal(
                symbol=symbol,
                score=1.0,
                action="buy",
                rule_id="fixed-research-signal",
                factor_snapshot_id="factor-v1",
                rationale="冻结输入中的标准化多头信号",
            )
            for symbol in SYMBOLS
        ),
        prices=mark_prices,
        execution_prices=mark_prices,
        lot_info={
            symbol: AssetLotInfo(code=symbol, lot_size=100)
            for symbol in SYMBOLS
        },
        input_artifact_ids=("release-v1", "factor-v1"),
        covariance=covariance,
        sleeve_map=dict.fromkeys(SYMBOLS, "equity"),
        fill_ratio_by_symbol=fill_ratios or {},
        rejected_symbols=rejected,
    )


@pytest.mark.asyncio
async def test_formal_pipeline_generates_complete_research_lifecycle(
    manifest_factory,
) -> None:
    manifest = _manifest(manifest_factory)
    adapter = PortfolioPipelineAdapter(
        strategy_kind="ma_cross",
        decision_inputs=(
            _input(
                covariance=_covariance(),
                fill_ratios={"B.SH": 0.5},
                rejected=frozenset({"C.SH"}),
            ),
        ),
    )
    store = InMemoryResearchRunStore()
    decision = (await _collect_decisions(adapter, manifest))[0]

    record = await ResearchRunCoordinator(store).execute(manifest, adapter)

    assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
    artifacts = await store.list_artifacts(manifest.run_id)
    assert len(artifacts) == 14
    risk_cap = next(
        item
        for item in decision.constraints
        if item.constraint == "max_risk_contribution"
    )
    assert risk_cap.hard is True
    assert risk_cap.passed is True
    assert risk_cap.before_value is not None
    assert risk_cap.before_value > 0.80
    assert risk_cap.after_value is not None
    assert risk_cap.after_value <= 0.40 + 1e-9

    assert {item.tier for item in decision.capital_feasibility} == {
        "100k",
        "200k",
        "500k",
    }
    assert len(
        {item.input_checksum for item in decision.capital_feasibility}
    ) == 1

    assert all(
        item.research_order_id.startswith("RR-PIPE-")
        for item in decision.orders
    )
    assert {item.status.value for item in decision.orders} == {
        ResearchOrderStatus.FILLED.value,
        ResearchOrderStatus.PARTIALLY_FILLED.value,
        ResearchOrderStatus.REJECTED.value,
    }
    assert len(decision.fills) == 2
    assert {item.symbol for item in decision.positions} == {"A.SH", "B.SH"}


@pytest.mark.asyncio
async def test_formal_pipeline_replay_is_deterministic(manifest_factory) -> None:
    manifest = _manifest(manifest_factory)
    adapter = PortfolioPipelineAdapter(
        strategy_kind="ma_cross",
        decision_inputs=(_input(covariance=_covariance()),),
    )
    store = InMemoryResearchRunStore()
    coordinator = ResearchRunCoordinator(store)
    source = await coordinator.execute(manifest, adapter)

    replay = await coordinator.replay(
        source_run_id=manifest.run_id,
        new_run_id="RR-pipeline-replay",
        idempotency_key="pipeline-replay-idempotency",
        requested_by="unit-test",
        adapter=adapter,
    )

    assert source.status is ResearchRunStatus.COMPLETED, source.error_summary
    assert replay.status is ResearchRunStatus.COMPLETED, replay.error_summary
    assert replay.result_checksum == source.result_checksum
    assert replay.manifest.input_checksum == source.manifest.input_checksum


@pytest.mark.asyncio
async def test_missing_covariance_rejects_before_research_orders(
    manifest_factory,
) -> None:
    manifest = _manifest(manifest_factory)
    store = InMemoryResearchRunStore()

    record = await ResearchRunCoordinator(store).execute(
        manifest,
        PortfolioPipelineAdapter(
            strategy_kind="ma_cross",
            decision_inputs=(_input(covariance=None),),
        ),
    )

    assert record.status is ResearchRunStatus.REJECTED
    assert record.error_code == "hard_constraint_rejected"
    assert "风险贡献硬约束缺少" in (record.error_summary or "")
    assert await store.list_artifacts(manifest.run_id) == []


@pytest.mark.asyncio
async def test_risk_exit_uses_filled_position_and_persists_state(
    manifest_factory,
) -> None:
    manifest = _manifest(manifest_factory, run_id="RR-pipeline-risk-exit")
    risk_policy = RiskExitPolicy(
        rules=(
            RiskExitRule(
                rule_type=RiskExitType.PRICE_STOP_LOSS,
                enabled=True,
                threshold=0.10,
                rationale="亏损 10% 后退出",
            ),
            RiskExitRule(
                rule_type=RiskExitType.COOLDOWN,
                enabled=True,
                days=5,
                rationale="止损后冷却五日",
            ),
        )
    )
    strategy_spec = manifest.strategy_spec.model_copy(
        update={"risk_exit_policy": risk_policy}
    )
    manifest = replace(
        manifest,
        strategy_spec=strategy_spec,
        strategy_spec_checksum=stable_checksum(strategy_spec.canonical_payload()),
    )
    second_prices = {"A.SH": 8.0, "B.SH": 10.0, "C.SH": 10.0}
    adapter = PortfolioPipelineAdapter(
        strategy_kind="ma_cross",
        decision_inputs=(
            _input(covariance=_covariance()),
            _input(day=3, covariance=_covariance(), prices=second_prices),
        ),
    )
    store = InMemoryResearchRunStore()
    decisions = await _collect_decisions(adapter, manifest)

    record = await ResearchRunCoordinator(store).execute(manifest, adapter)

    assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
    artifacts = await store.list_artifacts(manifest.run_id)
    assert len(artifacts) == 27
    second = decisions[1]
    assert any(
        item.rule_type == RiskExitType.PRICE_STOP_LOSS.value
        and item.symbol == "A.SH"
        and item.triggered
        for item in second.risk_exits
    )
    assert "A.SH" in second.risk_state.cooldown_until
    assert "A.SH" not in {item.symbol for item in second.targets_after_risk}
    assert any(
        item.symbol == "A.SH"
        and item.action is ResearchFillAction.CLOSE_LONG
        for item in second.fills
    )
