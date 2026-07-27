"""样本外验证实验的持久化仓储(issue #57)。

Repository 模式与交易域一致:不控制事务边界,commit 由调用方决定。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_backtest.validation.contracts import (
    ExperimentStatus,
    ResearchExperiment,
    RobustnessProbe,
    StatisticalReport,
    TrialRecord,
    TrialStatus,
    WindowMetrics,
    WindowRole,
    deserialize_experiment,
)
from finboard_persistence.models import ResearchExperimentModel, ResearchTrialModel


class ResearchExperimentRepository:
    """``ResearchExperiment`` 持久化仓储。

    用法::

        repo = ResearchExperimentRepository(session)
        await repo.save(experiment)
        exp = await repo.get(experiment_id)
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, experiment: ResearchExperiment) -> ResearchExperimentModel:
        """插入或更新实验(upsert by experiment_id)。"""
        existing = await self._get_model(experiment.experiment_id)
        if existing is None:
            model = _experiment_to_model(experiment)
            self._session.add(model)
        else:
            _apply_experiment_to_model(existing, experiment)
            model = existing
        await self._session.flush()
        return model

    async def get(self, experiment_id: str) -> ResearchExperiment | None:
        """按 ``experiment_id`` 读取实验。"""
        model = await self._get_model(experiment_id)
        if model is None:
            return None
        return _model_to_experiment(model)

    async def list_by_status(
        self,
        status: ExperimentStatus | None = None,
        *,
        limit: int = 100,
    ) -> list[ResearchExperiment]:
        """按状态列出实验(默认全部)。"""
        stmt = select(ResearchExperimentModel).order_by(
            ResearchExperimentModel.created_at.desc()
        )
        if status is not None:
            stmt = stmt.where(ResearchExperimentModel.status == status.value)
        stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return [_model_to_experiment(m) for m in result.scalars()]

    async def list_superseded_chain(
        self, experiment_id: str
    ) -> list[ResearchExperiment]:
        """列出 experiment 的全部历史版本(supersedes_id 链)。"""
        chain: list[ResearchExperiment] = []
        current_id: str | None = experiment_id
        seen: set[str] = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            exp = await self.get(current_id)
            if exp is None:
                break
            chain.append(exp)
            current_id = exp.supersedes_id
        return chain

    async def delete(self, experiment_id: str) -> bool:
        """删除实验(级联删除 trial)。返回是否删除了行。"""
        model = await self._get_model(experiment_id)
        if model is None:
            return False
        await self._session.delete(model)
        await self._session.flush()
        return True

    async def _get_model(self, experiment_id: str) -> ResearchExperimentModel | None:
        stmt = select(ResearchExperimentModel).where(
            ResearchExperimentModel.experiment_id == experiment_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


class ResearchTrialRepository:
    """``TrialRecord`` 持久化仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, trial: TrialRecord) -> ResearchTrialModel:
        """插入或更新 trial(upsert by trial_id)。"""
        existing = await self._get_model(trial.trial_id)
        if existing is None:
            model = _trial_to_model(trial)
            self._session.add(model)
        else:
            _apply_trial_to_model(existing, trial)
            model = existing
        await self._session.flush()
        return model

    async def save_many(self, trials: list[TrialRecord]) -> list[ResearchTrialModel]:
        """批量保存(按顺序)。"""
        models: list[ResearchTrialModel] = []
        for t in trials:
            models.append(await self.save(t))
        return models

    async def get(self, trial_id: str) -> TrialRecord | None:
        stmt = select(ResearchTrialModel).where(
            ResearchTrialModel.trial_id == trial_id
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        if model is None:
            return None
        return _model_to_trial(model)

    async def list_by_experiment(
        self,
        experiment_id: str,
        *,
        status: TrialStatus | None = None,
    ) -> list[TrialRecord]:
        """列出某实验的全部 trial(默认按 trial_index 升序)。"""
        stmt = (
            select(ResearchTrialModel)
            .where(ResearchTrialModel.experiment_id == experiment_id)
            .order_by(ResearchTrialModel.trial_index.asc())
        )
        if status is not None:
            stmt = stmt.where(ResearchTrialModel.status == status.value)
        result = await self._session.execute(stmt)
        return [_model_to_trial(m) for m in result.scalars()]

    async def count_by_experiment(self, experiment_id: str) -> int:
        """统计某实验的 trial 总数(包括失败)。"""
        stmt = select(ResearchTrialModel).where(
            ResearchTrialModel.experiment_id == experiment_id
        )
        result = await self._session.execute(stmt)
        return len(result.scalars().all())

    async def _get_model(self, trial_id: str) -> ResearchTrialModel | None:
        stmt = select(ResearchTrialModel).where(
            ResearchTrialModel.trial_id == trial_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# 转换函数(ORM ↔ Domain)
# ---------------------------------------------------------------------------


def _experiment_to_model(exp: ResearchExperiment) -> ResearchExperimentModel:
    return ResearchExperimentModel(
        experiment_id=exp.experiment_id,
        hypothesis=exp.hypothesis,
        version_stamp=exp.version_stamp.as_dict(),
        version_checksum=exp.version_stamp.checksum(),
        plan=exp.plan.as_dict(),
        thresholds=exp.thresholds.as_dict(),
        robustness=exp.robustness.as_dict(),
        strategy_params_space=exp.strategy_params_space,
        status=exp.status.value,
        frozen_at=exp.frozen_at,
        finalized_at=exp.finalized_at,
        trials_used=exp.trials_used,
        final_test_unsealed=exp.final_test_unsealed,
        rejection_reason=exp.rejection_reason,
        supersedes_id=exp.supersedes_id,
        notes=exp.notes,
    )


def _apply_experiment_to_model(
    model: ResearchExperimentModel, exp: ResearchExperiment
) -> None:
    model.hypothesis = exp.hypothesis
    model.version_stamp = exp.version_stamp.as_dict()
    model.version_checksum = exp.version_stamp.checksum()
    model.plan = exp.plan.as_dict()
    model.thresholds = exp.thresholds.as_dict()
    model.robustness = exp.robustness.as_dict()
    model.strategy_params_space = exp.strategy_params_space
    model.status = exp.status.value
    model.frozen_at = exp.frozen_at
    model.finalized_at = exp.finalized_at
    model.trials_used = exp.trials_used
    model.final_test_unsealed = exp.final_test_unsealed
    model.rejection_reason = exp.rejection_reason
    model.supersedes_id = exp.supersedes_id
    model.notes = exp.notes


def _model_to_experiment(model: ResearchExperimentModel) -> ResearchExperiment:
    data = {
        "experiment_id": model.experiment_id,
        "hypothesis": model.hypothesis,
        "version_stamp": model.version_stamp,
        "plan": model.plan,
        "thresholds": model.thresholds,
        "robustness": model.robustness,
        "strategy_params_space": model.strategy_params_space or {},
        "status": model.status,
        "created_at": model.created_at.isoformat() if model.created_at else datetime.now(UTC).isoformat(),
        "frozen_at": model.frozen_at.isoformat() if model.frozen_at else datetime.now(UTC).isoformat(),
        "finalized_at": model.finalized_at.isoformat() if model.finalized_at else None,
        "trials_used": model.trials_used,
        "final_test_unsealed": model.final_test_unsealed,
        "rejection_reason": model.rejection_reason,
        "supersedes_id": model.supersedes_id,
        "notes": model.notes or "",
    }
    return deserialize_experiment(data)


def _trial_to_model(trial: TrialRecord) -> ResearchTrialModel:
    return ResearchTrialModel(
        trial_id=trial.trial_id,
        experiment_id=trial.experiment_id,
        trial_index=trial.trial_index,
        parameters=trial.parameters,
        status=trial.status.value,
        in_sample_metrics=trial.in_sample_metrics.as_dict() if trial.in_sample_metrics else None,
        oos_metrics=trial.oos_metrics.as_dict() if trial.oos_metrics else None,
        walk_forward_windows=[w.as_dict() for w in trial.walk_forward_windows],
        robustness_probes=[p.as_dict() for p in trial.robustness_probes],
        statistical_report=trial.statistical_report.as_dict() if trial.statistical_report else None,
        failure_reason=trial.failure_reason,
        completed_at=trial.completed_at,
    )


def _apply_trial_to_model(model: ResearchTrialModel, trial: TrialRecord) -> None:
    model.experiment_id = trial.experiment_id
    model.trial_index = trial.trial_index
    model.parameters = trial.parameters
    model.status = trial.status.value
    model.in_sample_metrics = trial.in_sample_metrics.as_dict() if trial.in_sample_metrics else None
    model.oos_metrics = trial.oos_metrics.as_dict() if trial.oos_metrics else None
    model.walk_forward_windows = [w.as_dict() for w in trial.walk_forward_windows]
    model.robustness_probes = [p.as_dict() for p in trial.robustness_probes]
    model.statistical_report = trial.statistical_report.as_dict() if trial.statistical_report else None
    model.failure_reason = trial.failure_reason
    model.completed_at = trial.completed_at


def _model_to_trial(model: ResearchTrialModel) -> TrialRecord:
    in_sample = (
        _metrics_from_dict(cast(dict[str, Any], model.in_sample_metrics))
        if model.in_sample_metrics
        else None
    )

    oos = (
        _metrics_from_dict(cast(dict[str, Any], model.oos_metrics))
        if model.oos_metrics
        else None
    )

    walk_forward = tuple(
        _metrics_from_dict(cast(dict[str, Any], w))
        for w in (model.walk_forward_windows or [])
    )

    probes = tuple(
        _probe_from_dict(cast(dict[str, Any], p))
        for p in (model.robustness_probes or [])
    )

    stat = (
        _stat_from_dict(cast(dict[str, Any], model.statistical_report))
        if model.statistical_report
        else None
    )

    return TrialRecord(
        trial_id=model.trial_id,
        experiment_id=model.experiment_id,
        trial_index=model.trial_index,
        parameters=model.parameters or {},
        status=TrialStatus(model.status),
        in_sample_metrics=in_sample,
        oos_metrics=oos,
        walk_forward_windows=walk_forward,
        robustness_probes=probes,
        statistical_report=stat,
        failure_reason=model.failure_reason,
        created_at=model.created_at or datetime.now(UTC),
        completed_at=model.completed_at,
    )


def _role_value(value: str) -> WindowRole:
    return WindowRole(value)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _metrics_from_dict(data: Mapping[str, Any]) -> WindowMetrics:
    return WindowMetrics(
        role=_role_value(cast(str, data["role"])),
        start=_parse_date(cast(str, data["start"])),
        end=_parse_date(cast(str, data["end"])),
        total_return=float(data.get("total_return", 0.0)),
        annualized_return=float(data.get("annualized_return", 0.0)),
        sharpe_ratio=float(data.get("sharpe_ratio", 0.0)),
        sortino_ratio=float(data.get("sortino_ratio", 0.0)),
        calmar_ratio=float(data.get("calmar_ratio", 0.0)),
        information_ratio=float(data.get("information_ratio", 0.0)),
        max_drawdown=float(data.get("max_drawdown", 0.0)),
        max_drawdown_duration=int(data.get("max_drawdown_duration", 0)),
        monthly_win_rate=float(data.get("monthly_win_rate", 0.0)),
        var_95=float(data.get("var_95", 0.0)),
        cvar_95=float(data.get("cvar_95", 0.0)),
        trade_count=int(data.get("trade_count", 0)),
        turnover=float(data.get("turnover", 0.0)),
        benchmark_return=float(data.get("benchmark_return", 0.0)),
        excess_return=float(data.get("excess_return", 0.0)),
    )


def _probe_from_dict(p: Mapping[str, Any]) -> RobustnessProbe:
    return RobustnessProbe(
        probe_kind=cast(str, p.get("probe_kind", "")),
        label=cast(str, p.get("label", "")),
        sharpe_ratio=float(p.get("sharpe_ratio", 0.0)),
        max_drawdown=float(p.get("max_drawdown", 0.0)),
        total_return=float(p.get("total_return", 0.0)),
        passed=bool(p.get("passed", False)),
        detail=dict(cast(dict[str, object], p.get("detail", {}))),
    )


def _stat_from_dict(data: Mapping[str, Any]) -> StatisticalReport:
    return StatisticalReport(
        deflated_sharpe_ratio=float(data.get("deflated_sharpe_ratio", 0.0)),
        probabilistic_sharpe_ratio=float(data.get("probabilistic_sharpe_ratio", 0.0)),
        pbo=float(data.get("pbo", 0.0)),
        bootstrap_sharpe_ci_low=float(data.get("bootstrap_sharpe_ci_low", 0.0)),
        bootstrap_sharpe_ci_high=float(data.get("bootstrap_sharpe_ci_high", 0.0)),
        bootstrap_mdd_ci_low=float(data.get("bootstrap_mdd_ci_low", 0.0)),
        bootstrap_mdd_ci_high=float(data.get("bootstrap_mdd_ci_high", 0.0)),
        n_trials=int(data.get("n_trials", 0)),
        methodology_notes=cast(str, data.get("methodology_notes", "")),
    )
