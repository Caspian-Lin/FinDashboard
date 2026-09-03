"""issue #266:风险因子中性化 + max_ir 经 ResearchRun 正式组合流水线端到端。

覆盖:portfolio_config.overrides 声明 ``risk_factor_limits`` → 冻结特征
提取暴露观测 → build_portfolio 只减仓投影 → 约束审计行落 DecisionBundle;
暴露缺失降级可见;``AllocationMethod.MAX_IR`` 规格枚举注册与向后兼容。
(builder 层的不可满足 fail-closed 用例见
tests/unit/portfolio/test_issue_266_neutralization.py —— 现金基准下
long-only 只减仓恒可投影到 0,不可行只在显式基准下出现,属 builder 级语义。)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import cast

import numpy as np
import pytest

from finboard_backtest.portfolio import AssetLotInfo, CovarianceEstimate
from finboard_backtest.research_run import (
    FeatureValue,
    InMemoryResearchRunStore,
    NormalizedSignal,
    ResearchRunCoordinator,
    ResearchRunManifest,
    ResearchRunStatus,
    UniverseCandidate,
    stable_checksum,
)
from finboard_backtest.research_run.contracts import JsonValue
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
    _allocation_method,
    _risk_factor_limits,
)
from finboard_backtest.strategy_spec.contracts import AllocationMethod

SYMBOLS = ("A.SH", "B.SH", "C.SH")
BETAS = {"A.SH": 1.2, "B.SH": 1.0, "C.SH": 0.8}


def _covariance() -> CovarianceEstimate:
    return CovarianceEstimate(
        matrix=np.diag([0.04, 0.01, 0.02]),
        tickers=list(SYMBOLS),
        shrinkage=0.0,
        n_observations=252,
    )


def _manifest_with(
    manifest_factory: Callable[..., ResearchRunManifest],
    *,
    overrides: dict[str, object] | None = None,
    allocation_method: AllocationMethod | None = None,
    run_id: str = "RR-266-neutralization",
) -> ResearchRunManifest:
    manifest = manifest_factory(
        run_id=run_id,
        idempotency_key=f"idempotency-{run_id}",
    )
    if allocation_method is not None:
        policy = manifest.strategy_spec.portfolio_policy.model_copy(
            update={"allocation_method": allocation_method}
        )
        spec = manifest.strategy_spec.model_copy(update={"portfolio_policy": policy})
        manifest = replace(
            manifest,
            strategy_spec=spec,
            strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        )
    if overrides:
        manifest = replace(
            manifest,
            portfolio_config=cast(
                dict[str, JsonValue], {"overrides": overrides}
            ),
        )
    return manifest


def _input(
    *,
    market_beta: dict[str, float] | None = None,
    covariance: CovarianceEstimate | None = None,
    day: int = 2,
) -> PortfolioDecisionInput:
    decision_at = datetime(2025, 1, day, 15, tzinfo=UTC)
    mark_prices = dict.fromkeys(SYMBOLS, 10.0)
    feature_values = [
        FeatureValue(
            symbol=symbol,
            feature_id="close",
            value=mark_prices[symbol],
            source_artifact_ids=("release-v1",),
            available_at=decision_at,
        )
        for symbol in SYMBOLS
    ]
    for symbol, value in (market_beta or {}).items():
        feature_values.append(
            FeatureValue(
                symbol=symbol,
                feature_id="market_beta",
                value=value,
                source_artifact_ids=("release-v1",),
                available_at=decision_at,
            )
        )
    return PortfolioDecisionInput(
        business_date=date(2025, 1, day),
        decision_at=decision_at,
        execution_at=datetime(2025, 1, day + 1, 9, 30, tzinfo=UTC),
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
        features=tuple(feature_values),
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
        lot_info={symbol: AssetLotInfo(code=symbol, lot_size=100) for symbol in SYMBOLS},
        input_artifact_ids=("release-v1", "factor-v1"),
        covariance=covariance,
        sleeve_map=dict.fromkeys(SYMBOLS, "equity"),
    )


class TestAllocationMethodRegistration:
    def test_max_ir_enum_value_registered(self) -> None:
        assert AllocationMethod.MAX_IR.value == "max_ir"
        assert AllocationMethod("max_ir") is AllocationMethod.MAX_IR

    def test_mapping_routes_max_ir(self, manifest_factory) -> None:
        manifest = _manifest_with(
            manifest_factory, allocation_method=AllocationMethod.MAX_IR
        )
        assert _allocation_method(manifest) == "max_ir"

    def test_override_string_routes_max_ir(self, manifest_factory) -> None:
        """overrides.allocation_method 字符串直通(既有行为),max_ir 同样可用。"""
        manifest = _manifest_with(
            manifest_factory, overrides={"allocation_method": "max_ir"}
        )
        assert _allocation_method(manifest) == "max_ir"

    def test_legacy_methods_unchanged(self, manifest_factory) -> None:
        """旧枚举映射零漂移(向后兼容锚点)。"""
        manifest = _manifest_with(manifest_factory)
        assert _allocation_method(manifest) == "equal_weight"

    def test_spec_serialization_stable(self, manifest_factory) -> None:
        """旧规格 payload/checksum 不受新增枚举成员影响。"""
        manifest = manifest_factory()
        payload = manifest.strategy_spec.canonical_payload()
        assert payload["portfolio_policy"]["allocation_method"] == "equal_weight"
        assert stable_checksum(payload) == manifest.strategy_spec_checksum


class TestRiskFactorLimitsParsing:
    def test_parse_valid_limits(self) -> None:
        limits = _risk_factor_limits(
            {
                "risk_factor_limits": [
                    {"factor": "market_beta", "max_active_exposure": 0.05},
                    {"factor": "size_exposure", "max_active_exposure": 0.2},
                ]
            }
        )
        assert [item.factor for item in limits] == ["market_beta", "size_exposure"]
        assert limits[0].max_active_exposure == pytest.approx(0.05)

    def test_absent_returns_empty(self) -> None:
        assert _risk_factor_limits({}) == ()
        assert _risk_factor_limits({"risk_factor_limits": None}) == ()

    @pytest.mark.parametrize(
        "raw",
        [
            "not-a-list",
            [{"factor": "market_beta"}],
            [{"max_active_exposure": 0.1}],
            [{"factor": "market_beta", "max_active_exposure": "free"}],
            [{"factor": "market_beta", "max_active_exposure": -0.1}],
            [{"factor": "", "max_active_exposure": 0.1}],
        ],
    )
    def test_malformed_rejected(self, raw: object) -> None:
        with pytest.raises(ValueError, match="risk_factor_limits"):
            _risk_factor_limits({"risk_factor_limits": raw})

    def test_duplicate_factor_rejected_by_constraints(self) -> None:
        """因子名重复由 PortfolioConstraints 契约层拒绝(解析层放行)。"""
        from finboard_backtest.portfolio import PortfolioConstraints

        limits = _risk_factor_limits(
            {
                "risk_factor_limits": [
                    {"factor": "market_beta", "max_active_exposure": 0.1},
                    {"factor": "market_beta", "max_active_exposure": 0.2},
                ]
            }
        )
        with pytest.raises(ValueError, match="不允许重复"):
            PortfolioConstraints(risk_factor_limits=limits)


class TestNeutralizationPipeline:
    @pytest.mark.asyncio
    async def test_constraint_enforced_from_frozen_features(
        self, manifest_factory
    ) -> None:
        """声明上限 + 特征齐备:投影执行,审计行 hard/passed,暴露在限内。"""
        manifest = _manifest_with(
            manifest_factory,
            overrides={
                "risk_factor_limits": [
                    {"factor": "market_beta", "max_active_exposure": 0.05},
                ],
                "max_weight_per_sleeve": 1.0,
            },
        )
        adapter = PortfolioPipelineAdapter(
            strategy_kind="ma_cross",
            decision_inputs=(
                _input(market_beta=dict(BETAS), covariance=_covariance()),
            ),
        )
        decisions = [decision async for decision in adapter.decisions(manifest)]
        assert len(decisions) == 1
        row = next(
            item
            for item in decisions[0].constraints
            if item.constraint == "risk_factor_neutralization"
        )
        assert row.hard is True
        assert row.passed is True
        assert row.before_value is not None
        assert row.before_value > 0.05
        assert row.after_value is not None
        assert row.after_value <= 0.05 + 1e-9
        # 目标权重的 active 暴露独立复核(现金基准 → active = w)。
        weights = {
            item.symbol: float(item.weight)
            for item in decisions[0].targets_after_constraints
        }
        exposure = sum(weights.get(symbol, 0.0) * beta for symbol, beta in BETAS.items())
        assert exposure <= 0.05 + 1e-9

    @pytest.mark.asyncio
    async def test_missing_observations_degrade_visibly(
        self, manifest_factory
    ) -> None:
        """特征缺失:skipped 软约束行(具名 warning),构建不失败不静默。"""
        manifest = _manifest_with(
            manifest_factory,
            overrides={
                "risk_factor_limits": [
                    {"factor": "market_beta", "max_active_exposure": 0.05},
                ],
            },
            run_id="RR-266-missing-features",
        )
        adapter = PortfolioPipelineAdapter(
            strategy_kind="ma_cross",
            decision_inputs=(_input(covariance=_covariance()),),
        )
        decisions = [decision async for decision in adapter.decisions(manifest)]
        row = next(
            item
            for item in decisions[0].constraints
            if item.constraint == "risk_factor_neutralization_skipped"
        )
        assert row.hard is False
        assert row.passed is False
        assert "factor_neutralization_inactive" in row.reason
        assert "market_beta" in row.reason

    @pytest.mark.asyncio
    async def test_end_to_end_run_completes_with_audit_artifact(
        self, manifest_factory
    ) -> None:
        """走完整 coordinator:COMPLETED,约束审计进 14 stage artifacts。"""
        manifest = _manifest_with(
            manifest_factory,
            overrides={
                "risk_factor_limits": [
                    {"factor": "market_beta", "max_active_exposure": 0.05},
                ],
                "max_weight_per_sleeve": 1.0,
            },
            run_id="RR-266-e2e",
        )
        adapter = PortfolioPipelineAdapter(
            strategy_kind="ma_cross",
            decision_inputs=(
                _input(market_beta=dict(BETAS), covariance=_covariance()),
            ),
        )
        store = InMemoryResearchRunStore()
        record = await ResearchRunCoordinator(store).execute(manifest, adapter)
        assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
        constraint_artifacts = [
            artifact
            for artifact in await store.list_artifacts(manifest.run_id)
            if artifact.stage == "constraints"
        ]
        assert constraint_artifacts, "constraints artifact 应落库"
        payload = cast(dict[str, object], constraint_artifacts[0].payload)
        outcomes = cast(
            list[dict[str, object]], payload.get("constraints", [])
        )
        assert any(
            item.get("constraint") == "risk_factor_neutralization"
            and item.get("passed") is True
            for item in outcomes
        )


class TestMaxIrThroughPipeline:
    @pytest.mark.asyncio
    async def test_max_ir_manifest_completes_and_replays(self, manifest_factory) -> None:
        """规格声明 allocation_method=max_ir:端到端可运行且 replay 确定性一致。"""
        manifest = _manifest_with(
            manifest_factory,
            overrides={"max_weight_per_sleeve": 1.0},
            allocation_method=AllocationMethod.MAX_IR,
            run_id="RR-266-max-ir",
        )
        adapter = PortfolioPipelineAdapter(
            strategy_kind="ma_cross",
            decision_inputs=(
                _input(covariance=_covariance()),
                _input(covariance=_covariance(), day=3),
            ),
        )
        store = InMemoryResearchRunStore()
        coordinator = ResearchRunCoordinator(store)
        record = await coordinator.execute(manifest, adapter)
        assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
        replay = await coordinator.replay(
            source_run_id=manifest.run_id,
            new_run_id="RR-266-max-ir-replay",
            idempotency_key="idempotency-266-replay",
            requested_by="unit-test",
            adapter=adapter,
        )
        assert replay.status is ResearchRunStatus.COMPLETED
        assert replay.result_checksum == record.result_checksum
