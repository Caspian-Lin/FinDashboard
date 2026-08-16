"""因子实验室 PostgreSQL 仓储与 #57 机器验证绑定(issue #78)。"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.validation.contracts import (
    ExperimentStatus,
    ResearchExperiment,
    TrialRecord,
    TrialStatus,
)
from finboard_data.factor_lab import (
    ArtifactIntegrityError,
    FactorExperiment,
    FactorExperimentStatus,
    FactorSignal,
    FeatureSnapshot,
    ResearchArtifactStatus,
    update_factor_experiment,
)
from finboard_persistence.dataset_release_repo import (
    ResearchDatasetReleaseRepository,
)
from finboard_persistence.models import (
    FactorExperimentModel,
    FactorFeatureSnapshotModel,
    FactorSignalModel,
)
from finboard_persistence.validation_repo import (
    ResearchExperimentRepository,
    ResearchTrialRepository,
)


class FeatureSnapshotRepository:
    """按 ID/checksum 幂等发布不可变 FeatureSnapshot。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def publish(self, snapshot: FeatureSnapshot) -> FactorFeatureSnapshotModel:
        FeatureSnapshot.from_dict(snapshot.as_dict())
        release = await ResearchDatasetReleaseRepository(
            self._session
        ).require_usable(snapshot.dataset_release_id)
        if release.release_checksum != snapshot.dataset_release_checksum:
            raise ArtifactIntegrityError(
                "FeatureSnapshot 绑定的数据发布 checksum 不一致"
            )
        existing = await self._find_identity(snapshot.snapshot_id, snapshot.checksum)
        if existing is not None:
            if (
                existing.snapshot_id != snapshot.snapshot_id
                or existing.checksum != snapshot.checksum
                or existing.payload != snapshot.as_dict()
            ):
                raise ArtifactIntegrityError("FeatureSnapshot 身份已存在但内容不同")
            return existing
        row = FactorFeatureSnapshotModel(
            snapshot_id=snapshot.snapshot_id,
            dataset_release_id=snapshot.dataset_release_id,
            dataset_release_checksum=snapshot.dataset_release_checksum,
            decision_at=snapshot.decision_at,
            published_at=snapshot.published_at,
            framework_version=snapshot.framework_version,
            feature_names=sorted(
                {item.feature_name for item in snapshot.observations}
            ),
            symbol_count=len({item.symbol for item in snapshot.observations}),
            observation_count=len(snapshot.observations),
            code_version=snapshot.code_version,
            checksum=snapshot.checksum,
            payload=snapshot.as_dict(),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, snapshot_id: str) -> FeatureSnapshot | None:
        row = await self._model(snapshot_id)
        return (
            FeatureSnapshot.from_dict(row.payload)
            if row is not None
            else None
        )

    async def list(
        self,
        *,
        dataset_release_id: str | None = None,
        limit: int = 100,
    ) -> list[FeatureSnapshot]:
        statement = select(FactorFeatureSnapshotModel)
        if dataset_release_id is not None:
            statement = statement.where(
                FactorFeatureSnapshotModel.dataset_release_id
                == dataset_release_id
            )
        statement = statement.order_by(
            FactorFeatureSnapshotModel.decision_at.desc()
        ).limit(limit)
        rows = (await self._session.execute(statement)).scalars().all()
        return [FeatureSnapshot.from_dict(row.payload) for row in rows]

    async def _model(
        self,
        snapshot_id: str,
    ) -> FactorFeatureSnapshotModel | None:
        statement = select(FactorFeatureSnapshotModel).where(
            FactorFeatureSnapshotModel.snapshot_id == snapshot_id
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _find_identity(
        self,
        snapshot_id: str,
        checksum: str,
    ) -> FactorFeatureSnapshotModel | None:
        statement = select(FactorFeatureSnapshotModel).where(
            or_(
                FactorFeatureSnapshotModel.snapshot_id == snapshot_id,
                FactorFeatureSnapshotModel.checksum == checksum,
            )
        )
        return (await self._session.execute(statement)).scalars().first()


class FactorSignalRepository:
    """不可变信号仓储;validated_oos 必须绑定真实 #57 实验。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def publish(self, signal: FactorSignal) -> FactorSignalModel:
        FactorSignal.from_dict(signal.as_dict())
        snapshot = await FeatureSnapshotRepository(self._session).get(
            signal.feature_snapshot_id
        )
        if snapshot is None:
            raise ValueError(
                f"FeatureSnapshot 不存在: {signal.feature_snapshot_id}"
            )
        if snapshot.checksum != signal.feature_snapshot_checksum:
            raise ArtifactIntegrityError("FactorSignal 绑定的快照 checksum 不一致")
        if signal.research_status is ResearchArtifactStatus.VALIDATED_OOS:
            await self._require_validation_passed(signal)
        existing = await self._find_identity(signal.signal_id, signal.checksum)
        if existing is not None:
            if (
                existing.signal_id != signal.signal_id
                or existing.checksum != signal.checksum
                or existing.payload != signal.as_dict()
            ):
                raise ArtifactIntegrityError("FactorSignal 身份已存在但内容不同")
            return existing
        row = FactorSignalModel(
            signal_id=signal.signal_id,
            factor_name=signal.factor_name,
            factor_version=signal.factor_version,
            feature_snapshot_id=signal.feature_snapshot_id,
            feature_snapshot_checksum=signal.feature_snapshot_checksum,
            candidate_universe_version=signal.candidate_universe_version,
            research_status=signal.research_status.value,
            validation_experiment_id=signal.validation_experiment_id,
            symbol_count=len(signal.items),
            checksum=signal.checksum,
            payload=signal.as_dict(),
            created_at=signal.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, signal_id: str) -> FactorSignal | None:
        row = await self._model(signal_id)
        return FactorSignal.from_dict(row.payload) if row is not None else None

    async def require_strategy_usable(self, signal_id: str) -> FactorSignal:
        signal = await self.get(signal_id)
        if signal is None:
            raise ValueError(f"FactorSignal 不存在: {signal_id}")
        signal.require_strategy_usable()
        await self._require_validation_passed(signal)
        return signal

    async def list(
        self,
        *,
        factor_name: str | None = None,
        research_status: ResearchArtifactStatus | None = None,
        limit: int = 100,
    ) -> list[FactorSignal]:
        statement = select(FactorSignalModel)
        if factor_name is not None:
            statement = statement.where(
                FactorSignalModel.factor_name == factor_name
            )
        if research_status is not None:
            statement = statement.where(
                FactorSignalModel.research_status == research_status.value
            )
        statement = statement.order_by(FactorSignalModel.created_at.desc()).limit(
            limit
        )
        rows = (await self._session.execute(statement)).scalars().all()
        return [FactorSignal.from_dict(row.payload) for row in rows]

    async def _require_validation_passed(self, signal: FactorSignal) -> None:
        if not signal.validation_experiment_id:
            raise ValueError("validated signal 未绑定 validation_experiment_id")
        validation = await ResearchExperimentRepository(self._session).get(
            signal.validation_experiment_id
        )
        if (
            validation is None
            or validation.status is not ExperimentStatus.VALIDATED_OOS
        ):
            raise ValueError("FactorSignal 引用的 #57 实验未通过 OOS")
        snapshot = await FeatureSnapshotRepository(self._session).get(
            signal.feature_snapshot_id
        )
        if snapshot is None:
            raise ValueError("FactorSignal 引用的 FeatureSnapshot 不存在")
        if (
            validation.version_stamp.dataset_versions.get(
                "research_dataset_release"
            )
            != snapshot.dataset_release_id
        ):
            raise ArtifactIntegrityError(
                "FactorSignal 与验证实验的数据发布版本不一致"
            )
        if validation.version_stamp.factor_version != signal.factor_version:
            raise ArtifactIntegrityError(
                "FactorSignal 与验证实验的 factor_version 不一致"
            )
        universe_version = validation.version_stamp.selection_config.get(
            "candidate_universe_version"
        )
        if universe_version != signal.candidate_universe_version:
            raise ArtifactIntegrityError(
                "FactorSignal 与验证实验的候选池版本不一致"
            )
        trials = await ResearchTrialRepository(self._session).list_by_experiment(
            validation.experiment_id
        )
        if not _has_machine_oos_evidence(trials):
            raise ValueError("验证实验没有已完成的 OOS trial 证据")

    async def _model(self, signal_id: str) -> FactorSignalModel | None:
        statement = select(FactorSignalModel).where(
            FactorSignalModel.signal_id == signal_id
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _find_identity(
        self,
        signal_id: str,
        checksum: str,
    ) -> FactorSignalModel | None:
        statement = select(FactorSignalModel).where(
            or_(
                FactorSignalModel.signal_id == signal_id,
                FactorSignalModel.checksum == checksum,
            )
        )
        return (await self._session.execute(statement)).scalars().first()


class FactorExperimentRepository:
    """因子实验仓储;固定输入不可更新,仅允许状态机字段变化。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, experiment: FactorExperiment) -> FactorExperimentModel:
        await self._validate_references(experiment)
        existing = await self._model(experiment.experiment_id)
        if existing is None:
            row = FactorExperimentModel(
                experiment_id=experiment.experiment_id,
                hypothesis=experiment.hypothesis,
                factor_names=list(experiment.factor_names),
                dataset_release_id=experiment.dataset_release_id,
                dataset_release_checksum=experiment.dataset_release_checksum,
                feature_snapshot_id=experiment.feature_snapshot_id,
                plan=experiment.plan.as_dict(),
                comparison_group=experiment.comparison_group,
                status=experiment.status.value,
                validation_experiment_id=experiment.validation_experiment_id,
                result=experiment.result,
                failure_reason=experiment.failure_reason,
                created_at=experiment.created_at,
                updated_at=experiment.updated_at,
            )
            self._session.add(row)
            await self._session.flush()
            return row
        _assert_experiment_identity(existing, experiment)
        existing.status = experiment.status.value
        existing.validation_experiment_id = experiment.validation_experiment_id
        existing.result = experiment.result
        existing.failure_reason = experiment.failure_reason
        existing.updated_at = experiment.updated_at
        await self._session.flush()
        return existing

    async def get(self, experiment_id: str) -> FactorExperiment | None:
        row = await self._model(experiment_id)
        return _experiment_from_model(row) if row is not None else None

    async def list(
        self,
        *,
        status: FactorExperimentStatus | None = None,
        comparison_group: str | None = None,
        limit: int = 100,
    ) -> list[FactorExperiment]:
        statement = select(FactorExperimentModel)
        if status is not None:
            statement = statement.where(
                FactorExperimentModel.status == status.value
            )
        if comparison_group is not None:
            statement = statement.where(
                FactorExperimentModel.comparison_group == comparison_group
            )
        statement = statement.order_by(
            FactorExperimentModel.created_at.desc()
        ).limit(limit)
        rows = (await self._session.execute(statement)).scalars().all()
        return [_experiment_from_model(row) for row in rows]

    async def _validate_references(self, experiment: FactorExperiment) -> None:
        release = await ResearchDatasetReleaseRepository(
            self._session
        ).require_usable(experiment.dataset_release_id)
        if release.release_checksum != experiment.dataset_release_checksum:
            raise ArtifactIntegrityError("因子实验的数据发布 checksum 不一致")
        snapshot = await FeatureSnapshotRepository(self._session).get(
            experiment.feature_snapshot_id
        )
        if snapshot is None:
            raise ValueError(
                f"FeatureSnapshot 不存在: {experiment.feature_snapshot_id}"
            )
        if (
            snapshot.dataset_release_id != experiment.dataset_release_id
            or snapshot.dataset_release_checksum
            != experiment.dataset_release_checksum
        ):
            raise ArtifactIntegrityError("因子实验与特征快照的数据发布不一致")
        if experiment.validation_experiment_id is not None:
            validation = await ResearchExperimentRepository(self._session).get(
                experiment.validation_experiment_id
            )
            if validation is None:
                raise ValueError(
                    "validation_experiment_id 引用的 #57 实验不存在"
                )
            _assert_validation_plan_matches(experiment, validation)

    async def _model(
        self,
        experiment_id: str,
    ) -> FactorExperimentModel | None:
        statement = select(FactorExperimentModel).where(
            FactorExperimentModel.experiment_id == experiment_id
        )
        return (await self._session.execute(statement)).scalar_one_or_none()


