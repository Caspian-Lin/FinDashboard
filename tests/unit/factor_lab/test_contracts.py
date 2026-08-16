"""Feature → FactorSignal 与实验状态机契约测试(issue #78)。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from finboard_data.factor_lab import (
    ArtifactIntegrityError,
    FactorExperimentPlan,
    FactorExperimentStatus,
    FeatureObservation,
    FeatureSnapshot,
    PointInTimeViolationError,
    ResearchArtifactStatus,
    SignalNotValidatedError,
    build_factor_signal,
    build_feature_snapshot,
    factor_lab_catalog,
    new_factor_experiment,
    update_factor_experiment,
)

_DECISION = datetime(2024, 1, 10, 8, tzinfo=UTC)


def _observation(
    symbol: str,
    value: float,
    *,
    feature_name: str = "momentum",
    available_at: datetime = _DECISION,
) -> FeatureObservation:
    return FeatureObservation(
        symbol=symbol,
        feature_name=feature_name,
        value=value,
        observed_at=available_at,
        available_at=available_at,
        source="fixed",
        source_version="v1",
        market="a_share",
        asset_class="equity",
    )


def _snapshot() -> FeatureSnapshot:
    return build_feature_snapshot(
        dataset_release_id="release-v1",
        dataset_release_checksum="a" * 64,
        decision_at=_DECISION,
        code_version="deadbeef",
        observations=[
            _observation("510300.SH", 0.3),
            _observation("513100.SH", -0.1),
        ],
        calculation_windows={"momentum": 20},
        transformations={"momentum": "winsorize_zscore"},
    )


def test_catalog_only_exposes_implemented_factors() -> None:
    catalog = factor_lab_catalog()
    assert catalog
    assert all(item.implementation for item in catalog)
    assert all(item.unit and len(item.checksum) == 64 for item in catalog)
    assert "residual_momentum" not in {item.name for item in catalog}
    assert {"alpha", "risk", "market_input"} <= {
        item.role.value for item in catalog
    }


def test_feature_snapshot_is_deterministic_and_detects_corruption() -> None:
    snapshot = _snapshot()
    restored = FeatureSnapshot.from_dict(snapshot.as_dict())
    assert restored == snapshot

    corrupted = snapshot.as_dict()
    observations = corrupted["observations"]
    assert isinstance(observations, list)
    observations[0]["value"] = 99.0
    with pytest.raises(ArtifactIntegrityError):
        FeatureSnapshot.from_dict(corrupted)


def test_feature_snapshot_rejects_future_available_data() -> None:
    with pytest.raises(PointInTimeViolationError):
        build_feature_snapshot(
            dataset_release_id="release-v1",
            dataset_release_checksum="a" * 64,
            decision_at=_DECISION,
            code_version="deadbeef",
            observations=[
                _observation(
                    "510300.SH",
                    0.3,
                    available_at=_DECISION + timedelta(seconds=1),
                )
            ],
        )


def test_factor_signal_has_lineage_and_oos_gate() -> None:
    snapshot = _snapshot()
    signal = build_factor_signal(
        feature_snapshot=snapshot,
        factor_name="momentum",
        normalized_scores={"510300.SH": 1.2, "513100.SH": -0.5},
        candidate_universe_version="universe-v1",
    )
    assert signal.items[0].direction.value == "long"
    assert signal.items[1].direction.value == "short"
    assert signal.feature_snapshot_id == snapshot.snapshot_id
    with pytest.raises(SignalNotValidatedError):
        signal.require_strategy_usable()

    validated = build_factor_signal(
        feature_snapshot=snapshot,
        factor_name="momentum",
        normalized_scores={"510300.SH": 1.2, "513100.SH": -0.5},
        candidate_universe_version="universe-v1",
        research_status=ResearchArtifactStatus.VALIDATED_OOS,
        validation_experiment_id="validation-1",
    )
    validated.require_strategy_usable()
    assert type(validated).from_dict(validated.as_dict()) == validated


def test_risk_factor_cannot_become_alpha_signal() -> None:
    snapshot = build_feature_snapshot(
        dataset_release_id="release-v1",
        dataset_release_checksum="a" * 64,
        decision_at=_DECISION,
        code_version="deadbeef",
        observations=[
            _observation(
                "510300.SH",
                1.0,
                feature_name="market_beta",
            )
        ],
    )
    with pytest.raises(ValueError, match="不能转换为信号"):
        build_factor_signal(
            feature_snapshot=snapshot,
            factor_name="market_beta",
            normalized_scores={"510300.SH": 1.0},
            candidate_universe_version="v1",
        )


def test_factor_experiment_freezes_oos_and_terminal_state() -> None:
    snapshot = _snapshot()
    plan = FactorExperimentPlan(
        in_sample_start=date(2020, 1, 1),
        in_sample_end=date(2022, 1, 1),
        oos_start=date(2022, 1, 1),
        oos_end=date(2023, 1, 1),
        trial_budget=20,
        benchmark_symbol="510300.SH",
        transaction_cost_bps=10.0,
    )
    experiment = new_factor_experiment(
        hypothesis="中期趋势在多资产 ETF 上具有成本后预测能力",
        factor_names=("momentum",),
        dataset_release_id="release-v1",
        dataset_release_checksum="a" * 64,
        feature_snapshot_id=snapshot.snapshot_id,
        plan=plan,
        comparison_group="momentum-window",
        now=_DECISION,
    )
    running = update_factor_experiment(
        experiment,
        status=FactorExperimentStatus.RUNNING,
        now=_DECISION + timedelta(minutes=1),
    )
    failed = update_factor_experiment(
        running,
        status=FactorExperimentStatus.FAILED,
        failure_reason="worker interrupted after persistence failure",
        now=_DECISION + timedelta(minutes=2),
    )
    assert failed.failure_reason
    with pytest.raises(ValueError, match="终态"):
        update_factor_experiment(
            failed,
            status=FactorExperimentStatus.VALIDATED_OOS,
            result={"forged": True},
            validation_experiment_id="validation-1",
        )
