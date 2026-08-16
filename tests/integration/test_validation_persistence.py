"""研究实验 + Trial 持久化的集成测试(需要 DB)。

覆盖:
* ``ResearchExperimentRepository.save / get / list_by_status / delete``;
* ``ResearchTrialRepository.save / list_by_experiment``;
* experiment ↔ trial 级联删除;
* ``deserialize_experiment`` 往返一致性。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.validation.contracts import (
    AcceptanceThresholds,
    ExperimentStatus,
    ResearchExperiment,
    RobustnessPlan,
    TrialRecord,
    TrialStatus,
    ValidationMode,
    ValidationPlan,
    VersionStamp,
    WindowMetrics,
    WindowRole,
    new_experiment,
)
from finboard_persistence.validation_repo import (
    ResearchExperimentRepository,
    ResearchTrialRepository,
)


@pytest.fixture
def version_stamp() -> VersionStamp:
    return VersionStamp(
        matching_model_version="v2",
        asset_rules_version="v1",
        factor_version="v1",
        dataset_versions={"daily_metrics": "2024-01-01"},
        selection_config={"enabled": False},
        strategy_kind="ma_cross",
    )


@pytest.fixture
def plan() -> ValidationPlan:
    return ValidationPlan(
        mode=ValidationMode.ROLLING,
        train_start=date(2020, 1, 1),
        train_end=date(2020, 12, 31),
        validation_start=date(2021, 1, 1),
        validation_end=date(2021, 6, 30),
        test_start=date(2021, 7, 1),
        test_end=date(2021, 12, 31),
        train_window_days=252,
        test_window_days=63,
        step_days=21,
        trial_budget=20,
    )


@pytest.fixture
def experiment(version_stamp: VersionStamp, plan: ValidationPlan) -> ResearchExperiment:
    return new_experiment(
        hypothesis="测试假设:均线交叉在宽基 ETF 上有 alpha",
        version_stamp=version_stamp,
        plan=plan,
        thresholds=AcceptanceThresholds(),
        robustness=RobustnessPlan(),
        strategy_params_space={"window": [5, 10, 20]},
        notes="测试",
    )


class TestExperimentPersistence:
    async def test_save_and_get(
        self,
        db_session: AsyncSession,
        experiment: ResearchExperiment,
    ) -> None:
        repo = ResearchExperimentRepository(db_session)
        await repo.save(experiment)
        await db_session.commit()

        restored = await repo.get(experiment.experiment_id)
        assert restored is not None
        assert restored.experiment_id == experiment.experiment_id
        assert restored.hypothesis == experiment.hypothesis
        assert restored.status == experiment.status
        assert restored.trials_used == 0
        assert restored.plan.mode == experiment.plan.mode
        assert restored.thresholds.min_oos_sharpe == experiment.thresholds.min_oos_sharpe

    async def test_update_existing(
        self,
        db_session: AsyncSession,
        experiment: ResearchExperiment,
    ) -> None:
        repo = ResearchExperimentRepository(db_session)
        await repo.save(experiment)
        await db_session.commit()

        # 修改后重新 save
        from finboard_backtest.validation.contracts import (
            increment_trials_used,
            transition_status,
        )

        updated = increment_trials_used(experiment)
        updated = transition_status(updated, ExperimentStatus.IN_SAMPLE)
        await repo.save(updated)
        await db_session.commit()

        restored = await repo.get(experiment.experiment_id)
        assert restored is not None
        assert restored.trials_used == 1
        assert restored.status == ExperimentStatus.IN_SAMPLE

    async def test_list_by_status(
        self,
        db_session: AsyncSession,
        experiment: ResearchExperiment,
    ) -> None:
        repo = ResearchExperimentRepository(db_session)
        await repo.save(experiment)
        await db_session.commit()

        # status=hypothesis
        all_hyp = await repo.list_by_status(ExperimentStatus.HYPOTHESIS)
        assert any(e.experiment_id == experiment.experiment_id for e in all_hyp)

        # status=validated_oos (应该为空)
        validated = await repo.list_by_status(ExperimentStatus.VALIDATED_OOS)
        assert not any(e.experiment_id == experiment.experiment_id for e in validated)

    async def test_delete_cascades(
        self,
        db_session: AsyncSession,
        experiment: ResearchExperiment,
    ) -> None:
        repo = ResearchExperimentRepository(db_session)
        trial_repo = ResearchTrialRepository(db_session)
        await repo.save(experiment)
        # 添加一个 trial
        trial = TrialRecord(
            trial_id=f"{experiment.experiment_id}-t0",
            experiment_id=experiment.experiment_id,
            trial_index=0,
            parameters={"alpha": 0.001},
            status=TrialStatus.CANDIDATE,
        )
        await trial_repo.save(trial)
        await db_session.commit()

        # 删除实验 → trial 也应被级联删除
        assert await repo.delete(experiment.experiment_id) is True
        await db_session.commit()

        assert await repo.get(experiment.experiment_id) is None
        assert await trial_repo.get(trial.trial_id) is None


class TestTrialPersistence:
    async def test_save_and_get_with_metrics(
        self,
        db_session: AsyncSession,
        experiment: ResearchExperiment,
    ) -> None:
        exp_repo = ResearchExperimentRepository(db_session)
        trial_repo = ResearchTrialRepository(db_session)
        await exp_repo.save(experiment)

        is_metrics = WindowMetrics(
            role=WindowRole.TRAIN,
            start=date(2020, 1, 1),
            end=date(2020, 12, 31),
            sharpe_ratio=1.5,
            max_drawdown=-0.10,
            total_return=0.25,
        )
        oos_metrics = WindowMetrics(
            role=WindowRole.TEST,
            start=date(2021, 1, 1),
            end=date(2021, 6, 30),
            sharpe_ratio=0.8,
            max_drawdown=-0.15,
            total_return=0.10,
        )
        trial = TrialRecord(
            trial_id=f"{experiment.experiment_id}-t0",
            experiment_id=experiment.experiment_id,
            trial_index=0,
            parameters={"window": 10, "threshold": 0.02},
            status=TrialStatus.SELECTED,
            in_sample_metrics=is_metrics,
            oos_metrics=oos_metrics,
        )
        await trial_repo.save(trial)
        await db_session.commit()

        restored = await trial_repo.get(trial.trial_id)
        assert restored is not None
        assert restored.status == TrialStatus.SELECTED
        assert restored.in_sample_metrics is not None
        assert restored.in_sample_metrics.sharpe_ratio == pytest.approx(1.5)
        assert restored.oos_metrics is not None
        assert restored.oos_metrics.max_drawdown == pytest.approx(-0.15)

    async def test_list_by_experiment_ordered(
        self,
        db_session: AsyncSession,
        experiment: ResearchExperiment,
    ) -> None:
        exp_repo = ResearchExperimentRepository(db_session)
        trial_repo = ResearchTrialRepository(db_session)
        await exp_repo.save(experiment)

        for i in range(5):
            t = TrialRecord(
                trial_id=f"{experiment.experiment_id}-t{i}",
                experiment_id=experiment.experiment_id,
                trial_index=i,
                parameters={"i": i},
                status=TrialStatus.CANDIDATE if i % 2 == 0 else TrialStatus.FAILED,
                failure_reason="bad" if i % 2 else None,
            )
            await trial_repo.save(t)
        await db_session.commit()

        all_trials = await trial_repo.list_by_experiment(experiment.experiment_id)
        assert len(all_trials) == 5
        # trial_index 单调递增
        indices = [t.trial_index for t in all_trials]
        assert indices == sorted(indices)

        # status 过滤
        failed = await trial_repo.list_by_experiment(
            experiment.experiment_id, status=TrialStatus.FAILED
        )
        assert len(failed) == 2

    async def test_count_includes_failed(
        self,
        db_session: AsyncSession,
        experiment: ResearchExperiment,
    ) -> None:
        exp_repo = ResearchExperimentRepository(db_session)
        trial_repo = ResearchTrialRepository(db_session)
        await exp_repo.save(experiment)

        # 2 selected + 1 failed
        for i, status in enumerate(
            [TrialStatus.SELECTED, TrialStatus.FAILED, TrialStatus.SELECTED]
        ):
            t = TrialRecord(
                trial_id=f"{experiment.experiment_id}-t{i}",
                experiment_id=experiment.experiment_id,
                trial_index=i,
                parameters={},
                status=status,
            )
            await trial_repo.save(t)
        await db_session.commit()

        n = await trial_repo.count_by_experiment(experiment.experiment_id)
        assert n == 3  # 失败也算