class FactorExperimentValidationService:
    """从 #57 持久化结果同步因子实验终态,不接受 passed_oos 布尔值。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def sync(self, experiment_id: str) -> FactorExperiment:
        repository = FactorExperimentRepository(self._session)
        experiment = await repository.get(experiment_id)
        if experiment is None:
            raise ValueError(f"FactorExperiment 不存在: {experiment_id}")
        if not experiment.validation_experiment_id:
            raise ValueError("因子实验尚未绑定 #57 validation experiment")
        validation = await ResearchExperimentRepository(self._session).get(
            experiment.validation_experiment_id
        )
        if validation is None:
            raise ValueError("绑定的 #57 validation experiment 不存在")
        _assert_validation_plan_matches(experiment, validation)
        trials = await ResearchTrialRepository(self._session).list_by_experiment(
            validation.experiment_id
        )
        if experiment.status in (
            FactorExperimentStatus.HYPOTHESIS,
            FactorExperimentStatus.INTERRUPTED,
        ):
            experiment = update_factor_experiment(
                experiment,
                status=FactorExperimentStatus.RUNNING,
            )
        if validation.status is ExperimentStatus.VALIDATED_OOS:
            evidence = _machine_evidence(trials)
            if evidence is None:
                raise ValueError("VALIDATED_OOS 实验缺少完成的机器 OOS 证据")
            experiment = update_factor_experiment(
                experiment,
                status=FactorExperimentStatus.VALIDATED_OOS,
                result={
                    "validation_experiment_id": validation.experiment_id,
                    "validation_status": validation.status.value,
                    "trials_used": validation.trials_used,
                    "trial_budget": validation.plan.trial_budget,
                    "best_trial": evidence,
                },
                validation_experiment_id=validation.experiment_id,
            )
        elif validation.status is ExperimentStatus.REJECTED:
            experiment = update_factor_experiment(
                experiment,
                status=FactorExperimentStatus.REJECTED,
                result={
                    "validation_experiment_id": validation.experiment_id,
                    "validation_status": validation.status.value,
                    "trials_used": validation.trials_used,
                    "trial_budget": validation.plan.trial_budget,
                    "trial_status_counts": _trial_status_counts(trials),
                },
                failure_reason=(
                    validation.rejection_reason or "机器 OOS 验证拒绝"
                ),
                validation_experiment_id=validation.experiment_id,
            )
        else:
            # 非终态只同步 RUNNING,不擅自晋级或拒绝。
            await repository.save(experiment)
            return experiment
        await repository.save(experiment)
        return experiment


def _assert_validation_plan_matches(
    factor_experiment: FactorExperiment,
    validation: ResearchExperiment,
) -> None:
    factor_plan = factor_experiment.plan
    validation_plan = validation.plan
    actual = (
        factor_plan.in_sample_start,
        factor_plan.in_sample_end,
        factor_plan.oos_start,
        factor_plan.oos_end,
        factor_plan.trial_budget,
        factor_plan.benchmark_symbol,
    )
    expected = (
        validation_plan.train_start,
        validation_plan.validation_end,
        validation_plan.test_start,
        validation_plan.test_end,
        validation_plan.trial_budget,
        validation_plan.benchmark_symbol,
    )
    if actual != expected:
        raise ArtifactIntegrityError("因子实验 IS/OOS/预算与 #57 验证计划不一致")
    release_id = validation.version_stamp.dataset_versions.get(
        "research_dataset_release"
    )
    if release_id != factor_experiment.dataset_release_id:
        raise ArtifactIntegrityError("#57 验证实验未绑定同一研究数据发布")


def _assert_experiment_identity(
    row: FactorExperimentModel,
    experiment: FactorExperiment,
) -> None:
    existing = (
        row.hypothesis,
        tuple(row.factor_names),
        row.dataset_release_id,
        row.dataset_release_checksum,
        row.feature_snapshot_id,
        row.plan,
        row.comparison_group,
        row.created_at,
    )
    incoming = (
        experiment.hypothesis,
        experiment.factor_names,
        experiment.dataset_release_id,
        experiment.dataset_release_checksum,
        experiment.feature_snapshot_id,
        experiment.plan.as_dict(),
        experiment.comparison_group,
        experiment.created_at,
    )
    if existing != incoming:
        raise ArtifactIntegrityError("FactorExperiment 冻结字段不可修改")


def _experiment_from_model(row: FactorExperimentModel) -> FactorExperiment:
    return FactorExperiment.from_dict(
        {
            "experiment_id": row.experiment_id,
            "hypothesis": row.hypothesis,
            "factor_names": row.factor_names,
            "dataset_release_id": row.dataset_release_id,
            "dataset_release_checksum": row.dataset_release_checksum,
            "feature_snapshot_id": row.feature_snapshot_id,
            "plan": row.plan,
            "comparison_group": row.comparison_group,
            "status": row.status,
            "validation_experiment_id": row.validation_experiment_id,
            "result": row.result,
            "failure_reason": row.failure_reason,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
    )


def _machine_evidence(trials: list[TrialRecord]) -> dict[str, object] | None:
    candidates = [
        trial
        for trial in trials
        if trial.status is TrialStatus.SELECTED
        and trial.oos_metrics is not None
        and trial.statistical_report is not None
    ]
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda trial: trial.oos_metrics.sharpe_ratio
        if trial.oos_metrics is not None
        else float("-inf"),
    )
    return {
        "trial_id": best.trial_id,
        "parameters": best.parameters,
        "oos_metrics": (
            best.oos_metrics.as_dict() if best.oos_metrics is not None else None
        ),
        "statistical_report": (
            best.statistical_report.as_dict()
            if best.statistical_report is not None
            else None
        ),
        "robustness_probes": [
            probe.as_dict() for probe in best.robustness_probes
        ],
    }


def _has_machine_oos_evidence(trials: list[TrialRecord]) -> bool:
    return _machine_evidence(trials) is not None


def _trial_status_counts(trials: list[TrialRecord]) -> dict[str, int]:
    result: dict[str, int] = {}
    for trial in trials:
        result[trial.status.value] = result.get(trial.status.value, 0) + 1
    return result


__all__ = [
    "FactorExperimentRepository",
    "FactorExperimentValidationService",
    "FactorSignalRepository",
    "FeatureSnapshotRepository",
]
