"""冻结发布→特征→信号→OOS 实验→重启恢复集成测试(issue #78)。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    StatisticalReport,
    TrialRecord,
    TrialStatus,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    WindowMetrics,
    WindowRole,
    increment_trials_used,
    mark_final_test_unsealed,
    new_experiment,
    transition_status,
)
from finboard_data.cache import ParquetCache
from finboard_data.factor_lab import (
    ArtifactIntegrityError,
    FactorExperimentPlan,
    FactorExperimentStatus,
    FeatureObservation,
    ResearchArtifactStatus,
    SignalNotValidatedError,
    build_factor_signal,
    build_feature_snapshot,
    new_factor_experiment,
    update_factor_experiment,
)
from finboard_data.releases import DatasetReleaseSpec, ResearchDatasetRelease
from finboard_persistence import (
    FactorExperimentRepository,
    FactorExperimentValidationService,
    FactorSignalRepository,
    FeatureSnapshotRepository,
    ResearchDatasetReleaseService,
    ResearchExperimentRepository,
    ResearchTrialRepository,
    session_factory,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

pytestmark = pytest.mark.asyncio

_SYMBOLS = ("510300.SH", "513100.SH", "518880.SH", "511010.SH")
_START = date(2024, 1, 2)
_END = date(2024, 1, 5)
_DECISION = datetime(2024, 1, 5, 8, tzinfo=UTC)


async def _publish_release(
    session: AsyncSession,
    tmp_path: Path,
) -> ResearchDatasetRelease:
    cache = ParquetCache(tmp_path / "cache")
    for symbol_index, code in enumerate(_SYMBOLS):
        symbol = Symbol(code, Market.A_SHARE)
        bars = [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(
                    _START + timedelta(days=index),
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
                open=Decimal(str(3 + symbol_index + index * 0.01)),
                high=Decimal(str(3.1 + symbol_index + index * 0.01)),
                low=Decimal(str(2.9 + symbol_index + index * 0.01)),
                close=Decimal(str(3 + symbol_index + index * 0.01)),
                volume=Decimal("1000000"),
                amount=Decimal("3000000"),
            )
            for index in range(4)
        ]
        await cache.write(symbol, BarPeriod.D1, "qfq", bars)
    service = ResearchDatasetReleaseService(
        session,
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
    )
    return await service.publish(
        DatasetReleaseSpec(
            release_id="integration-factor-lab-v1",
            dataset_name="factor_lab_integration",
            source="fixed_sample",
            version="integration-v1",
            start_date=_START,
            end_date=_END,
            code_version="integration",
            required_capabilities=(
                "etf:index",
                "etf:cross_border",
                "etf:commodity",
                "etf:bond",
            ),
        ),
        list(_SYMBOLS),
    )


async def test_full_factor_lab_persistence_and_restart(
    _engine: AsyncEngine,  # noqa: PT019
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    release = await _publish_release(db_session, tmp_path)
    snapshot = build_feature_snapshot(
        dataset_release_id=release.release_id,
        dataset_release_checksum=release.release_checksum,
        decision_at=_DECISION,
        code_version="integration",
        observations=[
            FeatureObservation(
                symbol=code,
                feature_name="momentum",
                value=float(index - 1),
                observed_at=_DECISION - timedelta(minutes=30),
                available_at=_DECISION - timedelta(minutes=30),
                source="fixed_sample",
                source_version=release.version,
                market="a_share",
                asset_class=release.instrument(code).asset_class.value,
            )
            for index, code in enumerate(_SYMBOLS)
        ],
        calculation_windows={"momentum": 20},
    )
    feature_repo = FeatureSnapshotRepository(db_session)
    await feature_repo.publish(snapshot)
    assert (await feature_repo.publish(snapshot)).snapshot_id == snapshot.snapshot_id
    with pytest.raises(ArtifactIntegrityError, match=r"checksum|内容不同"):
        await feature_repo.publish(replace(snapshot, code_version="changed"))
    with pytest.raises(ArtifactIntegrityError, match="checksum"):
        await feature_repo.publish(
            replace(snapshot, dataset_release_checksum="0" * 64)
        )
    with pytest.raises(ArtifactIntegrityError, match="checksum"):
        await feature_repo.publish(
            replace(
                snapshot,
                snapshot_id="feature-forged",
                checksum="f" * 64,
            )
        )

    preview = build_factor_signal(
        feature_snapshot=snapshot,
        factor_name="momentum",
        normalized_scores={
            code: float(index - 1) for index, code in enumerate(_SYMBOLS)
        },
        candidate_universe_version="universe-v1",
    )
    signal_repo = FactorSignalRepository(db_session)
    await signal_repo.publish(preview)
    with pytest.raises(SignalNotValidatedError, match="只有 validated_oos"):
        await signal_repo.require_strategy_usable(preview.signal_id)

    validation_plan = ValidationPlan(
        mode=ValidationMode.ROLLING,
        train_start=date(2020, 1, 1),
        train_end=date(2020, 12, 31),
        validation_start=date(2021, 1, 1),
        validation_end=date(2021, 6, 30),
        test_start=date(2021, 7, 1),
        test_end=date(2021, 12, 31),
        trial_budget=10,
        benchmark_symbol="510300.SH",
    )
    validation = new_experiment(
        hypothesis="多资产 ETF 动量通过冻结 OOS 与成本检验",
        version_stamp=VersionStamp(
            matching_model_version="v2",
            asset_rules_version="v1",
            factor_version="v2",
            dataset_versions={
                "research_dataset_release": release.release_id,
            },
            selection_config={
                "candidate_universe_version": "universe-v1",
            },
            strategy_kind="factor_lab",
        ),
        plan=validation_plan,
        thresholds=AcceptanceThresholds(),
    )
    validation = increment_trials_used(validation)
    validation = transition_status(validation, ExperimentStatus.IN_SAMPLE)
    validation = mark_final_test_unsealed(validation)
    validation = transition_status(
        validation,
        ExperimentStatus.VALIDATED_OOS,
    )
    await ResearchExperimentRepository(db_session).save(validation)
    trial = TrialRecord(
        trial_id=f"{validation.experiment_id}-t0",
        experiment_id=validation.experiment_id,
        trial_index=0,
        parameters={"window": 20},
        status=TrialStatus.SELECTED,
        oos_metrics=WindowMetrics(
            role=WindowRole.TEST,
            start=validation_plan.test_start,
            end=validation_plan.test_end,
            sharpe_ratio=0.8,
            total_return=0.1,
            max_drawdown=-0.08,
        ),
        statistical_report=StatisticalReport(
            deflated_sharpe_ratio=0.2,
            probabilistic_sharpe_ratio=0.96,
            pbo=0.2,
            bootstrap_sharpe_ci_low=0.1,
            bootstrap_sharpe_ci_high=1.2,
            bootstrap_mdd_ci_low=-0.15,
            bootstrap_mdd_ci_high=-0.03,
            n_trials=1,
        ),
    )
    await ResearchTrialRepository(db_session).save(trial)

    factor_experiment = new_factor_experiment(
        hypothesis="多资产 ETF 动量通过冻结 OOS 与成本检验",
        factor_names=("momentum",),
        dataset_release_id=release.release_id,
        dataset_release_checksum=release.release_checksum,
        feature_snapshot_id=snapshot.snapshot_id,
        plan=FactorExperimentPlan(
            in_sample_start=validation_plan.train_start,
            in_sample_end=validation_plan.validation_end,
            oos_start=validation_plan.test_start,
            oos_end=validation_plan.test_end,
            trial_budget=validation_plan.trial_budget,
            benchmark_symbol="510300.SH",
            transaction_cost_bps=10,
        ),
        comparison_group="integration-momentum",
        validation_experiment_id=validation.experiment_id,
        now=_DECISION,
    )
    await FactorExperimentRepository(db_session).save(factor_experiment)
    synced = await FactorExperimentValidationService(db_session).sync(
        factor_experiment.experiment_id
    )
    assert synced.status.value == "validated_oos"
    assert synced.result is not None
    best_trial = cast(dict[str, object], synced.result["best_trial"])
    assert best_trial["trial_id"] == trial.trial_id

    wrong_universe_signal = build_factor_signal(
        feature_snapshot=snapshot,
        factor_name="momentum",
        normalized_scores={
            code: float(index - 1) for index, code in enumerate(_SYMBOLS)
        },
        candidate_universe_version="universe-forged",
        research_status=ResearchArtifactStatus.VALIDATED_OOS,
        validation_experiment_id=validation.experiment_id,
    )
    with pytest.raises(ArtifactIntegrityError, match="候选池版本"):
        await signal_repo.publish(wrong_universe_signal)

    validated_signal = build_factor_signal(
        feature_snapshot=snapshot,
        factor_name="momentum",
        normalized_scores={
            code: float(index - 1) for index, code in enumerate(_SYMBOLS)
        },
        candidate_universe_version="universe-v1",
        research_status=ResearchArtifactStatus.VALIDATED_OOS,
        validation_experiment_id=validation.experiment_id,
    )
    await signal_repo.publish(validated_signal)
    assert (
        await signal_repo.require_strategy_usable(validated_signal.signal_id)
    ) == validated_signal

    interrupted = new_factor_experiment(
        hypothesis="验证 worker 中断时必须保留完整失败记录",
        factor_names=("momentum",),
        dataset_release_id=release.release_id,
        dataset_release_checksum=release.release_checksum,
        feature_snapshot_id=snapshot.snapshot_id,
        plan=factor_experiment.plan,
        comparison_group="integration-interruption",
        now=_DECISION,
    )
    interrupted = update_factor_experiment(
        interrupted,
        status=FactorExperimentStatus.RUNNING,
        now=_DECISION + timedelta(minutes=1),
    )
    interrupted = update_factor_experiment(
        interrupted,
        status=FactorExperimentStatus.INTERRUPTED,
        failure_reason="validation worker lost database connection",
        now=_DECISION + timedelta(minutes=2),
    )
    await FactorExperimentRepository(db_session).save(interrupted)
    await db_session.commit()

    async with session_factory(_engine)() as restarted:
        assert (
            await FeatureSnapshotRepository(restarted).get(snapshot.snapshot_id)
        ) == snapshot
        assert (
            await FactorSignalRepository(restarted).require_strategy_usable(
                validated_signal.signal_id
            )
        ) == validated_signal
        restored_experiment = await FactorExperimentRepository(restarted).get(
            factor_experiment.experiment_id
        )
        assert restored_experiment == synced
        restored_interrupted = await FactorExperimentRepository(restarted).get(
            interrupted.experiment_id
        )
        assert restored_interrupted == interrupted
        assert restored_interrupted.failure_reason is not None
